"""
tests/test_custom_providers.py — D-0026 custom-provider CRUD + registry injection.

Tests:
  1. Validation: id format, blank base_url/model, conflict with built-in names.
  2. CRUD: create → list → update → delete (in-memory, using a tmp file).
  3. Duplicate id rejected.
  4. Registry injection: ProviderDef is reachable via get_provider_def() after create.
  5. Local flag: local=True custom provider is in local_candidate_ids().
  6. API routes: GET, POST 201, PUT, DELETE 204 via TestClient.
  7. API 404 on unknown custom provider.
  8. Built-in name conflict via API (422).
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_custom_store(tmp_path, monkeypatch):
    """Redirect the custom-providers JSON store to a temp file per test."""
    store = str(tmp_path / "custom-providers.json")
    monkeypatch.setenv("CUSTOM_PROVIDERS_PATH", store)
    import app.custom_providers as cp_mod
    monkeypatch.setattr(cp_mod, "_CUSTOM_PROVIDERS_PATH", store)
    monkeypatch.setattr(cp_mod, "_BUILTIN_NAMES", frozenset())  # will re-lazy on access
    yield store
    # Re-inject the now-empty registry so later tests see a clean state.
    cp_mod._inject_into_registry([])


@pytest.fixture
def cp_mod():
    import app.custom_providers as m
    return m


# ── 1. Validation ─────────────────────────────────────────────────────────────

def test_invalid_id_characters(cp_mod):
    with pytest.raises(cp_mod.CustomProviderError, match="id must be"):
        cp_mod.create_custom_provider(
            cp_id="My Provider!", label="x", base_url="http://localhost/v1",
            default_model="m",
        )


def test_id_cannot_start_with_hyphen(cp_mod):
    with pytest.raises(cp_mod.CustomProviderError, match="id must be"):
        cp_mod.create_custom_provider(
            cp_id="-bad", label="x", base_url="http://localhost/v1",
            default_model="m",
        )


def test_blank_base_url_rejected(cp_mod):
    with pytest.raises(cp_mod.CustomProviderError, match="base_url"):
        cp_mod.create_custom_provider(
            cp_id="my-ollama", label="x", base_url="", default_model="m",
        )


def test_blank_model_rejected(cp_mod):
    with pytest.raises(cp_mod.CustomProviderError, match="default_model"):
        cp_mod.create_custom_provider(
            cp_id="my-ollama", label="x", base_url="http://localhost:11434/v1",
            default_model="",
        )


def test_invalid_auth_type_rejected(cp_mod):
    with pytest.raises(cp_mod.CustomProviderError, match="auth_type"):
        cp_mod.create_custom_provider(
            cp_id="my-ollama", label="x", base_url="http://localhost:11434/v1",
            default_model="gemma4:12b", auth_type="magic",
        )


def test_conflict_with_builtin_name_rejected(cp_mod, monkeypatch):
    monkeypatch.setattr(cp_mod, "_BUILTIN_NAMES", frozenset({"claude", "mock", "ollama"}))
    with pytest.raises(cp_mod.CustomProviderError, match="conflicts with a built-in"):
        cp_mod.create_custom_provider(
            cp_id="claude", label="x", base_url="http://x/v1", default_model="m",
        )


# ── 2. CRUD round-trip ────────────────────────────────────────────────────────

def test_create_list_update_delete(cp_mod):
    cp = cp_mod.create_custom_provider(
        cp_id="my-ollama", label="My Ollama", base_url="http://localhost:11434/v1",
        default_model="gemma4:12b", local=True,
    )
    assert cp.id == "my-ollama"
    assert cp.local is True

    providers = cp_mod.list_all_custom_providers()
    assert len(providers) == 1
    assert providers[0].id == "my-ollama"

    updated = cp_mod.update_custom_provider("my-ollama", label="Ollama Local", local=False)
    assert updated.label == "Ollama Local"
    assert updated.local is False

    deleted = cp_mod.delete_custom_provider("my-ollama")
    assert deleted is True
    assert cp_mod.list_all_custom_providers() == []


# ── 3. Duplicate id ───────────────────────────────────────────────────────────

def test_duplicate_id_rejected(cp_mod):
    cp_mod.create_custom_provider(
        cp_id="my-lm-studio", label="LM Studio", base_url="http://localhost:1234/v1",
        default_model="llama3",
    )
    with pytest.raises(cp_mod.CustomProviderError, match="already exists"):
        cp_mod.create_custom_provider(
            cp_id="my-lm-studio", label="Dup", base_url="http://x/v1", default_model="m",
        )


# ── 4. Registry injection ─────────────────────────────────────────────────────

def test_registry_injection(cp_mod):
    from app.providers.registry import get_provider_def

    cp_mod.create_custom_provider(
        cp_id="my-local-llm", label="Local LLM", base_url="http://localhost:8080/v1",
        default_model="phi4", local=True,
    )
    pdef = get_provider_def("my-local-llm")
    assert pdef is not None
    assert pdef.kind == "openai_compatible"
    assert pdef.local is True
    assert pdef.base_url == "http://localhost:8080/v1"
    assert pdef.model == "phi4"


# ── 5. Local flag → local_candidate_ids ──────────────────────────────────────

def test_local_custom_provider_in_candidate_ids(cp_mod):
    from app.providers.registry import local_candidate_ids

    cp_mod.create_custom_provider(
        cp_id="my-sovereign", label="Sovereign", base_url="http://localhost:11434/v1",
        default_model="llama3", local=True,
    )
    ids = local_candidate_ids()
    assert "my-sovereign" in ids


def test_non_local_custom_provider_not_in_candidate_ids(cp_mod):
    from app.providers.registry import local_candidate_ids

    cp_mod.create_custom_provider(
        cp_id="my-remote", label="Remote", base_url="https://api.example.com/v1",
        default_model="gpt-x", local=False,
    )
    ids = local_candidate_ids()
    assert "my-remote" not in ids


# ── 6. API routes ─────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    import app.main as main
    return TestClient(main.app)


def test_api_list_empty(client):
    r = client.get("/api/custom-providers")
    assert r.status_code == 200
    assert r.json() == []


def test_api_create_and_list(client):
    payload = {
        "id": "test-ollama",
        "label": "Test Ollama",
        "base_url": "http://localhost:11434/v1",
        "default_model": "gemma4:12b",
        "auth_type": "none",
        "local": True,
    }
    r = client.post("/api/custom-providers", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "test-ollama"
    assert body["local"] is True

    r = client.get("/api/custom-providers")
    assert r.status_code == 200
    assert any(p["id"] == "test-ollama" for p in r.json())


def test_api_update(client):
    client.post("/api/custom-providers", json={
        "id": "upd-test", "label": "Old", "base_url": "http://localhost/v1",
        "default_model": "m1",
    })
    r = client.put("/api/custom-providers/upd-test", json={"label": "New Label"})
    assert r.status_code == 200
    assert r.json()["label"] == "New Label"


def test_api_delete(client):
    client.post("/api/custom-providers", json={
        "id": "del-test", "label": "Del", "base_url": "http://localhost/v1",
        "default_model": "m1",
    })
    r = client.delete("/api/custom-providers/del-test")
    assert r.status_code == 204
    assert all(p["id"] != "del-test" for p in client.get("/api/custom-providers").json())


# ── 7. 404 on unknown ────────────────────────────────────────────────────────

def test_api_update_unknown_404(client):
    r = client.put("/api/custom-providers/no-such-thing", json={"label": "X"})
    assert r.status_code == 404


def test_api_delete_unknown_404(client):
    r = client.delete("/api/custom-providers/no-such-thing")
    assert r.status_code == 404


# ── 8. Built-in name conflict via API ────────────────────────────────────────

def test_api_builtin_conflict_422(client, monkeypatch):
    import app.custom_providers as cp_mod
    monkeypatch.setattr(cp_mod, "_BUILTIN_NAMES", frozenset({"mock", "claude"}))
    r = client.post("/api/custom-providers", json={
        "id": "mock", "label": "x", "base_url": "http://localhost/v1",
        "default_model": "m",
    })
    assert r.status_code == 422
