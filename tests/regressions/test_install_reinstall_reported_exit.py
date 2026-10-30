"""2026-10-30: a colleague's reinstall posted "KOL Mail 退出了，退出码 -1" to the feedback group. install.ps1 stopped the
scheduled task, which ends conhost but not the powershell running service\run.ps1 inside it, then killed python; the
loop took the kill for a crash, reported it, and 15 s later would have started the old copy again in the middle of
the install. Stop-KolMailService ends this install's loop first, then its python.

Real processes in a temporary folder: a stand-in run.ps1 runs a stand-in app.py with a real venv's python and leaves
a mark when python ends under it (where the real loop reports and restarts). Only processes under that folder are
touched, and the scheduled task asked to stop does not exist, so the machine's own KOL Mail is never involved."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import powershell

LOOP = r"""
param([string]$Python, [string]$Marks)
while ($true) {
    cmd /c "`"$Python`" -I -c `"import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('x'); time.sleep(120)`" `"$Marks\running`" app.py"
    Set-Content -Path (Join-Path $Marks "reported") -Value $LASTEXITCODE
    Start-Sleep -Seconds 1
}
"""


def wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="install.ps1 is the Windows installer")
def test_a_reinstall_stops_the_service_loop_before_python(run_ps_functions, tmp_path):
    exe = powershell()
    app, marks = tmp_path / "app", tmp_path / "marks"
    (app / "service").mkdir(parents=True)
    marks.mkdir()
    (app / "service" / "run.ps1").write_text(LOOP, encoding="utf-8-sig")
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
    py = venv / "Scripts" / "python.exe"
    loop = subprocess.Popen([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                             str(app / "service" / "run.ps1"), "-Python", str(py), "-Marks", str(marks)],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert wait_for(marks / "running", 30), "the stand-in app never started"
        (marks / "running").unlink()
        r = run_ps_functions(["Stop-KolMailService"], f"""
            Stop-KolMailService '{app}' '{venv}' 'kol-mail-test-no-such-task'
            Start-Sleep -Seconds 1
            $left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object {{ $_.ExecutablePath -like '{venv}*' -or $_.CommandLine -like '*{marks}*' }})
            Write-Output "left=$($left.Count)"
        """)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        assert "left=0" in r.stdout.decode("utf-8", "replace")
        assert loop.wait(timeout=10) is not None  # the loop itself is gone
        time.sleep(2)  # what the old installer let happen: the loop noticed python's end
        assert not (marks / "reported").exists(), "the service loop saw python killed (it would report a crash)"
        assert not (marks / "running").exists(), "the old copy was started again"
    finally:
        if loop.poll() is None:
            loop.kill()
        subprocess.run([exe, "-NoProfile", "-Command",
                        f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {{ $_.CommandLine "
                        f"-like '*{marks}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                       capture_output=True, timeout=60)
