import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: requires network access or a local port (skipped without KG_TEST_NETWORK=1)",
    )


@pytest.fixture(autouse=True)
def _skip_network(request):
    if request.node.get_closest_marker("network"):
        if not os.environ.get("KG_TEST_NETWORK"):
            pytest.skip("network test — set KG_TEST_NETWORK=1 to enable")


@pytest.fixture(scope="session", autouse=True)
def _snapshot_guard():
    """Fail the session if snapshot/ is dirtied and not restored by the end."""
    before = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    ).stdout.strip()
    yield
    after = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    ).stdout.strip()
    assert after == before, (
        f"snapshot/ was modified during the test session:\n{after}"
    )


@pytest.fixture(autouse=True)
def _reset_semantic_caches():
    import kg_query.semantic
    import kg_query.server
    kg_query.semantic._index = None
    kg_query.server._store = None
    yield
    kg_query.semantic._index = None
    kg_query.server._store = None


@pytest.fixture
def snapshot_sources_backup():
    """Back up snapshot/ and sources.yml before a test; restore after."""
    snapshot_dir = REPO_ROOT / "snapshot"
    sources_file = REPO_ROOT / "sources.yml"

    td = Path(tempfile.mkdtemp(prefix="kg-test-backup-"))
    snap_backup = td / "snapshot"
    src_backup = td / "sources.yml"

    if snapshot_dir.exists():
        shutil.copytree(snapshot_dir, snap_backup)
    shutil.copy2(sources_file, src_backup)

    yield

    if snap_backup.exists():
        shutil.rmtree(snapshot_dir, ignore_errors=True)
        shutil.copytree(snap_backup, snapshot_dir)
    shutil.copy2(src_backup, sources_file)
    shutil.rmtree(td, ignore_errors=True)
