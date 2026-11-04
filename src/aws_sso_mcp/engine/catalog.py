"""The generic AWS call engine - the only place this server talks to a service API.

There are no per-service, per-question hardcoded tools. A caller supplies
``service`` + ``operation`` + ``params`` using exactly the shape an AWS CLI
command would take (``ec2`` / ``DescribeInstances`` /
``{"Filters": [...]}``), and this module dispatches it through boto3. botocore
validates the operation name and every parameter against the service's own JSON
model, so no hand-written schema needs to be maintained here.

Authorization
-------------
AWS IAM is the sole authorization boundary. Any operation the assumed role is
permitted to perform - read *or* mutating - is dispatched, and IAM alone decides
the outcome. This module never refuses a call that AWS itself would allow; if
the role lacks permission, AWS returns AccessDenied and that error is surfaced
verbatim (minus transport noise) to the caller.

The classification helpers :func:`is_read_operation` and
:func:`operation_risk` do not authorize anything. The first *labels*
each call read-vs-write in the audit log; the second tells the caller when to
ask the user for consent first (see ``confirmation.py``). Neither refuses a call.

Pagination
----------
Most AWS "list" style APIs return one page and a continuation token. Calling
such an API once and ignoring the token yields a silently incomplete answer -
"you have 1000 objects" when you have 50,000. :func:`execute_call` therefore
uses botocore's paginator whenever the operation supports one, and merges every
page into a single response. A hard item cap keeps a single account from
returning an unbounded amount of data, and when that cap truncates the result
the response says so explicitly rather than quietly lying.
"""

from __future__ import annotations

from typing import Any, Literal

import boto3
from botocore import xform_name
from botocore.exceptions import BotoCoreError, ClientError

from ..awscfg import build_client
from ..config import DEFAULT_MAX_ITEMS
from ..errors import AwsCallError, RegionError
from ..logging_setup import get_logger

logger = get_logger(__name__)

# Verb prefixes AWS uses near-universally for read-only operations.
_READ_PREFIXES = ("Describe", "List", "Get", "Head")

# Unambiguously read-only operations that do not match the prefixes above.
# Extend this set as new safe verbs are identified; never widen _READ_PREFIXES
# itself, since that would silently admit whole families of operations without
# review.
_READ_OPERATION_ALLOWLIST = frozenset(
    {
        "LookupEvents",  # cloudtrail
        "Query",  # dynamodb
        "Scan",  # dynamodb
        "BatchGetItem",  # dynamodb
        "SimulatePrincipalPolicy",  # iam - evaluates policy, mutates nothing
        "SimulateCustomPolicy",  # iam
        "FilterLogEvents",  # logs
        "TestMetricFilter",  # logs
    }
)

# Key added to a response when the item cap stopped pagination early, so the
# caller can tell "this is all of it" from "this is the first N".
TRUNCATED_KEY = "_truncated"


def is_read_operation(operation: str) -> bool:
    """Return ``True`` if ``operation`` is read-only.

    Args:
        operation: A PascalCase operation name as it appears in botocore's
            service model, e.g. ``"DescribeInstances"``.

    Returns:
        ``True`` for an operation known to mutate nothing. Used only to label
        audit-log entries, never to allow or deny a call.
    """
    return operation.startswith(_READ_PREFIXES) or operation in _READ_OPERATION_ALLOWLIST


# Verb prefixes that need the user's confirmation, grouped by what to warn about. A
# name that is not listed simply gets no prompt, so extend these rather than guess.
_IRREVERSIBLE_PREFIXES = (
    "Delete",
    "Terminate",
    "Remove",
    "Detach",
    "Disassociate",
    "Deregister",
    "Revoke",
    "Purge",
    "Destroy",
    "Release",
)
_CHANGE_PREFIXES = (
    "Stop",
    "Reboot",
    "Disable",
    "Update",
    "Put",
    "Cancel",
    "Modify",
    "Reset",
    "Suspend",
)
_COST_PREFIXES = ("Create", "Run", "Start", "Launch", "Allocate")

# Irreversible operations whose names do not start with an irreversible verb.
_IRREVERSIBLE_OPERATION_ALLOWLIST = frozenset({"ScheduleKeyDeletion"})

type Risk = Literal["irreversible", "change", "cost"]


def operation_risk(operation: str) -> Risk | None:
    """Classify what the user should be warned about before ``operation`` runs.

    A leading ``Batch`` is ignored so ``BatchDeleteImage`` counts as a delete.

    Args:
        operation: A PascalCase operation name, e.g. ``"TerminateInstances"``.

    Returns:
        ``"irreversible"`` (deletes or detaches), ``"change"`` (stops, disables or
        modifies), ``"cost"`` (creates or starts, so it may incur charges), or
        ``None`` for everything else. Used only to decide whether to ask, never
        to allow or deny a call.
    """
    verb = operation.removeprefix("Batch")
    if verb.startswith(_IRREVERSIBLE_PREFIXES) or operation in _IRREVERSIBLE_OPERATION_ALLOWLIST:
        return "irreversible"
    if verb.startswith(_CHANGE_PREFIXES):
        return "change"
    if verb.startswith(_COST_PREFIXES):
        return "cost"
    return None


