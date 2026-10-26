# A fresh Windows install end to end, the way a colleague runs it, with what bit colleagues before:
#  - an old CodeMaker (1.15.11 crashed on `run --agent`) is the only AI there is: the installer's AI check must pass;
#  - the console is chcp 65001 (the install kit's .cmd sets it): a password must be stored byte for
#    byte, through the installer's own password function and through the login page's code, even one holding a BOM.
# Runs in CI (.github/workflows/test.yml) and on a developer PC: everything goes under -Work, and USERPROFILE points
# there too, so the mail component's catalog (where it keeps passwords on Windows) of the PC running it is never
# touched. Keep -Work short: pip fails on paths longer than 260 characters.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\smoke\windows_fresh_install.ps1 -Work <empty folder>

param(
    [string]$Source = "",  # this checkout
    [Parameter(Mandatory = $true)][string]$Work,
    [string]$Python = "python",
    [int]$TimeoutMinutes = 20
)

$ErrorActionPreference = "Stop"
if (-not $Source) { $Source = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
function Step($text) { Write-Host "== $text" -ForegroundColor Cyan }
function Check($ok, $what) { if (-not $ok) { throw "FAILED: $what" } Write-Host "   ok: $what" }

New-Item -ItemType Directory -Force $Work | Out-Null
$Work = (Resolve-Path $Work).Path
$env:USERPROFILE = Join-Path $Work "home"
New-Item -ItemType Directory -Force $env:USERPROFILE | Out-Null
$Dir = Join-Path $Work "kol-mail"

Step "an old CodeMaker on PATH"
$cmDir = Join-Path $Work "codemaker"
$cmLog = Join-Path $cmDir "calls.jsonl"
& $Python -I (Join-Path $Source "tests\fakes\fake_codemaker.py") install $cmDir --version 1.15.11 --log $cmLog | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not set up the fake CodeMaker" }
$env:Path = "$cmDir;$env:Path"

Step "install.ps1, answering like a colleague: CodeMaker, no KOL sheet"
$answers = Join-Path $Work "answers.txt"
[IO.File]::WriteAllText($answers, "codemaker`r`n`r`n", (New-Object Text.UTF8Encoding $false))
$out, $err = (Join-Path $Work "install.out.txt"), (Join-Path $Work "install.err.txt")
$installer = Join-Path $Source "install.ps1"
# cmd /c: chcp 65001 first, as the colleague kit does, then the installer in that same console
$p = Start-Process cmd.exe -NoNewWindow -PassThru -RedirectStandardInput $answers -RedirectStandardOutput $out -RedirectStandardError $err `
    -ArgumentList "/d /c chcp 65001 >nul && powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$installer`" -Source `"$Source`" -Dir `"$Dir`" -NoService"
$null = $p.Handle  # without it PowerShell 5.1 never fills in ExitCode
if (-not $p.WaitForExit($TimeoutMinutes * 60000)) {
    & taskkill.exe /T /F /PID $p.Id | Out-Null
    Get-Content -Encoding UTF8 $out | Select-Object -Last 40
    throw "FAILED: the installer was still running after $TimeoutMinutes minutes (waiting for an answer?)"
}
Get-Content -Encoding UTF8 $out
Get-Content -Encoding UTF8 $err
Check ($p.ExitCode -eq 0) "installer exit code 0 (was $($p.ExitCode))"
Check ((Get-Content -Raw -Encoding UTF8 $out) -match "AI 连接测试通过") "the installer's AI check passed on CodeMaker 1.15.11"

$settings = Get-Content -Raw -Encoding UTF8 (Join-Path $Dir "app\data\settings.json") | ConvertFrom-Json
Check ($settings.llm.backend -eq "codemaker") "settings: backend codemaker"
Check ("$($settings.llm.codemaker_bin)" -like "$cmDir*") "settings: the CodeMaker the check verified"
$runs = @(Get-Content -Encoding UTF8 $cmLog | Where-Object { $_ -match '"argv": \["run"' })
Check ($runs.Count -eq 2) "two model calls (translate, draft) ($($runs.Count))"
Check (-not ($runs -match '"--agent"')) "no --agent (old CodeMaker crashes on it)"

$Py = Join-Path $Dir "venv\Scripts\python.exe"
$Mcp = Join-Path $Dir "venv\Scripts\mcp-email-server.exe"
$Password = [string][char]0xFEFF + "KOL-p" + [char]0xE4 + "ssw" + [char]0xF6 + "rd-" + [char]0x5BC6 + [char]0x7801 + " #1"
$expected = Join-Path $Work "password.json"
[IO.File]::WriteAllText($expected, ('"' + (($Password.ToCharArray() | ForEach-Object { "\u{0:x4}" -f [int]$_ }) -join "") + '"'),
    (New-Object Text.UTF8Encoding $false))
$Helper = Join-Path $Source "tests\smoke\mail_secret.py"
$Catalog = Join-Path $env:USERPROFILE ".config\mcp-email-server\catalog.sqlite3"

Step "a password with a BOM through install.ps1's Invoke-McpWithSecret (chcp 65001)"
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$errors)
$ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) |
    Where-Object { @("Run-Native", "Join-NativeArgs", "Invoke-McpWithSecret") -contains $_.Name } |
    ForEach-Object { . ([scriptblock]::Create($_.Extent.Text)) }
