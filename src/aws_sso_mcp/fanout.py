"""The shared multi-account fan-out pipeline used by every account-touching tool.

``aws_query`` and ``run_dag`` differ only in *what* they do once they hold
credentials for an account. Everything around that - deciding which orgs are in
scope, resolving selectors to accounts, enforcing the blast-radius cap,
checking SSO tokens, warming role caches, minting credentials, reporting
progress, retrying access-denied failures, auditing, and shaping the response -
is identical. All of it lives here, once.

A tool therefore supplies a single callback: "given credentials for this
account, produce a result". :func:`run_fanout` does the rest.

The pipeline, in order
----------------------
1. **Scope** - decide which org(s) the call targets (:func:`resolve_scope`).
2. **Pre-flight** - confirm a valid SSO token exists for every org in scope,
   *without* opening a browser, and fail with instructions if one does not.
   This happens before planning because planning itself refreshes account
   directories, and a refresh needs a token.
3. **Plan** - turn selectors into concrete accounts, and refuse to proceed if
   that exceeds the configured blast-radius cap.
4. **Warm** - fetch each account's granted roles concurrently on the credential
   lane, so role resolution is a local lookup during the fan-out itself.
5. **Fan out** - mint credentials and run the caller's callback per account,
   fully isolating each account's failures.
6. **Report** - audit every call, redact and size-cap every result, and return
   one uniform response shape.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import boto3

from .audit import append as audit_append
from .audit import new_entry as audit_new_entry
from .engine.catalog import is_read_operation
from .engine.executor import TargetResult
from .errors import AwsCallError, McpError, NoTokenError
from .hardening import process_output
from .logging_setup import get_logger
from .planner import ResolvedTarget, resolve_targets
from .redact import redact
from .roles import RoleResolution, resolve_role
from .sso.device_auth import get_access_token
from .sso.device_auth import token_status as sso_token_status
from .state import AppState

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AccountSelector:
    """Which accounts a fan-out should target.

    Every field is optional. Filters combine with AND: an account must satisfy
    all of the supplied ones to be selected. Exclusions are applied last and
    always win.

    Attributes:
        org: Restrict to a single org alias.
        orgs: Restrict to several org aliases. Supplying this is the explicit
            opt-in to a cross-org query.
        account_name: Exact account name. Must be unambiguous across the orgs
            in scope.
        account_names: Several exact account names.
        account_id: Exact 12-digit account id.
        account_ids: Several exact account ids.
        name_contains: Case-insensitive substring of the account name.
        environment: Match accounts that the org's ``[environments]`` rules
            classify under this name.
        exclude_account_ids: Drop these accounts even if they matched.
        exclude_account_names: Drop these accounts even if they matched.
    """

    org: str | None = None
    orgs: list[str] | None = None
    account_name: str | None = None
    account_names: list[str] | None = None
    account_id: str | None = None
    account_ids: list[str] | None = None
    name_contains: str | None = None
    environment: str | None = None
    exclude_account_ids: list[str] | None = None
    exclude_account_names: list[str] | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One AWS call that was attempted against one account, for the audit log."""

    service: str
    operation: str
    ok: bool
    error: str | None = None


@dataclass(slots=True)
class AccountContext:
    """Everything a per-account callback needs, plus somewhere to record its calls.

    An instance is created per account, after credentials have been minted, and
    passed to the callback given to :func:`run_fanout`.

    Attributes:
        org: The org alias this account belongs to.
        account_id: The 12-digit AWS account id.
        account_name: The account's display name from the SSO directory.
        region: The region the call targets.
        session: A credentialed boto3 session for this account and role.
        role: How the assumed role was chosen, for auditing and explanation.
        max_items: Per-account pagination ceiling.
        calls: Calls recorded so far, in the order they were attempted.
    """

    org: str
    account_id: str
    account_name: str
    region: str
    session: boto3.Session
    role: RoleResolution
    max_items: int
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, service: str, operation: str, *, ok: bool, error: str | None = None) -> None:
        """Record one attempted AWS call so the fan-out can audit it.

        Callbacks must call this for every API call they make, successful or
        not. The fan-out writes the audit log from these records, which is what
        lets a multi-step DAG produce accurate per-step audit entries rather
        than one vague entry for the whole account.

        Args:
            service: boto3 service name.
            operation: PascalCase operation name.
            ok: Whether the call succeeded.
            error: Clean error message when ``ok`` is false.
        """
        self.calls.append(CallRecord(service=service, operation=operation, ok=ok, error=error))


# A per-account callback: given credentials and context, produce a result dict.
PerAccountCallback = Callable[[AccountContext], dict[str, Any]]


