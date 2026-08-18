"""Tests for aws_sso_mcp.engine.catalog - the generic read-query engine."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from aws_sso_mcp.engine import catalog
from aws_sso_mcp.errors import AwsCallError, OperationDeniedError, RegionError


class _FakeSession:
    """Stand-in for boto3.Session that hands back a pre-built (stubbed) client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def client(self, service: str, region_name: str) -> Any:
        return self._client


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("DescribeInstances", True),
        ("ListBuckets", True),
        ("GetObject", True),
        ("HeadObject", True),
        ("LookupEvents", True),
        ("Query", True),
        ("Scan", True),
        ("CreateBucket", False),
        ("DeleteObject", False),
        ("StartInstances", False),
        ("StopInstances", False),
        ("PutObject", False),
        ("UpdateFunctionCode", False),
    ],
)
def test_is_read_operation(operation: str, expected: bool) -> None:
    assert catalog.is_read_operation(operation) is expected


def test_requires_secret_gate_always_gated() -> None:
    assert catalog.requires_secret_gate("secretsmanager", "GetSecretValue", {}) is True
    assert catalog.requires_secret_gate("kms", "Decrypt", {}) is True
    assert catalog.requires_secret_gate("s3", "GetObject", {}) is True


def test_requires_secret_gate_conditional_on_param() -> None:
    assert catalog.requires_secret_gate("ssm", "GetParameter", {}) is False
    assert catalog.requires_secret_gate("ssm", "GetParameter", {"WithDecryption": False}) is False
    assert catalog.requires_secret_gate("ssm", "GetParameter", {"WithDecryption": True}) is True


def test_requires_secret_gate_unrelated_operation() -> None:
    assert catalog.requires_secret_gate("ec2", "DescribeInstances", {}) is False


def test_validate_operation_denies_mutating_verb() -> None:
    with pytest.raises(OperationDeniedError, match="not a recognized read-only operation"):
        catalog.validate_operation(
            service="ec2",
            operation="StopInstances",
            params={},
            allow_secret_values=False,
        )


def test_validate_operation_denies_ungated_secret_read() -> None:
    with pytest.raises(OperationDeniedError, match="allow_secret_values"):
        catalog.validate_operation(
            service="secretsmanager",
            operation="GetSecretValue",
            params={},
            allow_secret_values=False,
        )


def test_validate_operation_allows_gated_secret_read_when_enabled() -> None:
    catalog.validate_operation(
        service="secretsmanager",
        operation="GetSecretValue",
        params={},
        allow_secret_values=True,
    )


def test_validate_operation_allows_plain_read() -> None:
    catalog.validate_operation(
        service="ec2", operation="DescribeInstances", params={}, allow_secret_values=False
    )


def test_execute_read_requires_region() -> None:
    with pytest.raises(RegionError):
        catalog.execute_read(
            session=_FakeSession(None),  # type: ignore[arg-type]
            region="",
            service="ec2",
            operation="DescribeInstances",
            params={},
            allow_secret_values=False,
        )


def test_execute_read_denies_mutating_before_any_client_call() -> None:
    with pytest.raises(OperationDeniedError):
        catalog.execute_read(
            session=_FakeSession(None),  # type: ignore[arg-type]
            region="us-east-1",
            service="ec2",
            operation="TerminateInstances",
            params={"InstanceIds": ["i-123"]},
            allow_secret_values=False,
        )


def test_execute_read_success_strips_response_metadata() -> None:
    client = boto3.client("ec2", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-123", "State": {"Name": "stopped"}}]}
            ],
            "ResponseMetadata": {"RequestId": "abc"},
        },
        {"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]},
    )
    stubber.activate()

    response = catalog.execute_read(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        params={"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]},
        allow_secret_values=False,
    )
    stubber.deactivate()

    assert "ResponseMetadata" not in response
    assert response["Reservations"][0]["Instances"][0]["InstanceId"] == "i-123"


def test_execute_read_wraps_client_error_cleanly() -> None:
    client = boto3.client("ec2", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_client_error(
        "describe_instances",
        service_error_code="UnauthorizedOperation",
        service_message="You are not authorized to perform this operation.",
        http_status_code=403,
    )
    stubber.activate()

    with pytest.raises(AwsCallError) as exc_info:
        catalog.execute_read(
            session=_FakeSession(client),  # type: ignore[arg-type]
            region="us-east-1",
            service="ec2",
            operation="DescribeInstances",
            params={},
            allow_secret_values=False,
        )
    stubber.deactivate()

    assert exc_info.value.code == "UnauthorizedOperation"
    assert exc_info.value.service == "ec2"
    assert exc_info.value.operation == "DescribeInstances"


def test_execute_read_unknown_operation_raises_clean_error() -> None:
    client = boto3.client("ec2", region_name="us-east-1")
    with pytest.raises(OperationDeniedError, match="not a recognized API operation"):
        catalog.execute_read(
            session=_FakeSession(client),  # type: ignore[arg-type]
            region="us-east-1",
            service="ec2",
            operation="DescribeTotallyNotARealOperation",
            params={},
            allow_secret_values=False,
        )
