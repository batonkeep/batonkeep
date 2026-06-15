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


def _fake_client(b64: str | None = _PNG_B64, *, url: str | None = None, usage=None):
    """An AsyncOpenAI stand-in whose images.generate returns one image (b64 or url)
    and an optional usage block. Records the kwargs the tool passed to generate()."""
    datum = type("D", (), {"b64_json": b64, "url": url})()
    has_data = b64 is not None or url is not None
    resp = type("R", (), {"data": [datum] if has_data else [], "usage": usage})()
    gen = AsyncMock(return_value=resp)
    client = type("C", (), {})()
    client.images = type("I", (), {"generate": gen})()
    client._gen = gen  # exposed so tests can assert the call kwargs
    return client


def _config(tmp_path, **over):
    cfg = {
        "api_key": "k", "base_url": "https://api.x.ai/v1",
        "model": "grok-imagine-image-quality", "cost_per_image": 0.05,
        "cost_per_mtok": 0.0, "response_format": "b64_json",
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
    assert cfg["cost_accumulator"] == [0.05]  # per-asset cost metered for the budget gate


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


@pytest.mark.asyncio
async def test_omits_response_format_for_openai_gpt_image(tmp_path):
    # gpt-image-* 400s on response_format → config carries None → param omitted.
    cfg = _config(tmp_path, response_format=None, model="gpt-image-1-mini")
    fake = _fake_client()
    with patch("openai.AsyncOpenAI", return_value=fake):
        await image_gen.run("x", workdir=str(tmp_path), config=cfg)
    assert "response_format" not in fake._gen.call_args.kwargs


@pytest.mark.asyncio
async def test_token_billed_cost_from_usage(tmp_path):
    # OpenAI gpt-image bills per-token: 1000 output tokens × $8/Mtok = $0.008.
    usage = type("U", (), {"output_tokens": 1000})()
    cfg = _config(tmp_path, response_format=None, cost_per_mtok=8.0,
                  cost_per_image=0.01, model="gpt-image-1-mini")
    with patch("openai.AsyncOpenAI", return_value=_fake_client(usage=usage)):
        await image_gen.run("x", workdir=str(tmp_path), config=cfg)
    assert cfg["cost_accumulator"] == [pytest.approx(0.008)]


@pytest.mark.asyncio
async def test_falls_back_to_flat_cost_without_usage(tmp_path):
    cfg = _config(tmp_path, cost_per_mtok=8.0, cost_per_image=0.05)
    with patch("openai.AsyncOpenAI", return_value=_fake_client(usage=None)):
        await image_gen.run("x", workdir=str(tmp_path), config=cfg)
    assert cfg["cost_accumulator"] == [0.05]


@pytest.mark.asyncio
async def test_fetches_image_from_url(tmp_path, monkeypatch):
    # Some endpoints return a hosted url instead of b64; the tool fetches the bytes.
    png = base64.b64decode(_PNG_B64)

    class _Resp:
        content = png
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    cfg = _config(tmp_path)
    with patch("openai.AsyncOpenAI", return_value=_fake_client(b64=None, url="http://img/x.png")):
        await image_gen.run("x", "u.png", workdir=str(tmp_path), config=cfg)
    assert (tmp_path / "u.png").read_bytes() == png


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
    assert grok.image_response_format == "b64_json"


def test_openai_provider_declares_image_capability():
    from app.providers.registry import get_provider_def

    oai = get_provider_def("openai-api")
    assert oai.supports_image_gen is True
    assert oai.image_model == "gpt-image-1-mini"
    assert oai.image_cost_per_mtok > 0
    assert oai.image_response_format is None  # gpt-image rejects the param