def _raise_clean_error(service: str, operation: str, exc: Exception) -> AwsCallError:
    """Convert a raw botocore exception into a clean, user-facing error.

    Raw botocore exceptions can embed partial request and response data in their
    ``repr``, so they must never cross a tool boundary. Only the AWS error code
    and message are extracted.

    Args:
        service: boto3 service name, used for the message.
        operation: PascalCase operation name, used for the message.
        exc: The ``ClientError`` or ``BotoCoreError`` that was raised.

    Returns:
        An :class:`AwsCallError` ready to be raised by the caller.
    """
    response_data = getattr(exc, "response", {}) or {}
    error_data = response_data.get("Error", {}) if isinstance(response_data, dict) else {}
    code = error_data.get("Code", type(exc).__name__)
    message = error_data.get("Message", "Call failed.")
    return AwsCallError(service, operation, code, message)


def _call_paginated(
    client: Any,
    operation: str,
    python_operation: str,
    params: dict[str, Any],
    max_items: int,
) -> dict[str, Any]:
    """Run a paginated operation to completion (or to ``max_items``) and merge the pages.

    botocore's ``build_full_result`` concatenates the paginated result keys
    across pages, so the merged response has the same shape as a single page -
    callers never have to know whether pagination happened.

    Args:
        client: The boto3 client to call.
        operation: PascalCase operation name, for error messages.
        python_operation: The snake_case method name botocore knows it by.
        params: Caller-supplied parameters.
        max_items: Ceiling on items collected from the primary result key.

    Returns:
        The merged response. If the cap stopped pagination early, the response
        carries a ``_truncated`` marker explaining what happened.
    """
    paginator = client.get_paginator(python_operation)
    page_iterator = paginator.paginate(**params, PaginationConfig={"MaxItems": max_items})
    result: dict[str, Any] = page_iterator.build_full_result()

    # build_full_result only emits a continuation token when MaxItems cut the
    # iteration short, so its presence is exactly our "there is more" signal.
    continuation = result.pop("NextToken", None)
    if continuation is not None:
        result[TRUNCATED_KEY] = (
            f"Stopped after {max_items} items for {operation}; more results exist. "
            f"Narrow the query with filters, or retry with max_items={max_items * 2}."
        )
    return result


def execute_call(
    *,
    session: boto3.Session,
    region: str,
    service: str,
    operation: str,
    params: dict[str, Any],
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict[str, Any]:
    """Execute one AWS API call, returning a plain dict.

    No operation is refused here. Whatever the assumed role is permitted to do,
    read or mutating, is dispatched; AWS IAM decides the outcome and any denial
    comes back as a clean :class:`~aws_sso_mcp.errors.AwsCallError`.

    When the operation supports pagination, every page is fetched and merged
    (up to ``max_items``) so the caller receives a complete answer rather than
    an unmarked first page.

    Args:
        session: A credentialed session from
            :class:`~aws_sso_mcp.sso.broker.CredentialBroker`.
        region: Required on every call - never defaulted.
        service: A boto3 service name, e.g. ``"ec2"``.
        operation: The PascalCase operation name as it appears in botocore's
            service model / the AWS CLI docs, e.g. ``"DescribeInstances"``.
        params: Keyword parameters for the call. Validated by botocore itself
            against the service model.
        max_items: Ceiling on items returned from a paginated operation.

    Returns:
        The API response as a plain dict, with botocore's ``ResponseMetadata``
        stripped (it is pure transport noise and wastes model context).

    Raises:
        RegionError: If ``region`` is empty.
        AwsCallError: If the operation does not exist for this service, or
            botocore/AWS rejects or fails the call - including invalid
            parameters, throttling, and access-denied.
    """
    if not region:
        raise RegionError("A region is required for every AWS call - none was supplied.")

    client = build_client(service, region=region, session=session)

    # botocore exposes operations as snake_case methods; the caller names them
    # in PascalCase, matching the AWS docs.
    python_operation = xform_name(operation)
    if getattr(client, python_operation, None) is None:
        raise AwsCallError(
            service,
            operation,
            "UnknownOperation",
            f"{service} has no operation named {operation!r}. Use the exact PascalCase name "
            "from the AWS API reference.",
        )

    # Log parameter *names* only. Values can contain customer data, and this
    # line is emitted for every account in a fan-out.
    logger.debug("%s:%s region=%s params=%s", service, operation, region, sorted(params))

    try:
        if client.can_paginate(python_operation):
            response = _call_paginated(client, operation, python_operation, params, max_items)
        else:
            response = getattr(client, python_operation)(**params)
    except (ClientError, BotoCoreError) as exc:
        raise _raise_clean_error(service, operation, exc) from exc

    response.pop("ResponseMetadata", None)
    return response
