"""providers/vision.py — referenced-image vision input for the API lane (P-0051 / D-0047).

The API executor passes images the user *explicitly references* in their prompt to
vision-capable models as native image blocks. Referenced-only — not every image in the
workspace — keeps intent explicit and bounds per-run token cost. Plan-CLI providers read
images natively, so this path only applies to the API kinds (anthropic / openai_compatible
/ gemini) whose `ProviderDef.supports_vision` is set.

"Referenced" = an image file that exists in the run workspace whose relative path or
basename appears verbatim in the prompt text. The executor turns each into the SDK-native
shape (Anthropic base64 `image` block, OpenAI `image_url` data URL, Gemini inline bytes).
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Extension → MIME. Kept in sync with the upload-in image set (sessions/uploads.py).
_IMAGE_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Backstops on the per-run vision payload. Referenced-only already bounds this; these cap a
# prompt that references many/large images so request size and token cost stay sane.
_MAX_IMAGES = 8
_MAX_BYTES = 20 * 1024 * 1024  # 20 MB total across all referenced images
# Don't walk unbounded trees looking for candidates.
_MAX_SCANNED_FILES = 2000


@dataclass(frozen=True)
class VisionImage:
    """A referenced workspace image, ready to render into any SDK's vision block."""

    rel_path: str
    mime: str
    data: bytes

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64}"


def referenced_images(
    prompt: str,
    workdir: str | None,
    *,
    max_images: int = _MAX_IMAGES,
    max_bytes: int = _MAX_BYTES,
) -> list[VisionImage]:
    """Images in `workdir` that the `prompt` text explicitly references, capped by count
    and total bytes. Returns [] when nothing is referenced or the workspace is absent —
    the caller then falls back to today's text-only message.
    """
    if not prompt or not workdir or not os.path.isdir(workdir):
        return []

    matched: list[VisionImage] = []
    total = 0
    scanned = 0
    seen: set[str] = set()
    for root, dirs, files in os.walk(workdir):
        # Skip VCS / hidden dirs so we don't surface internal artifacts.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            scanned += 1
            if scanned > _MAX_SCANNED_FILES:
                return matched
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            mime = _IMAGE_EXT_MIME.get(ext)
            if mime is None:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, workdir)
            # Referenced = relpath or basename appears verbatim in the prompt. Match the
            # longer relpath first; fall back to the basename for a bare "chart.png".
            if rel not in prompt and fname not in prompt:
                continue
            if rel in seen:
                continue
            try:
                data = _read_capped(full, max_bytes - total)
            except OSError as exc:
                logger.warning("vision: could not read referenced image %s: %s", rel, exc)
                continue
            if data is None:  # would exceed the byte budget
                continue
            seen.add(rel)
            matched.append(VisionImage(rel_path=rel, mime=mime, data=data))
            total += len(data)
            if len(matched) >= max_images or total >= max_bytes:
                return matched
    return matched


def _read_capped(path: str, remaining: int) -> bytes | None:
    """Read the file only if it fits in `remaining` bytes; else return None."""
    if remaining <= 0:
        return None
    size = os.path.getsize(path)
    if size > remaining:
        return None
    with open(path, "rb") as fh:
        return fh.read()