def resolve_scope(
    state: AppState, selector: AccountSelector
) -> tuple[str | None, list[str] | None]:
    """Decide which org or orgs a fan-out targets.

    Rules, applied in order:

    1. An explicit ``orgs=[...]`` wins - the user opted in to a cross-org query.
    2. An explicit ``org=`` wins.
    3. Otherwise the *active* org is used, even when other selectors are
       present. Falling back to "every configured org" here would make an
       innocuous query such as ``name_contains="prod"`` reach into orgs the
       user never mentioned, which in turn triggers surprise sign-in prompts
       for those orgs.

    Args:
        state: The running server state, used to resolve the active org.
        selector: The caller's account selectors.

    Returns:
        An ``(org, orgs)`` pair, exactly one of which is set.

    Raises:
        McpError: If no org was given and no active org is set.
    """
    if selector.orgs is not None:
        return None, list(selector.orgs)
    if selector.org is not None:
        return selector.org, None
    return state.resolve_org(None).name, None


class _FanOut:
    """One fan-out in progress: shared state for the credential and service lanes.

    Implemented as a class rather than a stack of nested closures purely for
    readability - the credential step, the service step, and the reporting step
    all need the same handful of lookups, and methods keep that explicit
    instead of burying it three closures deep.
    """

    def __init__(
        self,
        state: AppState,
        *,
        selector: AccountSelector,
        region: str,
        targets: list[ResolvedTarget],
        per_account: PerAccountCallback,
        max_items: int | None = None,
    ) -> None:
        self._state = state
        self._selector = selector
        self._region = region
        self._targets = targets
        self._per_account = per_account
        self._max_items = (
            max_items if max_items is not None else state.config.performance.max_items_per_account
        )

        # (org, account_id) -> (access_token, granted_roles), warmed up front.
        self._warmed: dict[tuple[str, str], tuple[str, list[str]]] = {}
        # (org, account_id) -> how that account's role was chosen.
        self._resolutions: dict[tuple[str, str], RoleResolution] = {}
        # (org, account_id) -> the calls its callback attempted.
        self._calls: dict[tuple[str, str], list[CallRecord]] = {}

    # ---------------------------------------------------------------- warm-up

    def warm_roles(self) -> None:
        """Fetch SSO tokens and granted roles for every target, concurrently.

        Role resolution needs to know which roles are granted on each account.
        Doing that lazily inside the fan-out would serialize an SSO call in
        front of every account's real work, so it is done up front on the
        credential lane - which also keeps it under the same concurrency cap
        that protects us from SSO throttling.

        Failures are deliberately not fatal: an account missing from the warm
        cache simply falls back to an on-demand lookup during the fan-out, so a
        transient throttle degrades performance rather than failing the query.
        """
        tokens = {org: self._token_for(org) for org in {t.org for t in self._targets}}
        logger.info(
            "Warmed SSO tokens for %d org(s); resolving roles for %d account(s)",
            len(tokens),
            len(self._targets),
        )

        def _loader(target: ResolvedTarget) -> Callable[[], tuple[str, list[str]]]:
            """Build the role lookup for one account, binding its token."""
            portal = self._state.require_portal(target.org)
            token = tokens[target.org]

            def _load() -> tuple[str, list[str]]:
                roles = self._state.cache.roles_for_account(
                    target.org,
                    target.account_id,
                    region=portal.sso_region,
                    access_token=token,
                )
                return token, roles

            return _load

        loaded = self._state.executor.map_credential_lane(
            [_loader(target) for target in self._targets]
        )
        for target, result in zip(self._targets, loaded, strict=True):
            if result is not None:
                self._warmed[(target.org, target.account_id)] = result

    def _token_for(self, org: str) -> str:
        """Return a valid SSO access token for ``org``.

        ``auto_login=False`` because :func:`preflight_tokens` has already
        confirmed a valid cached token exists. Should that guarantee ever be
        broken, this raises :class:`NoTokenError` for the affected account
        rather than opening a browser from inside a fan-out worker thread.
        """
        return get_access_token(
            self._state.state_dir, self._state.require_portal(org), auto_login=False
        )

    # ------------------------------------------------------------ credentials

    def credential_step(self, org: str, account_id: str, account_name: str) -> AccountContext:
        """Resolve the role for one account and mint credentials for it.

        Runs on the credential lane. Raising here marks only this account as
        failed; the rest of the fan-out continues.

        Returns:
            A fully populated :class:`AccountContext` for the service step.
        """
        session, resolution = self._build_session(org, account_id, account_name)
        self._resolutions[(org, account_id)] = resolution
        context = AccountContext(
            org=org,
            account_id=account_id,
            account_name=account_name,
            region=self._region,
            session=session,
            role=resolution,
            max_items=self._max_items,
        )
        self._calls[(org, account_id)] = context.calls
        return context

    def _build_session(
        self, org: str, account_id: str, account_name: str, *, refresh: bool = False
    ) -> tuple[boto3.Session, RoleResolution]:
        """Resolve a role and mint a boto3 session for one account.

        Args:
            org: Org alias.
            account_id: Target account.
            account_name: Target account's display name, used for environment
                classification.
            refresh: Bypass the warm cache and re-fetch granted roles. Used by
                the access-denied retry, where the cached role list is the most
                likely thing to be wrong.

        Returns:
            The credentialed session and the role resolution that produced it.
        """
        portal = self._state.require_portal(org)
        warmed = None if refresh else self._warmed.get((org, account_id))

        if warmed is not None:
            token, granted = warmed
        else:
            token = self._token_for(org)
            granted = self._state.cache.roles_for_account(
                org,
                account_id,
                region=portal.sso_region,
                access_token=token,
                force=refresh,
            )

        resolution = resolve_role(
            portal=portal,
            account_id=account_id,
            account_name=account_name,
            granted_roles=granted,
        )
        session = self._state.broker.get_session(
            org=org,
            region=portal.sso_region,
            account_id=account_id,
            role=resolution.role,
            access_token=token,
        )
        return session, resolution

    # ---------------------------------------------------------------- service

    def service_step(
        self, org: str, account_id: str, account_name: str, context: object
    ) -> dict[str, Any]:
        """Run the caller's callback for one account, retrying once on access-denied.

        An ``AccessDenied`` almost always means the cached role list is stale -
        the role was revoked, renamed, or newly granted since the directory was
        last refreshed. Rather than failing until the cache TTL expires, the
        stale role and its cached credentials are dropped and the account is
        retried once with a freshly fetched role list. Only one retry is
        attempted, so a genuine permissions problem still fails promptly.

        Runs on the service lane.
        """
        assert isinstance(context, AccountContext)  # noqa: S101 - lane contract
        try:
            return self._per_account(context)
        except AwsCallError as exc:
            if not exc.is_access_denied:
                raise
            logger.info(
                "Access denied for %s/%s with role %s; refreshing role cache and retrying once",
                org,
                account_id,
                context.role.role,
            )
            return self._retry_after_access_denied(org, account_id, account_name, context)

    def _retry_after_access_denied(
        self, org: str, account_id: str, account_name: str, context: AccountContext
    ) -> dict[str, Any]:
        """Drop this account's cached role and credentials, then run the callback again."""
        self._state.cache.invalidate_role(org, account_id, context.role.role)
        self._state.broker.invalidate(org=org, account_id=account_id, role=context.role.role)

        session, resolution = self._build_session(org, account_id, account_name, refresh=True)
        self._resolutions[(org, account_id)] = resolution

        # Start the retry with a clean call list so the audit log shows the
        # calls that actually determined the outcome, not the failed first try
        # followed by a confusingly similar second one.
        context.session = session
        context.role = resolution
        context.calls.clear()
        return self._per_account(context)

    # --------------------------------------------------------------- reporting

    def build_response(
        self,
        outcomes: list[TargetResult[dict[str, Any]]],
        *,
        fallback_label: tuple[str, str],
    ) -> dict[str, Any]:
        """Audit every outcome and shape the final tool response.

        Args:
            outcomes: One result per target, from the executor.
            fallback_label: ``(service, operation)`` used for the audit entry
                when an account failed before making any call at all - a
                credential or role-resolution failure, say - and therefore has
                nothing of its own to report.

        Returns:
            The tool response: a target count, a success count, and one entry
            per account, ordered by ``(org, account_name)``.

        Note:
            The executor yields outcomes in *completion* order, which varies
            run to run with network timing. Sorting here means the same query
            produces a byte-identical response every time, so a user comparing
            two runs sees only the differences that are real, and a model
            re-reading a result finds accounts where it left them.
        """
        results: list[dict[str, Any]] = []
        ok_count = 0

        for outcome in sorted(outcomes, key=lambda o: (o.org, o.account_name, o.account_id)):
            key = (outcome.org, outcome.account_id)
            resolution = self._resolutions.get(key)
            self._audit_outcome(outcome, resolution, fallback_label)

            entry: dict[str, Any] = {
                "org": outcome.org,
                "account_id": outcome.account_id,
                "account_name": outcome.account_name,
                "ok": outcome.ok,
            }
            if resolution is not None:
                entry["role"] = resolution.role
            if outcome.ok:
                ok_count += 1
                entry["data"] = process_output(outcome.value)
            else:
                entry["error"] = redact(outcome.error or "unknown error")
            results.append(entry)

        return {
            "target_count": len(results),
            "ok_count": ok_count,
            "failed_count": len(results) - ok_count,
            "results": results,
        }

    def _audit_outcome(
        self,
        outcome: TargetResult[dict[str, Any]],
        resolution: RoleResolution | None,
        fallback_label: tuple[str, str],
    ) -> None:
        """Write audit entries for one account's outcome.

        One entry is written per call the callback actually attempted. If the
        account failed before attempting any - which is what a credential or
        role-resolution failure looks like - a single entry is written using
        ``fallback_label`` so the failure is never silently absent from the log.
        """
        recorded = self._calls.get((outcome.org, outcome.account_id), [])
        if not recorded:
            service, operation = fallback_label
            recorded = [
                CallRecord(
                    service=service,
                    operation=operation,
                    ok=outcome.ok,
                    error=outcome.error,
                )
            ]

        for call in recorded:
            audit_append(
                self._state.audit_log_path,
                audit_new_entry(
                    org=outcome.org,
                    account_id=outcome.account_id,
                    account_name=outcome.account_name,
                    role=resolution.role if resolution else None,
                    role_source=resolution.source if resolution else None,
                    region=self._region,
                    service=call.service,
                    operation=call.operation,
                    write=not is_read_operation(call.operation),
                    ok=call.ok,
                    error=call.error,
                ),
            )


