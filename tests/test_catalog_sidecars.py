"""mailserver.patch_catalog(): an mcp-email-server killed while it opened its settings catalog leaves the -wal/-shm
files with inherited permissions, and the server then refused every later open (KOL Mail's mail backend stayed down
after the computer woke from sleep, 2026-11-04). Run against the real mcp-email-server and a real catalog."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mailserver  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the private-file policy is Windows only")


@pytest.fixture
def managed(monkeypatch):
    from mcp_email_server import managed
    monkeypatch.setattr(managed, "_validate_sidecars", managed._validate_sidecars)  # patch_catalog's is undone after
    return managed


@pytest.fixture
def catalog(managed, tmp_path):
    from mcp_email_server.windows_security import ensure_private_parent
    path = tmp_path / "cfg" / "catalog.sqlite3"
    ensure_private_parent(path)
    return managed.ManagedCatalog.initialize(path)


def kill_while_open(path: Path):
    """A server process that dies with the catalog open, as a forced restart kills it."""
    code = f"import sqlite3, os; c = sqlite3.connect({str(path)!r}); c.execute('select 1 from catalog'); os._exit(0)"
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
    assert Path(f"{path}-wal").exists() and Path(f"{path}-shm").exists()


def test_without_the_patch_a_killed_server_locks_every_later_open_out(managed, catalog):
    kill_while_open(catalog.path)
    with pytest.raises(managed.ManagedCatalogSecurityError, match="sidecar"):
        catalog.catalog_revision()


def test_the_next_open_locks_the_left_files_down_and_goes_on(managed, catalog):
    assert mailserver.patch_catalog()
    kill_while_open(catalog.path)
    assert catalog.catalog_revision() == 1


def test_a_sidecar_that_is_a_hard_link_is_still_refused(managed, catalog, tmp_path):
    assert mailserver.patch_catalog()
    other = tmp_path / "cfg" / "other"
    other.write_bytes(b"")
    os.link(other, f"{catalog.path}-wal")
    with pytest.raises(managed.ManagedCatalogSecurityError, match="could not be secured"):  # hardening refused it
        catalog.catalog_revision()
