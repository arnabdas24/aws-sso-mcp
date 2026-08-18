"""Generic, schema-validated AWS read-query engine.

This is the sole mechanism for actually calling an AWS API in this server -
there are no per-service, per-question hardcoded tools. A caller supplies
``service`` + ``operation`` + ``params`` (the same shape as an AWS CLI
command, e.g. ``ec2`` / ``DescribeInstances`` / ``{"Filters": [...]}"``);
this module classifies the operation, enforces the read-only and
secret-value gates, then dispatches through boto3/botocore, which performs
its own parameter validation against the service's JSON model.

AWS's own IAM evaluation is still the real authorization boundary - this
module only prevents *mutating* calls and *ungated secret-value* reads from
ever reaching AWS in the first place; it never grants anything AWS itself
would deny.
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore import xform_name
from botocore.exceptions import BotoCoreError, ClientError

from ..errors import AwsCallError, OperationDeniedError, RegionError

# Verb prefixes AWS uses near-universally for read-only operations.
_READ_PREFIXES = ("Describe", "List", "Get", "Head")

# Unambiguously read-only operations that don't match the prefixes above.
# Extend this set as new safe verbs are identified - never widen
# _READ_PREFIXES itself, since that would silently admit whole families of
# operations without review.
_READ_OPERATION_ALLOWLIST = frozenset(
    {
        "LookupEvents",  # cloudtrail
        "Query",  # dynamodb
        "Scan",  # dynamodb
        "BatchGetItem",  # dynamodb
        "SimulatePrincipalPolicy",  # iam - evaluates policy, mutates nothing
        "SimulateCustomPolicy",
        "FilterLogEvents",  # logs
        "TestMetricFilter",  # logs
    }
)

# (service, operation) -> the params key that, when truthy, turns this call
# into one returning decrypted/plaintext secret material. ``None`` means the
# operation always returns the value (no gating param to check).
_SECRET_VALUE_OPERATIONS: dict[tuple[str, str], str | None] = {
    ("secretsmanager", "GetSecretValue"): None,
    ("kms", "Decrypt"): None,
    ("s3", "GetObject"): None,
    ("ssm", "GetParameter"): "WithDecryption",
    ("ssm", "GetParameters"): "WithDecryption",
    ("ssm", "GetParametersByPath"): "WithDecryption",
}


def is_read_operation(operation: str) -> bool:
    """True if ``operation`` (PascalCase, as in botocore's service model) is read-only."""
    return operation.startswith(_READ_PREFIXES) or operation in _READ_OPERATION_ALLOWLIST


def requires_secret_gate(service: str, operation: str, params: dict[str, Any]) -> bool:
    """True if this exact call would return decrypted/plaintext secret material."""
    key = (service, operation)
    if key not in _SECRET_VALUE_OPERATIONS:
        return False
    gate_param = _SECRET_VALUE_OPERATIONS[key]
    if gate_param is None:
        return True
    return bool(params.get(gate_param))


def validate_operation(
    *, service: str, operation: str, params: dict[str, Any], allow_secret_values: bool
) -> None:
    """Raise :class:`OperationDeniedError` if this call is mutating or an ungated secret read."""
    if not is_read_operation(operation):
        raise OperationDeniedError(
            f"{service}:{operation} is not a recognized read-only operation - only "
            "Describe*/List*/Get*/Head* and a small explicit allowlist are permitted."
        )
    if requires_secret_gate(service, operation, params) and not allow_secret_values:
        raise OperationDeniedError(
            f"{service}:{operation} would return decrypted/plaintext secret material, which "
            "is disabled by config (allow_secret_values = false)."
        )


def execute_read(
    *,
    session: boto3.Session,
    region: str,
    service: str,
    operation: str,
    params: dict[str, Any],
    allow_secret_values: bool,
) -> dict[str, Any]:
    """Validate and execute one read-only AWS API call, returning a plain dict.

    Args:
        session: A credentialed boto3 session (from :class:`CredentialBroker`).
        region: Required explicitly on every call - never defaulted.
        service: A boto3 service name, e.g. ``"ec2"``.
        operation: The PascalCase API operation name, e.g. ``"DescribeInstances"``
            (as it appears in botocore's service model / the AWS CLI docs).
        params: Keyword parameters for the call, validated by botocore itself.
        allow_secret_values: Whether value-returning secret reads are permitted.

    Raises:
        RegionError: If ``region`` is empty.
        OperationDeniedError: If the operation is mutating, unrecognized, or
            an ungated secret-value read.
        AwsCallError: If botocore/AWS rejects or fails the call (including
            bad parameters, throttling, or access-denied).
    """
    if not region:
        raise RegionError("A region is required for every AWS read call - none was supplied.")

    validate_operation(
        service=service,
        operation=operation,
        params=params,
        allow_secret_values=allow_secret_values,
    )

    # boto3-stubs' Session.client() overloads require a Literal service name;
    # here the service name is inherently dynamic (any AWS service the
    # caller names), so this one call can't be statically checked against
    # those overloads - everything downstream (operation existence, params)
    # is still validated at runtime by botocore itself.
    client = session.client(service, region_name=region)  # type: ignore[call-overload]
    method = getattr(client, xform_name(operation), None)
    if method is None:
        raise OperationDeniedError(f"{service}:{operation} is not a recognized API operation.")

    try:
        response: dict[str, Any] = method(**params)
    except (ClientError, BotoCoreError) as exc:
        response_data = getattr(exc, "response", {}) or {}
        error_data = response_data.get("Error", {}) if isinstance(response_data, dict) else {}
        code = error_data.get("Code", type(exc).__name__)
        message = error_data.get("Message", "Call failed.")
        raise AwsCallError(service, operation, code, message) from exc

    response.pop("ResponseMetadata", None)
    return response
