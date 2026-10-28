"""Tests for aws_sso_mcp.sso.directory, using botocore Stubber - never live AWS."""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber

from aws_sso_mcp.sso import directory


def test_list_accounts_paginated(monkeypatch) -> None:
    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)

    stubber.add_response(
        "list_accounts",
        {
            "accountList": [{"accountId": "111", "accountName": "acct-a"}],
            "nextToken": "page2",
        },
        {"accessToken": "tok", "maxResults": 100},
    )
    stubber.add_response(
        "list_accounts",
        {"accountList": [{"accountId": "222", "accountName": "acct-b"}]},
        {"accessToken": "tok", "maxResults": 100, "nextToken": "page2"},
    )
    stubber.activate()
    monkeypatch.setattr(directory, "_build_sso_client", lambda region: client)

    accounts = directory.list_accounts("us-east-1", "tok")
    assert [a.account_id for a in accounts] == ["111", "222"]
    assert [a.account_name for a in accounts] == ["acct-a", "acct-b"]
    stubber.deactivate()


def test_list_account_roles(monkeypatch) -> None:
    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "list_account_roles",
        {"roleList": [{"roleName": "DevRole"}, {"roleName": "ReadOnly"}]},
        {"accessToken": "tok", "accountId": "111", "maxResults": 100},
    )
    stubber.activate()
    monkeypatch.setattr(directory, "_build_sso_client", lambda region: client)

    roles = directory.list_account_roles("us-east-1", "tok", "111")
    assert roles == ["DevRole", "ReadOnly"]
    stubber.deactivate()


def test_list_accounts_error_wrapped(monkeypatch) -> None:
    from aws_sso_mcp.errors import AwsCallError

    client = boto3.client("sso", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_client_error("list_accounts", service_error_code="AccessDeniedException")
    stubber.activate()
    monkeypatch.setattr(directory, "_build_sso_client", lambda region: client)

    with pytest.raises(AwsCallError) as excinfo:
        directory.list_accounts("us-east-1", "tok")
    assert excinfo.value.service == "sso"
    assert excinfo.value.operation == "ListAccounts"
    stubber.deactivate()
