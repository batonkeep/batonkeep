"""
conftest.py — Shared pytest fixtures.

Registry isolation: snapshot the static _ALL_PROVIDERS / _REGISTRY at
collection time and restore them after every test, so that tests which
inject custom providers (via app.custom_providers) don't leak into the
global registry observed by other tests.
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _coherent_settings():
    """Keep the `Settings` singleton coherent across modules around every test.

    Several modules bind ``_settings = get_settings()`` at import time, and some
    tests both (a) call ``get_settings.cache_clear()`` (e.g. ``test_db_migrations``
    repointing ``DATABASE_URL``) and (b) patch a module's ``_settings.__dict__``
    directly (e.g. the orchestrator/run-single tests setting ``work_dir`` to a tmp
    path). Once the cache is cleared, two modules imported on opposite sides of the
    clear hold *different* ``Settings`` instances depending on collection/import
    order — so a ``_settings.__dict__`` patch applied to one module is invisible to a
    reader in another. That manifested as the order-dependent
    ``OSError: Read-only file system: '/work'`` flakiness (``task_workspace`` reading
    the default ``work_dir`` while the test patched the orchestrator's snapshot).

    Before each test, rebind every loaded module's ``_settings`` snapshot to the one
    canonical ``get_settings()`` instance — independent of import order, so a
    module-level ``from app import …`` in a test file is safe again. We deliberately
    do **not** clear the cache or swap the canonical object during/after a test:
    in-place mutation of the live object (as the auth tests do) must stay visible to
    code paths that captured it (FastAPI deps, closures). Tests that repoint settings
    via ``cache_clear()`` (``test_db_migrations``) restore it themselves; the
    setup-time coalesce here then re-converges every module on the next test.
    """
    from app.config import Settings, get_settings

    canon = get_settings()
    for mod in list(sys.modules.values()):
        try:
            snap = getattr(mod, "_settings", None)
        except Exception:
            continue
        if isinstance(snap, Settings) and snap is not canon:
            try:
                mod._settings = canon
            except Exception:
                pass
    yield


@pytest.fixture(autouse=True)
def _registry_snapshot():
    """Snapshot the provider registry before each test, restore it after.

    This prevents custom-provider injection tests from polluting the static
    registry that P9 / routing tests rely on.
    """
    import app.providers.registry as reg
    import app.custom_providers as cp_mod

    # Deep-copy the mutable module globals that custom_providers.py mutates.
    saved_all = list(reg._ALL_PROVIDERS)
    saved_reg = dict(reg._REGISTRY)
    saved_names = frozenset(reg.ALL_TEMPLATE_NAMES)
    saved_injected = set(cp_mod._INJECTED_NAMES)
    yield
    # Restore to the pre-test state.
    reg._ALL_PROVIDERS[:] = saved_all
    reg._REGISTRY.clear()
    reg._REGISTRY.update(saved_reg)
    reg.ALL_TEMPLATE_NAMES = saved_names
    cp_mod._INJECTED_NAMES = saved_injected
