"""
logging_config.py — structured JSON logging + correlation IDs (D-0021).

The control-plane inflection widened the surface (tasks, build sessions, web-TTY,
PTY, privilege-split spawns); plain stdout logs without correlation made incidents
hard to trace. This module gives:

  • a JSON formatter (one object per line — greppable + ingestible);
  • contextvar-based correlation IDs (request_id / owner_id / run_id / session_id)
    injected into every record automatically, so a run's or request's logs can be
    followed end to end;
  • `configure_logging()` wiring it onto the root logger.

It is also the shared observability spine the opt-in product telemetry (P-0024)
builds on. Correlation IDs ride asyncio contextvars, so each request task and each
fire-and-forget run task (orchestrator) carries its own isolated set.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.redact import redact_text

# ── Correlation contextvars ───────────────────────────────────────────────────
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
owner_id_var: ContextVar[str | None] = ContextVar("owner_id", default=None)
run_id_var: ContextVar[int | None] = ContextVar("run_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)

_CORRELATION = (
    ("request_id", request_id_var),
    ("owner_id", owner_id_var),
    ("run_id", run_id_var),
    ("session_id", session_id_var),
)

# Standard LogRecord attributes we should not re-emit as "extra" fields.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"taskName", "asctime", "message"}


class _CorrelationFilter(logging.Filter):
    """Attach the current correlation ids to every record (None ⇒ omitted)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CORRELATION:
            setattr(record, name, var.get())
        return True


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": _dt.datetime.fromtimestamp(record.created, _dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name, _ in _CORRELATION:
            val = getattr(record, name, None)
            if val is not None:
                payload[name] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Any structured extras passed via logger.x(..., extra={...}).
        # Correlation names are handled above (the filter sets them on the record,
        # possibly as None) — skip them here so unset ones stay omitted.
        skip = _RESERVED | {name for name, _ in _CORRELATION}
        for key, val in record.__dict__.items():
            if key not in skip and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = repr(val)
        # Secrets wall (D-0058 A6): one pass over the serialized line catches
        # message text, extras, and exception traces alike.
        return redact_text(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler + correlation filter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_CorrelationFilter())
    # Replace any existing handlers (e.g. a prior basicConfig) so output stays JSON-only.
    root.handlers = [handler]


# ── Binding helpers ───────────────────────────────────────────────────────────
@contextmanager
def bind_run(run_id: int, owner_id: str | None = None) -> Iterator[None]:
    """Bind run (and owner) correlation for the duration of a run's execution."""
    tok_r = run_id_var.set(run_id)
    tok_o = owner_id_var.set(owner_id) if owner_id is not None else None
    try:
        yield
    finally:
        run_id_var.reset(tok_r)
        if tok_o is not None:
            owner_id_var.reset(tok_o)


@contextmanager
def bind_session(session_id: str, owner_id: str | None = None) -> Iterator[None]:
    """Bind session (and owner) correlation for the duration of a turn."""
    tok_s = session_id_var.set(session_id)
    tok_o = owner_id_var.set(owner_id) if owner_id is not None else None
    try:
        yield
    finally:
        session_id_var.reset(tok_s)
        if tok_o is not None:
            owner_id_var.reset(tok_o)
