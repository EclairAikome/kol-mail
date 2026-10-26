"""Helpers for the fresh-install smoke tests, run with the installed venv's Python (they import the installed
mail component and app). The password comes from a JSON file so no console encoding ever touches it.

    mail_secret.py check <catalog.sqlite3> <account> <password.json>   stored password == the typed one, both roles
    mail_secret.py add-web <app dir> <mcp exe> <account> <address> <password.json>   as the login page adds one
    mail_secret.py remove <app dir> <mcp exe> <account>...             remove test accounts and their passwords"""

import asyncio
import json
import sys
from pathlib import Path


def main(cmd: str, *args: str) -> int:
    if cmd == "check":
        from mcp_email_server.managed import ManagedCatalog
        catalog, account, password_file = args
        want = json.loads(Path(password_file).read_text(encoding="utf-8"))
        for role in ("incoming", "outgoing"):
            settings = ManagedCatalog(Path(catalog)).load_account(account, roles=(role,))
            got = getattr(settings, role).password.get_secret_value()
            if got != want:
                print(f"{account} {role}: stored {got!r}, typed {want!r}")
                return 1
        print(f"{account}: stored byte for byte")
        return 0
    sys.path.insert(0, args[0])
    import accounts
    cli = accounts.Cli(args[1])
    if cmd == "add-web":
        account, address, password_file = args[2:]
        password = json.loads(Path(password_file).read_text(encoding="utf-8"))
        servers = {"imap_host": "127.0.0.1", "imap_port": 993, "imap_security": "ssl",
                   "smtp_host": "127.0.0.1", "smtp_port": 465, "smtp_security": "ssl"}
        asyncio.run(cli.add(account, address, "CI Tester", servers, password))
        print(f"{account}: added")
        return 0
    if cmd == "remove":
        left = [name for name in args[2:] if not asyncio.run(cli.remove(name))]
        print("removed" if not left else f"password left behind for {left}")
        return 1 if left else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
