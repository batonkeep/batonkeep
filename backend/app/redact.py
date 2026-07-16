"""
redact.py — secrets sanitizer at the durable-record boundary (D-0058 A6).

Telemetry is content-free by construction (D-0022), but the durable event log
and structured logs are not: an agent that echoes an env var, an
`Authorization:` header, or a pasted key into stdout/stderr lands verbatim in
`RunEvent` rows and JSON log lines. This module is the single redaction wall
applied where those records are *written*:

  • `logging_config.JsonFormatter` — every emitted JSON log line;
  • `orchestrator._emit_event` — every persisted `RunEvent` (message + data);
  • turn/run error strings before they are stored.

Deliberately NOT applied to the deliverable itself (`Run` results,
`SessionTurn.response`, workspace files) — that is the user's own content in
their own store, and rewriting it would corrupt legitimate output (e.g. a task
that generates a config template). The live WS stream is likewise untouched;
the wall is the durable record, not the wire.

§S0 evidence capture (slice 3) MUST route bundle text through `redact_text` /
`redact_json` at write time — evidence is append-only, so a leaked secret
there is permanent (§5.9 audit integrity).

Precision stance: favor recall on well-known credential *shapes* (prefixed
keys, tokens with issuer-fixed formats, PEM blocks) and keyed assignments
(NAME=value where NAME says secret); guard against false positives by
requiring values to be non-trivial (length + at least one letter) so token
counts and IDs survive.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# ── Value shapes: secrets wherever they appear ────────────────────────────────
_VALUE_PATTERNS: list[re.Pattern[str]] = [
    # PEM private-key blocks (before the generic ones so the whole block goes)
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    # Provider API keys: OpenAI/Anthropic `sk-…`, xAI `xai-…`, Google `AIza…`
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    # GitHub tokens (classic + fine-grained)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # AWS access-key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Slack tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # JWTs (three base64url segments)
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # Fernet tokens — the shape of our own encrypted credential store (P4);
    # a leaked ciphertext pairs with APP_SECRET to become the plaintext.
    re.compile(r"\bgAAAA[A-Za-z0-9_=-]{20,}"),
]

# ── Keyed forms: keep the name, drop the value ────────────────────────────────
# Value must be ≥8 chars and contain a letter, so `MAX_TOKENS=4096` or
# `tokens=123456` are never mangled.
_KEYED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)\b(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s'\"]{4,}"
    ),
    # Never consumes quote characters, so redacting inside an already-serialized
    # JSON line (the formatter path) cannot eat a string's closing quote.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_KEY"
        r"|PRIVATE_KEY|CREDENTIAL)S?[A-Z0-9_]*\s*[=:]\s*['\"]?)"
        r"(?=[^\s'\"]*[A-Za-z])[^\s'\"]{8,}"
    ),
]

# Dict keys whose entire string value is dropped in redact_json.
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:secret|token|password|passwd|api_?key|apikey|access_key"
    r"|private_key|credential)s?(?:$|_)"
)


def redact_text(text: str) -> str:
    """Redact known secret shapes in free text. Cheap no-op on clean text."""
    if not text:
        return text
    for pat in _KEYED_PATTERNS:
        text = pat.sub(rf"\g<1>{REDACTED}", text)
    for pat in _VALUE_PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


def redact_json(obj: Any) -> Any:
    """Recursively redact a JSON-shaped structure (dict/list/str scalars).

    String values under a sensitive-looking key are dropped wholesale (the key
    name is the signal — no shape guessing needed); all other strings pass
    through `redact_text`. Non-string scalars are returned unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: (
                REDACTED
                if isinstance(v, str) and v and _SENSITIVE_KEY_RE.search(str(k))
                else redact_json(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list | tuple):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
