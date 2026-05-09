"""Pytest bootstrap for cplugapi unit tests.

The cplugapi endpoint modules pull state from ``modules.progress`` and
``modules.shared`` at call time. Importing the real ``modules.shared`` means
booting half the WebUI (CUDA, gradio, model registries). For unit tests we
install lightweight stubs into ``sys.modules`` before any cplugapi module
runs, so endpoints exercise their real logic against in-memory state.
"""

from __future__ import annotations

import os
import sys
import types
from collections import OrderedDict

import pytest

# FastAPI ``TestClient`` defaults to ``Host: testserver``, which the
# audit-02 security middleware (correctly) rejects in production. For
# the test suite we extend the host allow-list with ``testserver`` once
# at collection time so fixtures using plain ``TestClient(app)`` (no
# ``base_url`` override) keep passing. Tests that monkeypatch this env
# var see ``testserver`` as the captured pre-test value, so teardown
# reverts here.
os.environ["CPLUG_ALLOWED_HOSTS"] = "testserver"


def _install_progress_stub() -> types.ModuleType:
    """Replace ``modules.progress`` with a tiny in-memory stub."""
    stub = types.ModuleType("modules.progress")
    stub.pending_tasks = OrderedDict()  # type: ignore[attr-defined]
    stub.current_task = None  # type: ignore[attr-defined]
    stub.finished_tasks = []  # type: ignore[attr-defined]
    sys.modules["modules.progress"] = stub
    return stub


def _install_shared_stub() -> types.ModuleType:
    """Replace ``modules.shared`` with a tiny stub exposing ``state.interrupt``."""
    stub = types.ModuleType("modules.shared")

    class _State:
        def __init__(self) -> None:
            self.interrupt_called = 0

        def interrupt(self) -> None:
            self.interrupt_called += 1

    stub.state = _State()  # type: ignore[attr-defined]
    sys.modules["modules.shared"] = stub
    return stub


# Install stubs at collection time. Done at module top so any subsequent
# ``from modules import progress`` or ``from modules import shared`` (in
# cplugapi or elsewhere) gets the stub even if the test never explicitly
# touches the fixtures below.
_install_progress_stub()
_install_shared_stub()


@pytest.fixture
def progress_stub():
    """Reset and return the ``modules.progress`` stub for this test."""
    stub = sys.modules["modules.progress"]
    stub.pending_tasks.clear()
    stub.current_task = None
    stub.finished_tasks.clear()
    return stub


@pytest.fixture
def shared_stub():
    """Reset and return the ``modules.shared`` stub for this test.

    Replaces ``state`` with a fresh ``_State`` instance rather than
    just resetting the counter — the previous test may have monkey-
    patched ``state.interrupt`` (e.g., test_session_cancel's
    interrupt-raises path) and only resetting the counter would leave
    that override in place to poison subsequent tests.
    """
    stub = sys.modules["modules.shared"]

    class _State:
        def __init__(self) -> None:
            self.interrupt_called = 0

        def interrupt(self) -> None:
            self.interrupt_called += 1

    stub.state = _State()  # type: ignore[attr-defined]
    return stub


@pytest.fixture
def clean_capabilities():
    """Clear the capability registry before/after each test."""
    from modules.cplugapi import capabilities

    capabilities.reset()
    yield capabilities
    capabilities.reset()


@pytest.fixture
def clean_cancelled():
    """Clear the cancelled-tasks registry before/after each test."""
    from modules.cplugapi import cancelled_tasks

    cancelled_tasks.reset()
    yield cancelled_tasks
    cancelled_tasks.reset()
