"""Regression for dd44afa (v0.2.2). Every new Windows install had its mail login rejected: under the kit's chcp 65001,
.NET wrote a UTF-8 BOM to the mail component's stdin ahead of the password, and the PowerShell 5.1 pipeline turned
non-ASCII characters into "?". install.ps1's Invoke-McpWithSecret must hand the passwords over byte for byte.

Matches: provider policy rejected"""

import sys

STUB = "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())\n"


def test_passwords_reach_the_mail_component_byte_for_byte(run_ps_functions, tmp_path):
    stub = tmp_path / "mcp_stub.py"
    stub.write_text(STUB, encoding="utf-8")
    got = tmp_path / "stdin.bin"
    first, second = "Pässwörd-密码 #1", "plain-ASCII-2"
    r = run_ps_functions(["Run-Native", "?Join-NativeArgs", "Invoke-McpWithSecret"], f"""
        chcp 65001 > $null  # what the colleague kit's .cmd does before starting install.ps1
        [Console]::InputEncoding = [Text.Encoding]::UTF8  # UTF-8 *with* a BOM preamble, as chcp 65001 leaves it
        $Mcp = '{sys.executable}'
        $r = Invoke-McpWithSecret @('{first}', '{second}') @('-I', '{stub}', '{got}')
        if ($r.Code -ne 0) {{ throw "stub failed: $($r.Out)" }}
    """)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace") + r.stdout.decode("utf-8", "replace")
    data = got.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "a BOM went in ahead of the first password"
    assert b"?" not in data, "non-ASCII characters were turned into '?'"
    assert data.replace(b"\r\n", b"\n") == f"{first}\n{second}\n".encode("utf-8")
