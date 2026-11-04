"""Tests for aws_sso_mcp.engine.catalog - the generic read-query engine."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from aws_sso_mcp.engine import catalog
from aws_sso_mcp.errors import AwsCallError, RegionError


class _FakeSession:
    """Stand-in for boto3.Session that hands back a pre-built (stubbed) client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def client(self, service: str, region_name: str, config: Any | None = None) -> Any:
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


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("DeleteBucket", True),
        ("TerminateInstances", True),
        ("RemoveTagsFromResource", True),
        ("DetachVolume", True),
        ("DisassociateAddress", True),
        ("DeregisterImage", True),
        ("RevokeSecurityGroupIngress", True),
        ("ReleaseAddress", True),
        ("BatchDeleteImage", True),
        ("ScheduleKeyDeletion", True),
        ("DescribeInstances", False),
        ("ListBuckets", False),
        ("CreateBucket", False),
        ("PutObject", False),
        ("StopInstances", False),
        ("BatchGetItem", False),
    ],
)
def test_is_destructive_operation(operation: str, expected: bool) -> None:
    assert catalog.is_destructive_operation(operation) is expected


def test_execute_call_requires_region() -> None:
    with pytest.raises(RegionError):
        catalog.execute_call(
            session=_FakeSession(None),  # type: ignore[arg-type]
            region="",
            service="ec2",
            operation="DescribeInstances",
            params={},
        )


def test_execute_call_success_strips_response_metadata() -> None:
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

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        params={"Filters": [{"Name": "instance-state-name", "Values": ["stopped"]}]},
    )
    stubber.deactivate()

    assert "ResponseMetadata" not in response
    assert response["Reservations"][0]["Instances"][0]["InstanceId"] == "i-123"


def test_execute_call_wraps_client_error_cleanly() -> None:
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
        catalog.execute_call(
            session=_FakeSession(client),  # type: ignore[arg-type]
            region="us-east-1",
            service="ec2",
            operation="DescribeInstances",
            params={},
        )
    stubber.deactivate()

    assert exc_info.value.code == "UnauthorizedOperation"
    assert exc_info.value.service == "ec2"
    assert exc_info.value.operation == "DescribeInstances"


def test_execute_call_unknown_operation_raises_clean_error() -> None:
    client = boto3.client("ec2", region_name="us-east-1")
    with pytest.raises(AwsCallError, match="has no operation named"):
        catalog.execute_call(
            session=_FakeSession(client),  # type: ignore[arg-type]
            region="us-east-1",
            service="ec2",
            operation="DescribeTotallyNotARealOperation",
            params={},
        )


def test_execute_call_dispatches_mutating_operation() -> None:
    """IAM is the sole boundary - a destructive op is dispatched, not refused locally."""
    client = boto3.client("ec2", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "terminate_instances",
        {"TerminatingInstances": [{"InstanceId": "i-123"}]},
        {"InstanceIds": ["i-123"]},
    )
    stubber.activate()

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="ec2",
        operation="TerminateInstances",
        params={"InstanceIds": ["i-123"]},
    )
    stubber.deactivate()

    assert response["TerminatingInstances"][0]["InstanceId"] == "i-123"


def test_execute_call_dispatches_secret_value_read() -> None:
    """Secret reads are no longer gated locally; AWS decides via IAM."""
    client = boto3.client("secretsmanager", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "get_secret_value",
        {"Name": "prod/db", "SecretString": "s3kr3t"},
        {"SecretId": "prod/db"},
    )
    stubber.activate()

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="secretsmanager",
        operation="GetSecretValue",
        params={"SecretId": "prod/db"},
    )
    stubber.deactivate()

    assert response["Name"] == "prod/db"


def _stub_two_pages(stubber: Stubber) -> None:
    """Queue a two-page DescribeInstances response linked by NextToken."""
    stubber.add_response(
        "describe_instances",
        {
            "Reservations": [{"Instances": [{"InstanceId": "i-1"}]}],
            "NextToken": "page2",
        },
        {},
    )
    stubber.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [{"InstanceId": "i-2"}]}]},
        {"NextToken": "page2"},
    )


def test_execute_call_merges_all_pages() -> None:
    """Every page is fetched, so a caller never sees a silently truncated answer."""
    client = boto3.client("ec2", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_two_pages(stubber)
    stubber.activate()

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        params={},
    )
    stubber.deactivate()

    ids = [i["InstanceId"] for r in response["Reservations"] for i in r["Instances"]]
    assert ids == ["i-1", "i-2"]
    assert catalog.TRUNCATED_KEY not in response


def test_execute_call_marks_result_truncated_at_max_items() -> None:
    """Hitting the cap must be announced, never silently hidden from the model."""
    client = boto3.client("ec2", region_name="us-east-1")
    stubber = Stubber(client)
    _stub_two_pages(stubber)
    stubber.activate()

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="ec2",
        operation="DescribeInstances",
        params={},
        max_items=1,
    )
    stubber.deactivate()

    assert "more results exist" in response[catalog.TRUNCATED_KEY]
    # The continuation token itself is an implementation detail, not an answer.
    assert "NextToken" not in response


def test_execute_call_uses_single_shot_for_unpaginated_operation() -> None:
    """Operations botocore cannot paginate still work through the same entry point."""
    client = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "get_caller_identity",
        {"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:role/Example"},
        {},
    )
    stubber.activate()

    response = catalog.execute_call(
        session=_FakeSession(client),  # type: ignore[arg-type]
        region="us-east-1",
        service="sts",
        operation="GetCallerIdentity",
        params={},
    )
    stubber.deactivate()

    assert response["Account"] == "111111111111"