def plan_targets(
    state: AppState,
    selector: AccountSelector,
    *,
    max_accounts: int | None = None,
) -> list[ResolvedTarget]:
    """Resolve selectors to concrete accounts, enforcing the blast-radius cap.

    Args:
        state: The running server state.
        selector: The caller's account selectors.
        max_accounts: Override the configured cap for this call. Use this to
            deliberately run a very wide query.

    Returns:
        The accounts the query will touch. May be empty.

    Raises:
        McpError: If the selection exceeds the cap. A query that silently
            touches 700 accounts when the user meant 7 is expensive at best and
            destructive at worst, so this is an error the caller must
            acknowledge rather than a warning they might miss.
    """
    org, orgs = resolve_scope(state, selector)
    targets = resolve_targets(
        config=state.config,
        cache=state.cache,
        # auto_login=False is load-bearing, not belt-and-braces. Resolving
        # targets refreshes each org's directory, and a refresh needs a token;
        # with auto-login enabled that call would open a browser from inside
        # the planning phase. preflight_tokens() has already guaranteed a valid
        # cached token for every org in scope, so this only ever reads it.
        access_token_getter=lambda name: get_access_token(
            state.state_dir, state.require_portal(name), auto_login=False
        ),
        org=org,
        orgs=orgs,
        account_name=selector.account_name,
        account_names=selector.account_names,
        account_id=selector.account_id,
        account_ids=selector.account_ids,
        name_contains=selector.name_contains,
        environment=selector.environment,
        exclude_account_ids=selector.exclude_account_ids,
        exclude_account_names=selector.exclude_account_names,
    )

    cap = max_accounts if max_accounts is not None else state.config.performance.max_accounts
    if len(targets) > cap:
        raise McpError(
            f"This selection resolves to {len(targets)} accounts, which exceeds the "
            f"blast-radius limit of {cap}. Narrow the selection (account_names=, "
            f"name_contains=, environment=), or pass max_accounts={len(targets)} to "
            "confirm you intend to touch them all."
        )
    return targets


