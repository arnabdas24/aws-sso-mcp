"""Account/role selector planner.

Resolves *which* accounts a query touches - by org, name, id, substring, or
configured environment tag - into a deduplicated list of concrete targets.
This module has no opinion about *what* AWS call will be made against those
targets; that is the generic engine's job (``engine/catalog.py``).

No per-question hardcoding lives here: every selector is a generic filter
over the cached account directory, so a brand-new kind of question never
requires new planner code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .cache.accounts import AccountCache
from .config import Config, PortalConfig
from .errors import AccountNotFoundError, AmbiguousAccountError
from .roles import classify_environment
from .sso.directory import SsoAccount


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """One concrete account this query will run against."""

    org: str
    account_id: str
    account_name: str


def _candidate_orgs(config: Config, *, org: str | None, orgs: list[str] | None) -> list[str]:
    if org is not None:
        return [org]
    if orgs is not None:
        return list(dict.fromkeys(orgs))  # de-dup, preserve order
    return list(config.portals)  # search every configured org


def _bind_token_getter(getter: Callable[[str], str], org: str) -> Callable[[], str]:
    """Bind an org-parameterized token getter to one org, for AccountCache.ensure_fresh."""

    def _get() -> str:
        return getter(org)

    return _get


@dataclass(frozen=True, slots=True)
class _AccountFilter:
    """The selector set, normalized once into the form the per-account test needs.

    Building this up front keeps :func:`resolve_targets` readable and does the
    set-building and lower-casing a single time rather than once per account -
    which matters, because :meth:`matches` runs against every account in every
    candidate org.

    Attributes:
        names: Account names that may match; empty means "no name filter".
        ids: Account ids that may match; empty means "no id filter".
        contains: Lower-cased substring the account name must contain.
        environment: Environment name the account must classify into.
        excluded_ids: Account ids dropped even when they matched.
        excluded_names: Account names dropped even when they matched.
    """

    names: frozenset[str]
    ids: frozenset[str]
    contains: str | None
    environment: str | None
    excluded_ids: frozenset[str]
    excluded_names: frozenset[str]

    @property
    def has_explicit_identity(self) -> bool:
        """Whether the caller named specific accounts, by name or by id.

        Used to decide whether an empty result is an error: asking for
        ``account_name="payments-prod"`` and getting nothing back is a
        mistake worth raising, whereas a substring or environment filter
        matching nothing is a legitimate empty answer.
        """
        return bool(self.names or self.ids)

    def matches(self, portal: PortalConfig, account: SsoAccount) -> bool:
        """Return whether one account passes every active filter."""
        if self.names and account.account_name not in self.names:
            return False
        if self.ids and account.account_id not in self.ids:
            return False
        if self.contains and self.contains not in account.account_name.lower():
            return False
        if self.environment is not None:
            env = classify_environment(portal, account.account_name)
            if env is None or env.name != self.environment:
                return False
        return (
            account.account_id not in self.excluded_ids
            and account.account_name not in self.excluded_names
        )


def _build_filter(
    *,
    account_name: str | None,
    account_names: list[str] | None,
    account_id: str | None,
    account_ids: list[str] | None,
    name_contains: str | None,
    environment: str | None,
    exclude_account_ids: list[str] | None,
    exclude_account_names: list[str] | None,
) -> _AccountFilter:
    """Collapse the singular/plural selector pairs into one normalized filter."""
    names = set(account_names or ())
    if account_name is not None:
        names.add(account_name)
    ids = set(account_ids or ())
    if account_id is not None:
        ids.add(account_id)
    return _AccountFilter(
        names=frozenset(names),
        ids=frozenset(ids),
        contains=name_contains.lower() if name_contains else None,
        environment=environment,
        excluded_ids=frozenset(exclude_account_ids or ()),
        excluded_names=frozenset(exclude_account_names or ()),
    )


def resolve_targets(
    *,
    config: Config,
    cache: AccountCache,
    access_token_getter: Callable[[str], str],
    org: str | None = None,
    orgs: list[str] | None = None,
    account_name: str | None = None,
    account_names: list[str] | None = None,
    account_id: str | None = None,
    account_ids: list[str] | None = None,
    name_contains: str | None = None,
    environment: str | None = None,
    exclude_account_ids: list[str] | None = None,
    exclude_account_names: list[str] | None = None,
    force_refresh: bool = False,
) -> list[ResolvedTarget]:
    """Resolve the concrete ``(org, account_id, account_name)`` targets for a query.

    Args:
        config: The loaded configuration, used to look up portals.
        cache: The account directory cache, refreshed as needed before searching.
        access_token_getter: Called with an org name, returns a valid SSO
            access token for it - called lazily, and only for orgs whose
            cache actually needs refreshing.
        org / orgs: Restrict the search to one or more orgs. If neither is
            given, every configured org is searched (cross-org lookup).
        account_name / account_names: Exact account-name match(es).
        account_id / account_ids: Exact account-id match(es).
        name_contains: Case-insensitive substring match on account name. This
            matches :meth:`AccountCache.search_accounts` exactly, so the same
            value selects the same accounts whether a user is searching the
            directory or running a query.
        environment: Match accounts classified into this environment name
            by the org's ``[portals.<org>.environments.<env>]`` patterns
            (an account with no configured environments never matches).
        exclude_account_ids / exclude_account_names: Drop these even if
            they matched another filter above.
        force_refresh: Force a directory refresh for every candidate org
            before searching, bypassing the TTL.

    Returns:
        The matching targets, ordered by org then account name. No
        de-duplication is needed: candidate orgs are de-duplicated, and
        ``(org, account_id)`` is the cache's primary key, so one account can
        be visited at most once.

    Raises:
        AccountNotFoundError: If an explicit name/id filter matched nothing.
        AmbiguousAccountError: If an explicit ``account_name`` (singular)
            matches more than one account across the searched orgs.
    """
    candidate_orgs = _candidate_orgs(config, org=org, orgs=orgs)
    for candidate in candidate_orgs:
        portal = config.portals[candidate]
        cache.ensure_fresh(
            candidate,
            region=portal.sso_region,
            access_token_getter=_bind_token_getter(access_token_getter, candidate),
            force=force_refresh,
        )

    account_filter = _build_filter(
        account_name=account_name,
        account_names=account_names,
        account_id=account_id,
        account_ids=account_ids,
        name_contains=name_contains,
        environment=environment,
        exclude_account_ids=exclude_account_ids,
        exclude_account_names=exclude_account_names,
    )

    matches = [
        ResolvedTarget(
            org=candidate, account_id=account.account_id, account_name=account.account_name
        )
        for candidate in candidate_orgs
        for account in cache.accounts_for_org(candidate)
        if account_filter.matches(config.portals[candidate], account)
    ]

    if not matches and account_filter.has_explicit_identity:
        raise AccountNotFoundError(
            f"No account matched name(s)={sorted(account_filter.names) or None} "
            f"id(s)={sorted(account_filter.ids) or None} in org(s)={candidate_orgs}."
        )

    if account_name is not None:
        exact = [m for m in matches if m.account_name == account_name]
        if len(exact) > 1:
            raise AmbiguousAccountError(account_name, [(m.org, m.account_id) for m in exact])

    return matches
