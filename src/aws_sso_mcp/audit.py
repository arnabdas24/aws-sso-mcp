"""Append-only JSONL audit log for every AWS read call this server makes.

One line per call, written best-effort (a logging failure must never break
a query). Never contains credentials - only the identifying facts about
what was called and how it was authorized.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from .redact import redact_value

_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audited AWS call."""

    timestamp: str
    org: str
    account_id: str
    account_name: str
    role: str | None
    role_source: str | None
    region: str
    service: str
    operation: str
    ok: bool
    error: str | None = None


def new_entry(
    *,
    org: str,
    account_id: str,
    account_name: str,
    role: str | None,
    role_source: str | None,
    region: str,
    service: str,
    operation: str,
    ok: bool,
    error: str | None = None,
) -> AuditEntry:
    """Build one audit entry, stamping the current time."""
    return AuditEntry(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        org=org,
        account_id=account_id,
        account_name=account_name,
        role=role,
        role_source=role_source,
        region=region,
        service=service,
        operation=operation,
        ok=ok,
        error=cast(str, redact_value(error)) if error is not None else None,
    )


def append(log_path: Path, entry: AuditEntry) -> None:
    """Append one entry as a JSON line. Never raises - a broken audit log must not break a query."""
    try:
        line = json.dumps(asdict(entry), sort_keys=True)
        with _lock, log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
