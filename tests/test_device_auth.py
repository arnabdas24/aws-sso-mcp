"""Tests for aws_sso_mcp.sso.device_auth, using botocore Stubber - never live AWS."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from aws_sso_mcp.config import PortalConfig
from aws_sso_mcp.errors import AwsCallError, NoTokenError
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
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": future}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is True
    assert status.expires_at == future


def test_token_status_expired(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    past = (datetime.now(UTC) - timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": past}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is False
    assert status.reason == "expired"


def test_token_status_expired_memory_falls_back_to_fresher_disk_token(tmp_path: Path) -> None:
    stale = datetime.now(UTC) - timedelta(minutes=1)
    device_auth._put_mem("org1", "old", stale)
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "new", "expires_at": future}), encoding="utf-8")

    assert device_auth.token_status(tmp_path, "org1").valid is True
    assert device_auth.get_access_token(tmp_path, _portal(), auto_login=False) == "new"


def test_token_status_corrupted_cache(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"access_token": "tok"}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")
    assert status.valid is False
    assert status.reason == "cached token corrupted"


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

    # File permissions: owner read/write only. Windows ignores POSIX mode bits.
    if os.name == "posix":
        mode = device_auth.token_cache_path(tmp_path, "org1").stat().st_mode & 0o777
        assert mode == 0o600

    stubber.deactivate()


def test_login_times_out_when_user_never_approves(tmp_path: Path, monkeypatch) -> None:
    """An unapproved sign-in must end with a clear, actionable error - not hang forever."""
    portal = _portal()
    client = boto3.client("sso-oidc", region_name="us-east-1")
    stubber = Stubber(client)

    stubber.add_response("register_client", {"clientId": "cid", "clientSecret": "csecret"})
    # expiresIn=0 makes the device code deadline expire immediately, so the
    # first "still pending" reply is also the last poll.
    stubber.add_response(
        "start_device_authorization",
        {
            "deviceCode": "dcode",
            "userCode": "ABCD-EFGH",
            "verificationUri": "https://device.sso.us-east-1.amazonaws.com/",
            "expiresIn": 0,
        },
    )
    stubber.add_client_error("create_token", service_error_code="AuthorizationPendingException")
    stubber.activate()

    monkeypatch.setattr(device_auth, "_build_sso_oidc_client", lambda p: client)
    monkeypatch.setattr(device_auth.webbrowser, "open", lambda url: True)

    with pytest.raises(AwsCallError) as excinfo:
        device_auth.login(tmp_path, portal)
    assert excinfo.value.code == "LoginTimeout"

    stubber.deactivate()


def test_get_access_token_no_auto_login_raises(tmp_path: Path) -> None:
    portal = _portal()
    try:
        device_auth.get_access_token(tmp_path, portal, auto_login=False)
        raise AssertionError("expected NoTokenError")
    except NoTokenError:
        pass


# ------------------------------------------------ expiry margin / atomic write


def test_token_expiring_within_the_margin_is_not_usable(tmp_path: Path) -> None:
    """A technically-valid token that would lapse mid-fan-out is refused up front."""
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    soon = (datetime.now(UTC) + timedelta(minutes=2)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": soon}), encoding="utf-8")

    status = device_auth.token_status(tmp_path, "org1")

    assert status.valid is False
    assert status.reason == "expiring within 5 minutes"
    with pytest.raises(NoTokenError):
        device_auth.get_access_token(tmp_path, _portal(), auto_login=False)


def test_token_beyond_the_margin_is_usable(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    path.parent.mkdir(parents=True)
    later = (datetime.now(UTC) + timedelta(minutes=30)).strftime(device_auth._TIME_FORMAT)
    path.write_text(json.dumps({"access_token": "tok", "expires_at": later}), encoding="utf-8")

    assert device_auth.token_status(tmp_path, "org1").valid is True
    assert device_auth.get_access_token(tmp_path, _portal(), auto_login=False) == "tok"


def test_write_cached_token_overwrites_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = device_auth.token_cache_path(tmp_path, "org1")
    expires = datetime.now(UTC) + timedelta(hours=1)

    device_auth._write_cached_token(path, "tok-1", expires)
    device_auth._write_cached_token(path, "tok-2", expires)

    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "tok-2"
    assert list(path.parent.glob("*.tmp")) == []


def test_failed_token_write_leaves_the_previous_file_intact(tmp_path: Path, monkeypatch) -> None:
    """The point of the temp-file + rename: a failed write never destroys a good token."""
    path = device_auth.token_cache_path(tmp_path, "org1")
    expires = datetime.now(UTC) + timedelta(hours=1)
    device_auth._write_cached_token(path, "good", expires)

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(device_auth.os, "replace", explode)

    with pytest.raises(OSError, match="no space left on device"):
        device_auth._write_cached_token(path, "clobbered", expires)

    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "good"
    assert list(path.parent.glob("*.tmp")) == []