def preflight_tokens(state: AppState, selector: AccountSelector) -> None:
    """Verify a valid SSO token is cached for every org in scope, without logging in.

    This runs **before** any account planning, and that ordering is the whole
    point. Planning refreshes each org's account directory, and a directory
    refresh needs an SSO token; if it were allowed to fetch one itself it would
    open a browser and block the entire tool call - potentially for minutes -
    from inside what the user experiences as an ordinary query. Checking up
    front turns that into an immediate, actionable message instead.

    The scope is derived from the selector rather than from resolved targets,
    precisely so that no cache read has happened yet when this check runs.

    Args:
        state: The running server state.
        selector: The caller's account selectors, used to decide which orgs
            are in scope.

    Raises:
        NoTokenError: If any org in scope has no valid cached token, naming the
            exact ``login()`` call needed to fix it.
    """
    org, orgs = resolve_scope(state, selector)
    scope = [org] if org is not None else list(orgs or ())

    needs_login = []
    for name in sorted(set(scope)):
        # Fail fast on an unknown org alias rather than reporting it as a
        # missing sign-in, which would send the user to login() for an org
        # that does not exist.
        state.require_portal(name)
        status = sso_token_status(state.state_dir, name)
        if not status.valid:
            needs_login.append(f"{name} ({status.reason})")

    if needs_login:
        summary = ", ".join(needs_login)
        first = needs_login[0].split(" ")[0]
        raise NoTokenError(
            f'No valid SSO session for: {summary}. Run login(org="{first}") to sign in, '
            "then retry. Sign-in is never started automatically mid-query, because it "
            "opens a browser and blocks until you approve it."
        )


