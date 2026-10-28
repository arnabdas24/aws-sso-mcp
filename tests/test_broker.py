"""Tests for aws_sso_mcp.sso.broker - in-memory credential caching, never persisted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from aws_sso_mcp.sso.broker import CredentialBroker


def _epoch_millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_get_session_mints_and_caches(monkeypatch) -> None:
    broker = CredentialBroker()
    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)
    expiration = _epoch_millis(datetime.now(UTC) + timedelta(hours=1))
    stubber.add_response(
        "get_role_credentials",
        {
            "roleCredentials": {
                "accessKeyId": "AKIAFAKE",
                "secretAccessKey": "secret",
                "sessionToken": "sessiontok",
                "expiration": expiration,
            }
        },
        {"accessToken": "tok", "roleName": "DevRole", "accountId": "111"},
    )
    stubber.activate()
    monkeypatch.setattr("aws_sso_mcp.sso.broker.build_client", lambda service, region, **_: client)

    session1 = broker.get_session(
        org="org1", region="us-east-1", account_id="111", role="DevRole", access_token="tok"
    )
    session2 = broker.get_session(
        org="org1", region="us-east-1", account_id="111", role="DevRole", access_token="tok"
    )
    assert session1 is session2  # cached, only one get_role_credentials call
    stubber.deactivate()


def test_get_session_error_wrapped(monkeypatch) -> None:
    from aws_sso_mcp.errors import AwsCallError

    broker = CredentialBroker()
    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_client_error("get_role_credentials", service_error_code="AccessDeniedException")
    stubber.activate()
    monkeypatch.setattr("aws_sso_mcp.sso.broker.build_client", lambda service, region, **_: client)

    with pytest.raises(AwsCallError) as excinfo:
        broker.get_session(
            org="org1", region="us-east-1", account_id="111", role="DevRole", access_token="tok"
        )
    assert excinfo.value.service == "sso"
    assert excinfo.value.operation == "GetRoleCredentials"
    stubber.deactivate()


def test_invalidate_forces_remint(monkeypatch) -> None:
    broker = CredentialBroker()
    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)
    expiration = _epoch_millis(datetime.now(UTC) + timedelta(hours=1))
    for _ in range(2):
        stubber.add_response(
            "get_role_credentials",
            {
                "roleCredentials": {
                    "accessKeyId": "AKIAFAKE",
                    "secretAccessKey": "secret",
                    "sessionToken": "sessiontok",
                    "expiration": expiration,
                }
            },
            {"accessToken": "tok", "roleName": "DevRole", "accountId": "111"},
        )
    stubber.activate()
    monkeypatch.setattr("aws_sso_mcp.sso.broker.build_client", lambda service, region, **_: client)

    broker.get_session(
        org="org1", region="us-east-1", account_id="111", role="DevRole", access_token="tok"
    )
    broker.invalidate(org="org1", account_id="111", role="DevRole")
    broker.get_session(
        org="org1", region="us-east-1", account_id="111", role="DevRole", access_token="tok"
    )
    stubber.assert_no_pending_responses()
    stubber.deactivate()
