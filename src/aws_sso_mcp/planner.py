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
from .config import Config
from .errors import AccountNotFoundError, AmbiguousAccountError
from .roles import classify_environment


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
        access_token_getter: Called with an org name, returns a valid SSO
            access token for it - called lazily, and only for orgs whose
            cache actually needs refreshing.
        org / orgs: Restrict the search to one or more orgs. If neither is
            given, every configured org is searched (cross-org lookup).
        account_name / account_names: Exact account-name match(es).
        account_id / account_ids: Exact account-id match(es).
        name_contains: Case-sensitive substring match on account name.
        environment: Match accounts classified into this environment name
            by the org's ``[portals.<org>.environments.<env>]`` patterns
            (an account with no configured environments never matches).
        exclude_account_ids / exclude_account_names: Drop these even if
            they matched another filter above.
        force_refresh: Force a directory refresh for every candidate org
            before searching, bypassing the TTL.

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

    names_wanted = set(account_names or ())
    if account_name is not None:
        names_wanted.add(account_name)
    ids_wanted = set(account_ids or ())
    if account_id is not None:
        ids_wanted.add(account_id)
    excluded_ids = set(exclude_account_ids or ())
    excluded_names = set(exclude_account_names or ())

    matches: list[ResolvedTarget] = []
    for candidate in candidate_orgs:
        portal = config.portals[candidate]
        for account in cache.accounts_for_org(candidate):
            if names_wanted and account.account_name not in names_wanted:
                continue
            if ids_wanted and account.account_id not in ids_wanted:
                continue
            if name_contains and name_contains not in account.account_name:
                continue
            if environment is not None:
                env = classify_environment(portal, account.account_name)
                if env is None or env.name != environment:
                    continue
            if account.account_id in excluded_ids or account.account_name in excluded_names:
                continue
            matches.append(
                ResolvedTarget(
                    org=candidate, account_id=account.account_id, account_name=account.account_name
                )
            )

    if not matches and (names_wanted or ids_wanted):
        raise AccountNotFoundError(
            f"No account matched name(s)={sorted(names_wanted) or None} "
            f"id(s)={sorted(ids_wanted) or None} in org(s)={candidate_orgs}."
        )

    if account_name is not None:
        exact = [m for m in matches if m.account_name == account_name]
        if len(exact) > 1:
            raise AmbiguousAccountError(account_name, [(m.org, m.account_id) for m in exact])

    # De-dup by (org, account_id) in case overlapping filters double-matched.
    seen: set[tuple[str, str]] = set()
    deduped: list[ResolvedTarget] = []
    for match in matches:
        key = (match.org, match.account_id)
        if key not in seen:
            seen.add(key)
            deduped.append(match)
    return deduped
