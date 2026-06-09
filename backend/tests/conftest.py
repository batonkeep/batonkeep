"""
conftest.py — Shared pytest fixtures.

Registry isolation: snapshot the static _ALL_PROVIDERS / _REGISTRY at
collection time and restore them after every test, so that tests which
inject custom providers (via app.custom_providers) don't leak into the
global registry observed by other tests.
"""
from __future__ import annotations

import pytest


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
