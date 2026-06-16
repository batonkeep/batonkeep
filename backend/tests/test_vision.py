"""test_vision.py — P-0051 / D-0047 referenced-image vision input.

Covers the referenced-only selection rule (basename + relpath match, no false
positives), the count/byte backstops, and that vision-capable provider defs carry
`supports_vision` while plan-CLI / non-vision ones don't.
"""
from __future__ import annotations

import base64

import pytest

from app.providers import vision
from app.providers.registry import get_provider_def

# 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _write(p, data=_PNG):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_matches_referenced_basename(tmp_path):
    _write(tmp_path / "assets" / "chart.png")
    imgs = vision.referenced_images("Please analyze chart.png and summarize", str(tmp_path))
    assert [i.rel_path for i in imgs] == ["assets/chart.png"]
    assert imgs[0].mime == "image/png"
    assert imgs[0].data_url.startswith("data:image/png;base64,")


def test_matches_referenced_relpath(tmp_path):
    _write(tmp_path / "assets" / "a.png")
    imgs = vision.referenced_images("look at assets/a.png", str(tmp_path))
    assert [i.rel_path for i in imgs] == ["assets/a.png"]


def test_unreferenced_image_ignored(tmp_path):
    _write(tmp_path / "assets" / "chart.png")
    assert vision.referenced_images("nothing to see here", str(tmp_path)) == []


def test_non_image_reference_ignored(tmp_path):
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    assert vision.referenced_images("read data.csv", str(tmp_path)) == []


def test_missing_workdir(tmp_path):
    assert vision.referenced_images("chart.png", str(tmp_path / "nope")) == []
    assert vision.referenced_images("chart.png", None) == []


def test_image_count_cap(tmp_path):
    refs = []
    for i in range(5):
        _write(tmp_path / f"img{i}.png")
        refs.append(f"img{i}.png")
    imgs = vision.referenced_images(" ".join(refs), str(tmp_path), max_images=2)
    assert len(imgs) == 2


def test_byte_cap_skips_oversized(tmp_path):
    _write(tmp_path / "big.png", data=_PNG * 100)
    imgs = vision.referenced_images("big.png", str(tmp_path), max_bytes=10)
    assert imgs == []


@pytest.mark.parametrize("name", ["claude-api", "openai-api", "grok-api", "gemini-api"])
def test_vision_providers_flagged(name):
    assert get_provider_def(name).supports_vision is True


@pytest.mark.parametrize("name", ["claude", "grok", "ollama", "open-default"])
def test_non_vision_providers_not_flagged(name):
    assert get_provider_def(name).supports_vision is False
