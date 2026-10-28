"""Command-line surface of the ``aws-sso-mcp`` console script."""

from __future__ import annotations

import pytest

from aws_sso_mcp import __version__, server


def test_version_flag_prints_the_package_version_and_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        server.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"aws-sso-mcp {__version__}"


def test_version_flag_never_starts_the_stdio_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(server.app, "run", lambda *a, **k: started.append(True))

    with pytest.raises(SystemExit):
        server.main(["--version"])

    assert started == []


def test_unknown_arguments_are_rejected_instead_of_silently_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(server.app, "run", lambda *a, **k: started.append(True))

    with pytest.raises(SystemExit) as exit_info:
        server.main(["--bogus"])

    assert exit_info.value.code == 2
    assert started == []


def test_no_arguments_starts_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(server.app, "run", lambda *a, **k: started.append(True))

    server.main([])

    assert started == [True]