def progress_reporter(ctx: Any, verb: str) -> Callable[[int, int], None] | None:
    """Build a fan-out progress callback that reports through the MCP client.

    The tool body runs on an AnyIO worker thread while the fan-out's progress
    callback fires on a dispatch thread. The anyio event-loop token is therefore
    captured here - on the worker thread, where it is still valid - and passed
    explicitly on every nested call.

    Args:
        ctx: The MCP ``Context`` for the in-flight tool call, or ``None``.
        verb: Past-tense verb for the message, e.g. ``"queried"``.

    Returns:
        A ``(done, total)`` callback, or ``None`` when progress cannot be
        reported. Every failure inside the callback is swallowed: progress must
        never break a query.
    """
    if ctx is None:
        return None

    import anyio.from_thread

    try:
        token = anyio.from_thread.threadlocals.current_token  # type: ignore[attr-defined]
    except AttributeError:
        return None

    def _report(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            anyio.from_thread.run(
                ctx.report_progress,
                float(done),
                float(total),
                f"{verb} {done}/{total} accounts",
                token=token,
            )

    return _report


def run_fanout(
    state: AppState,
    *,
    selector: AccountSelector,
    region: str,
    per_account: PerAccountCallback,
    audit_label: tuple[str, str],
    verb: str,
    ctx: Any = None,
    max_accounts: int | None = None,
    max_items: int | None = None,
    confirm: Callable[[Sequence[ResolvedTarget]], None] | None = None,
) -> dict[str, Any]:
    """Run ``per_account`` against every account the selector resolves to.

    This is the single entry point every account-touching tool uses.

    Args:
        state: The running server state.
        selector: Which accounts to target.
        region: The region every call is made in. Never defaulted.
        per_account: Called once per account with an :class:`AccountContext`
            carrying a credentialed session. Must record each AWS call it makes
            via :meth:`AccountContext.record`.
        audit_label: ``(service, operation)`` used for the audit entry when an
            account fails before making any call.
        verb: Past-tense verb used in progress messages, e.g. ``"queried"``.
        ctx: The MCP ``Context``, used for progress reporting.
        max_accounts: Override the configured blast-radius cap.
        max_items: Override the configured per-account pagination ceiling.
            ``None`` uses ``performance.max_items_per_account``.
        confirm: Called with the resolved accounts before anything runs; it
            raises to stop the fan-out (see ``confirmation.make_confirmer``).
            Skipped when no account matched, since nothing would be touched.

    Returns:
        A uniform response: ``target_count``, ``ok_count``, ``failed_count``,
        and one ``results`` entry per account, ordered by org then account
        name. If no account matched, a ``warning`` explains that instead.

    Raises:
        McpError: If the selection exceeds the blast-radius cap, or ``confirm``
            refuses.
        NoTokenError: If any org in scope needs an interactive sign-in first.
    """
    # Token check first: planning refreshes account directories, which needs a
    # token, so checking afterwards would be too late to prevent a surprise
    # browser prompt mid-query.
    preflight_tokens(state, selector)

    targets = plan_targets(state, selector, max_accounts=max_accounts)
    if not targets:
        return {
            "target_count": 0,
            "ok_count": 0,
            "failed_count": 0,
            "results": [],
            "warning": "No accounts matched the given selectors.",
        }

    # After planning so the user sees the exact accounts, before any credential is minted.
    if confirm is not None:
        confirm(targets)

    fan = _FanOut(
        state,
        selector=selector,
        region=region,
        targets=targets,
        per_account=per_account,
        max_items=max_items,
    )
    fan.warm_roles()

    outcomes = state.executor.fan_out(
        [(t.org, t.account_id, t.account_name) for t in targets],
        credential_fn=fan.credential_step,
        service_fn=fan.service_step,
        on_progress=progress_reporter(ctx, verb),
    )
    response = fan.build_response(outcomes, fallback_label=audit_label)
    logger.info(
        "Fan-out complete: %d/%d accounts succeeded",
        response["ok_count"],
        response["target_count"],
    )
    return response