& chcp.com 65001 | Out-Null
[Console]::InputEncoding = [Text.Encoding]::UTF8  # with a BOM preamble, as chcp 65001 leaves it
$r = Invoke-McpWithSecret @($Password, $Password) @("account", "add", "cibom", "--email", "ci@example.com", "--full-name", "CI Tester",
    "--imap-host", "127.0.0.1", "--smtp-host", "127.0.0.1", "--imap-user", "ci@example.com", "--smtp-user", "ci@example.com", "--password-stdin")
Check ($r.Code -eq 0) "mail component stored the account ($($r.Out))"
$check = Run-Native { & $Py -I $Helper check $Catalog "cibom" $expected }
Write-Host "   $($check.Out)"
Check ($check.Code -eq 0) "installer path: password stored byte for byte"

Step "the same password through the login page's code (accounts.Cli)"
$w = Run-Native { & $Py -I $Helper add-web (Join-Path $Dir "app") $Mcp "ciweb" "ci2@example.com" $expected }
Write-Host "   $($w.Out)"
Check ($w.Code -eq 0) "login page path: account added"
$check = Run-Native { & $Py -I $Helper check $Catalog "ciweb" $expected }
Write-Host "   $($check.Out)"
Check ($check.Code -eq 0) "login page path: password stored byte for byte"

Step "remove the two test accounts and their passwords"
$c = Run-Native { & $Py -I $Helper remove (Join-Path $Dir "app") $Mcp "cibom" "ciweb" }
Write-Host "   $($c.Out)"
Check ($c.Code -eq 0) "test accounts and passwords removed"

Step "the installed app starts and serves its page (dev mode: no background work, nothing sent)"
$env:KOL_DEV = "1"; $env:KOL_MAIL_PORT = "8766"
$app = Start-Process $Py -ArgumentList @("-I", (Join-Path $Dir "app\app.py")) -WorkingDirectory (Join-Path $Dir "app") -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $Work "app.out.txt") -RedirectStandardError (Join-Path $Work "app.err.txt")
try {
    $status = $null
    for ($i = 0; $i -lt 60 -and -not $status; $i++) {
        Start-Sleep -Seconds 1
        try { $status = Invoke-RestMethod -Headers @{ "X-KOL-Mail" = "1" } "http://127.0.0.1:8766/api/status" } catch { }
    }
    if (-not $status) { Get-Content (Join-Path $Work "app.err.txt") -Tail 30 }
    $want = (Get-Content -Raw (Join-Path $Source "VERSION")).Trim()
    Check ($status -and "$($status.version)" -eq $want) "app answers /api/status with version $want"
} finally {
    & taskkill.exe /T /F /PID $app.Id 2>$null | Out-Null
}
Write-Host "fresh install smoke test passed" -ForegroundColor Green
