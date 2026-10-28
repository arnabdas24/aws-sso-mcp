"""Append-only JSONL audit log for every AWS call this server makes.

One line per call, written best-effort via a background daemon thread that
drains a bounded queue - a fan-out across hundreds of accounts finishes
without ever waiting on file I/O in the request path. A logging failure
must never break a query.

Because the writer threads are daemons, :func:`flush_all` is registered as an
:mod:`atexit` hook to drain them on shutdown; without it the entries still
queued when the process exits - typically the ones from the call that just
ran - would be silently lost.

Never contains credentials - only the identifying facts about what was
called and how it was authorized.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import queue
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .logging_setup import get_logger
from .redact import redact_value

logger = get_logger(__name__)


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
    write: bool
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
    write: bool = False,
    error: str | None = None,
) -> AuditEntry:
    """Build one audit entry, stamping the current UTC time."""
    return AuditEntry(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        org=org,
        account_id=account_id,
        account_name=account_name,
        role=role,
        role_source=role_source,
        region=region,
        service=service,
        operation=operation,
        write=write,
        ok=ok,
        error=cast(str, redact_value(error)) if error is not None else None,
    )


class _AuditWriter:
    """Background daemon draining audit entries to disk in batches.

    One writer per log path. Submitting is lock-free after startup (queue
    is thread-safe) and blocks only if the queue fills to its bound - which
    only matters for pathological runaway loops, not normal fan-outs.
    """

    _MAX_QUEUE = 10_000
    _FLUSH_INTERVAL_SECONDS = 0.25

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._queue: queue.Queue[AuditEntry | None] = queue.Queue(maxsize=self._MAX_QUEUE)
        self._thread = threading.Thread(
            target=self._run, name=f"aws-sso-mcp-audit[{log_path.name}]", daemon=True
        )
        self._started = False
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._thread.start()
            self._started = True

    def submit(self, entry: AuditEntry) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            # Queue is bounded to keep memory sane. Dropping a line is
            # strictly preferable to blocking a query.
            logger.warning("Audit queue full; dropping one entry.")

    def close(self, timeout: float = 2.0) -> None:
        """Drain anything still queued and stop the writer thread.

        The writer is a daemon thread, so without this the interpreter exits
        without waiting for it and whatever was still queued is lost. For an
        audit log that is the one failure mode that actually matters, since
        the entries most likely to be in flight are the ones from the call
        that just ran.

        Args:
            timeout: How long to wait for the drain before giving up. Bounded
                so a wedged writer can never stop the process from exiting.
        """
        if not self._started:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            batch: list[AuditEntry] = []
            first = self._queue.get()
            if first is None:
                return
            batch.append(first)
            # Drain anything already waiting, up to a small cap.
            for _ in range(255):
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    self._flush(batch)
                    return
                batch.append(item)
            self._flush(batch)

    def _flush(self, batch: list[AuditEntry]) -> None:
        if not batch:
            return
        try:
            lines = [json.dumps(asdict(e), sort_keys=True) + "\n" for e in batch]
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.writelines(lines)
        except OSError as exc:
            logger.warning("Failed to write %d audit entries: %s", len(batch), exc)


_WRITERS: dict[Path, _AuditWriter] = {}
_WRITERS_LOCK = threading.Lock()


def _writer_for(log_path: Path) -> _AuditWriter:
    with _WRITERS_LOCK:
        writer = _WRITERS.get(log_path)
        if writer is None:
            writer = _AuditWriter(log_path)
            _WRITERS[log_path] = writer
        return writer


def append(log_path: Path, entry: AuditEntry) -> None:
    """Enqueue one audit entry for background writing. Never raises.

    Args:
        log_path: The JSONL file to append to. One background writer is
            created per distinct path.
        entry: The entry to record.
    """
    try:
        _writer_for(log_path).submit(entry)
    except Exception as exc:  # noqa: BLE001 - audit must not break a query
        logger.warning("Audit submit failed: %s", exc)


def flush_all(timeout: float = 2.0) -> None:
    """Drain and stop every audit writer. Never raises.

    Registered as an :mod:`atexit` hook below, so entries queued when the
    server shuts down still reach disk instead of dying with the daemon
    threads that were about to write them.

    Args:
        timeout: Per-writer bound on how long to wait for the drain.
    """
    with _WRITERS_LOCK:
        writers = list(_WRITERS.values())
        _WRITERS.clear()
    for writer in writers:
        try:
            writer.close(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning("Audit flush failed: %s", exc)


atexit.register(flush_all)
