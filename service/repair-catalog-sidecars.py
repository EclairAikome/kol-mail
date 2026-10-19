"""Re-harden mcp-email-server's catalog -wal/-shm files before kol-mail starts.

SQLite creates these sidecars with inherited permissions and mcp-email-server locks them down right after. A process
killed in between (e.g. a forced service restart) leaves them inherited, and every later mcp-email-server open then
refuses with "Managed catalog sidecar does not satisfy the Windows private-file policy". This applies the server's own
hardening to whatever sidecars exist. It never opens the database. Run with the mcp-email-server venv python, -I.
"""
from pathlib import Path

from mcp_email_server.windows_security import harden_private_file

catalog = Path.home() / ".config" / "mcp-email-server" / "catalog.sqlite3"
for suffix in ("-wal", "-shm"):
    sidecar = Path(f"{catalog}{suffix}")
    try:
        harden_private_file(sidecar)
    except FileNotFoundError:
        continue
    except Exception as e:  # never block the app start; the server reports the problem itself
        print(f"could not harden {sidecar.name}: {e}")
