"""Tests for the cplug-fork patch to ``modules.sd_vae_taesd.download_model``.

The upstream Forge implementation has a TOCTOU race: two threads that
both observe ``os.path.exists(path) == False`` simultaneously each
call ``torch.hub.download_url_to_file(url, path)`` and truncate the
same path. The result is a torn file that fails ``torch.load`` with
``PytorchStreamReader`` errors.

Our patch (per-path lock + atomic rename via ``.part``) must:

- Serialise concurrent downloads of the same path (only one network
  fetch even with N concurrent callers).
- Publish atomically — observers never see a half-written file at the
  final path; the ``.part`` sibling is the only place a partial write
  exists.
- Not deadlock when callers from different threads request different
  paths (per-path locks, not a global lock).
- Clean up ``.part`` on failure so retries are not blocked by stale
  partial files.

Network is mocked — we never actually download. The mock's only job
is to (a) write some bytes to whatever path it's given and (b) report
how many times it was called.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from unittest.mock import patch

import pytest

# Stub the heavy Forge transitive imports BEFORE importing the module
# under test. ``sd_vae_taesd``'s class definitions reference
# ``backend.state_dict.load_state_dict`` etc., but ``download_model``
# (the function under test) needs none of them. We provide enough
# scaffolding to satisfy the module body's import statements.
for _mod in (
    "backend",
    "backend.state_dict",
    "backend.utils",
    "modules.devices",
    "modules.paths_internal",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

sys.modules["backend.state_dict"].load_state_dict = lambda *a, **k: None
sys.modules["backend.utils"].load_torch_file = lambda *a, **k: {}
sys.modules["modules.devices"].device = "cpu"
sys.modules["modules.devices"].dtype = None
sys.modules["modules.paths_internal"].models_path = "/dev/null"

from modules import sd_vae_taesd  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_locks():
    """Drop stashed per-path locks between tests so identity-based
    assertions don't leak across cases."""
    sd_vae_taesd._download_locks.clear()
    yield
    sd_vae_taesd._download_locks.clear()


def _fake_download_writes(_url, dest):
    """Stand-in for ``torch.hub.download_url_to_file`` that writes
    deterministic bytes so we can assert on them."""
    with open(dest, "wb") as f:
        f.write(b"FAKETAESDBYTES")


def test_concurrent_calls_download_once(tmp_path):
    target = tmp_path / "VAE-taesd" / "fake.pth"
    call_count = {"n": 0}
    barrier = threading.Barrier(8)

    def counting_download(url, dest):
        # Barrier-sync ensures all 8 threads enter the function near-
        # simultaneously, maximising the race window the lock has to
        # close. The lock is what makes call_count == 1; without it
        # the upstream impl gets call_count == 8.
        with threading.Lock():
            call_count["n"] += 1
        _fake_download_writes(url, dest)

    def worker():
        barrier.wait()
        sd_vae_taesd.download_model(str(target), "https://example.invalid/x.pth")

    with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", counting_download):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Exactly one network fetch even with 8 concurrent callers.
    assert call_count["n"] == 1
    # Final file is at the canonical path with the expected bytes.
    assert target.read_bytes() == b"FAKETAESDBYTES"
    # No leftover .part — atomic rename completed cleanly.
    assert not (tmp_path / "VAE-taesd" / "fake.pth.part").exists()


def test_existing_file_short_circuits(tmp_path):
    target = tmp_path / "already.pth"
    target.write_bytes(b"PRE-EXISTING")
    call_count = {"n": 0}

    def counting_download(url, dest):
        call_count["n"] += 1
        _fake_download_writes(url, dest)

    with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", counting_download):
        sd_vae_taesd.download_model(str(target), "https://example.invalid/x.pth")

    assert call_count["n"] == 0
    assert target.read_bytes() == b"PRE-EXISTING"


def test_atomic_publish_via_dot_part(tmp_path):
    """Network writes go to ``.part``; the canonical path appears only
    after a successful rename. Observers cannot see a half-written
    final-path file."""
    target = tmp_path / "atomic.pth"
    seen_part_during_write: list[bool] = []

    def slow_download(url, dest):
        # Confirm the network write hits the .part sibling, not the
        # final path — that's the atomicity guarantee.
        assert dest.endswith(".part")
        seen_part_during_write.append(target.exists())
        with open(dest, "wb") as f:
            f.write(b"X")

    with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", slow_download):
        sd_vae_taesd.download_model(str(target), "https://example.invalid/x.pth")

    # Final path didn't exist while we were writing the .part.
    assert seen_part_during_write == [False]
    # After return, final path exists and .part is gone.
    assert target.read_bytes() == b"X"
    assert not (tmp_path / "atomic.pth.part").exists()


def test_failed_download_cleans_up_part(tmp_path):
    """A network failure must leave no orphan ``.part`` blocking a retry."""
    target = tmp_path / "fail.pth"

    def boom(url, dest):
        # Write some partial bytes (mimicking a failed download mid-stream)
        # then raise — exercises the cleanup path properly.
        with open(dest, "wb") as f:
            f.write(b"partial")
        raise RuntimeError("simulated network error")

    with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", boom):
        with pytest.raises(RuntimeError, match="simulated"):
            sd_vae_taesd.download_model(str(target), "https://example.invalid/x.pth")

    assert not target.exists()
    assert not (tmp_path / "fail.pth.part").exists()


def test_different_paths_use_different_locks(tmp_path):
    """Two concurrent callers downloading DIFFERENT files must not
    serialise on each other — per-path locks are independent."""
    a = tmp_path / "a.pth"
    b = tmp_path / "b.pth"

    in_progress = threading.Event()
    release = threading.Event()
    second_done = threading.Event()

    def block_first(url, dest):
        in_progress.set()
        # First caller holds its lock until released.
        release.wait(timeout=2.0)
        with open(dest, "wb") as f:
            f.write(b"A")

    def quick(url, dest):
        with open(dest, "wb") as f:
            f.write(b"B")

    def first():
        with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", block_first):
            sd_vae_taesd.download_model(str(a), "https://example.invalid/a.pth")

    def second():
        in_progress.wait(timeout=2.0)
        with patch.object(sd_vae_taesd.torch.hub, "download_url_to_file", quick):
            sd_vae_taesd.download_model(str(b), "https://example.invalid/b.pth")
        second_done.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()

    # Second caller (different path) must complete WITHOUT waiting for
    # the first to release — that proves the locks are per-path.
    assert second_done.wait(timeout=2.0), "different-path callers must not serialise"

    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert a.read_bytes() == b"A"
    assert b.read_bytes() == b"B"


def test_lock_for_path_normalises(tmp_path):
    """``./foo.pth`` and ``foo.pth`` resolve to the same lock — otherwise
    the race re-emerges when one caller passes a relative path and
    another passes an absolute path to the same file."""
    rel = "./relative.pth"
    abs_ = os.path.abspath(rel)
    assert sd_vae_taesd._lock_for_path(rel) is sd_vae_taesd._lock_for_path(abs_)
