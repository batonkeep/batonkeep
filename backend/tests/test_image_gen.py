"""
test_image_gen.py — capability-gated image generation (P-0046 slice 6 / P-0037).

Locks: the tool writes the decoded image into the workspace as an artifact, meters
the per-asset cost into the run's accumulator, refuses path traversal, and degrades
when no image-capable provider is configured. Also locks the executor-side gating —
`image_generate` is offered only when the run carries an `image_gen` config.
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.tools import image_gen

# A 1x1 transparent PNG, base64 — what the provider would hand back.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _fake_client(b64: str | None = _PNG_B64):
    """An AsyncOpenAI stand-in whose images.generate returns one b64 image."""
    datum = type("D", (), {"b64_json": b64})()
    resp = type("R", (), {"data": [datum] if b64 is not None else []})()
    client = type("C", (), {})()
    client.images = type("I", (), {"generate": AsyncMock(return_value=resp)})()
    return client


def _config(tmp_path, **over):
    cfg = {
        "api_key": "k", "base_url": "https://api.x.ai/v1",
        "model": "grok-2-image-1212", "cost_per_image": 0.07,
        "cost_accumulator": [],
    }
    cfg.update(over)
    return cfg


@pytest.mark.asyncio
async def test_writes_image_and_meters_cost(tmp_path):
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client()):
        out = await image_gen.run("a red square", "logo.png", workdir=str(tmp_path), config=cfg)
    saved = tmp_path / "logo.png"
    assert saved.exists()
    assert saved.read_bytes() == base64.b64decode(_PNG_B64)
    assert "logo.png" in out
    assert cfg["cost_accumulator"] == [0.07]  # per-asset cost metered for the budget gate


@pytest.mark.asyncio
async def test_defaults_filename_into_assets(tmp_path):
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client()):
        out = await image_gen.run("x", workdir=str(tmp_path), config=cfg)
    assert (tmp_path / "assets").is_dir()
    assert "assets/" in out


@pytest.mark.asyncio
async def test_appends_png_extension_when_missing(tmp_path):
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client()):
        await image_gen.run("x", "diagram", workdir=str(tmp_path), config=cfg)
    assert (tmp_path / "diagram.png").exists()


@pytest.mark.asyncio
async def test_refuses_path_traversal(tmp_path):
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client()):
        out = await image_gen.run("x", "../escape.png", workdir=str(tmp_path), config=cfg)
    assert "traversal" in out
    assert cfg["cost_accumulator"] == []  # nothing generated → no cost


@pytest.mark.asyncio
async def test_no_config_degrades_gracefully(tmp_path):
    out = await image_gen.run("x", workdir=str(tmp_path), config=None)
    assert "error" in out.lower()


@pytest.mark.asyncio
async def test_empty_provider_data_is_an_error(tmp_path):
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client(b64=None)):
        out = await image_gen.run("x", workdir=str(tmp_path), config=cfg)
    assert "no image data" in out


def test_executor_offers_image_gen_only_when_configured():
    from app.providers.model_executor import _active_tool_schemas

    base = {s["name"] for s in _active_tool_schemas({})}
    assert "image_generate" not in base
    gated = {s["name"] for s in _active_tool_schemas({"image_gen": {"api_key": "k"}})}
    assert "image_generate" in gated


def test_grok_provider_declares_image_capability():
    from app.providers.registry import get_provider_def

    grok = get_provider_def("grok-api")
    assert grok.supports_image_gen is True
    assert grok.image_model
    assert grok.image_cost_per_image > 0
