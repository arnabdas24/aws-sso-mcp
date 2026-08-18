"""Tests for aws_sso_mcp.sso.device_auth, using botocore Stubber - never live AWS."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import boto3
from botocore.stub import Stubber

from aws_sso_mcp.config import PortalConfig
from aws_sso_mcp.sso import device_auth


def _portal() -> PortalConfig:
    return PortalConfig(
        name="org1", start_url="https://example.awsapps.com/start", sso_region="us-east-1"
    )


def test_token_status_no_cache(tmp_path: Path) -> None:
    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is False
    assert status.reason == "no cached token"


def test_token_status_valid_cached(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    future = (datetime.now() + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": future}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is True
    assert status.expires_at == future


def test_token_status_expired(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    past = (datetime.now() - timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": past}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is False
    assert status.reason == "expired"


def test_login_success(tmp_path: Path, monkeypatch) -> None:
    portal = _portal()
    client = boto3.client("sso-oidc", region_name="us-east-1")
    stubber = Stubber(client)

    stubber.add_response(
        "register_client",
        {"clientId": "cid", "clientSecret": "csecret"},
        {"clientName": "aws-sso-mcp", "clientType": "public"},
    )
    stubber.add_response(
        "start_device_authorization",
        {
            "deviceCode": "dcode",
            "userCode": "ABCD-EFGH",
            "verificationUri": "https://device.sso.us-east-1.amazonaws.com/",
            "verificationUriComplete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
            "expiresIn": 600,
        },
        {"clientId": "cid", "clientSecret": "csecret", "startUrl": portal.start_url},
    )
    stubber.add_response(
        "create_token",
        {"accessToken": "atoken", "tokenType": "Bearer", "expiresIn": 3600},
        {
            "clientId": "cid",
            "clientSecret": "csecret",
            "grantType": "urn:ietf:params:oauth:grant-type:device_code",
            "deviceCode": "dcode",
        },
    )
    stubber.activate()

    monkeypatch.setattr(device_auth, "_build_sso_oidc_client", lambda p: client)
    monkeypatch.setattr(device_auth.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    status = device_auth.login(tmp_path, portal)
    assert status.valid is True

    cached = json.loads(device_auth.token_cache_path(tmp_path, "org1").read_text())
    assert cached["access_token"] == "atoken"

    # File permissions: owner read/write only.
    mode = device_auth.token_cache_path(tmp_path, "org1").stat().st_mode & 0o777
    assert mode == 0o600

    stubber.deactivate()


def test_get_access_token_no_auto_login_raises(tmp_path: Path) -> None:
    from aws_sso_mcp.errors import NoTokenError

    portal = _portal()
    try:
        device_auth.get_access_token(tmp_path, portal, auto_login=False)
        raise AssertionError("expected NoTokenError")
    except NoTokenError:
        pass
