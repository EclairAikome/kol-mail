# KOL Mail 一键安装（Windows 10/11，PowerShell 5.1 即可）
#
# 用法（维护者会在 POPO 群里发带参数的完整命令）：
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Repo owner/kol-mail -Feedback <反馈地址> -ImapHost <收信服务器> -SmtpHost <发信服务器>
#
# 做的事：找到或安装 Python，下载最新版 KOL Mail，装依赖，配置你的邮箱和 AI（Claude Code 或 CodeMaker），
# 写好你的签名，注册开机自动启动的后台任务，在桌面放一个“KOL Mail”图标。重复运行是安全的，已有的设置会保留。
# 邮箱密码只在邮件组件自己的提示里输入，这个脚本看不到也不保存。

param(
    [string]$Repo = "",
    [string]$Feedback = "",
    [string]$ImapHost = "",
    [string]$SmtpHost = "",
    [string]$PipIndex = "",
    [string]$Mirror = "",  # GitHub 连不上时用的 https 代理前缀
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "kol-mail"),
    [string]$Source = ""   # 本地源码目录（维护者测试用）；留空则从 GitHub 下载最新版
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Say($text) { Write-Host $text -ForegroundColor Cyan }
function Warn($text) { Write-Host $text -ForegroundColor Yellow }
function Ask($question, $default = "") {
    $hint = if ($default) { "（直接回车用：$default）" } else { "" }
    $answer = Read-Host "$question$hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim()
}
function Has($obj, $name) {
    return ($null -ne $obj) -and ($obj.PSObject.Properties.Name -contains $name) -and ($null -ne $obj.$name) -and ("$($obj.$name)" -ne "")
}
# Windows PowerShell 5.1 turns any stderr line of a native command into a terminating error under "Stop": run them
# with "Continue" and judge them by their exit code only.
function Run-Native([scriptblock]$block) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & $block 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $saved }
    return @{ Code = $LASTEXITCODE; Out = ($out -join "`n") }
}
function Gh($url) { if ($Mirror) { return "$($Mirror.TrimEnd('/'))/$url" } return $url }

if ($Mirror -and -not $Mirror.StartsWith("https://")) { throw "-Mirror 必须是 https 地址" }

$App = Join-Path $Dir "app"
$Venv = Join-Path $Dir "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Data = Join-Path $App "data"
New-Item -ItemType Directory -Force $Dir | Out-Null

# 重复安装时先停掉正在运行的旧版本，不然 pip 换不了被占用的文件
if (Get-ScheduledTask -TaskName "kol-mail" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "kol-mail" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*app.py*" -and $_.ExecutablePath -like "$Venv*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- 1. Python
Say "[1/8] 检查 Python…"
function Find-Python {
    foreach ($cand in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"))) {
        try {
            $exe = $cand[0]; $rest = @($cand | Select-Object -Skip 1)
            $r = Run-Native { & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" }
            $v = @($r.Out -split "`n")
            if ($r.Code -eq 0 -and $v.Count -ge 2) {
                if ([version]$v[0].Trim() -ge [version]"3.11" -and $v[1] -notlike "*WindowsApps*") { return $v[1].Trim() }
            }
        } catch {}
    }
    return $null
}
$BasePy = Find-Python
if (-not $BasePy) {
    Say "没找到 Python 3.11 以上，正在用 winget 安装 Python 3.13（只装给你自己）…"
    Run-Native { winget install --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements } | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $BasePy = Find-Python
    if (-not $BasePy) { throw "Python 安装失败。请先从 python.org 装 Python 3.13（勾选 Add to PATH），再运行本脚本。" }
}
Say "  用 $BasePy"

# ---------------------------------------------------------------- 2. 程序本体
Say "[2/8] 获取 KOL Mail…"
$staging = Join-Path $env:TEMP ("kolmail-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $staging | Out-Null
try {
    if ($Source) {
        $src = (Resolve-Path $Source).Path
    } else {
        if (-not $Repo) { $Repo = Ask "GitHub 仓库（owner/name）" }
        $rel = Invoke-RestMethod -Headers @{ Accept = "application/vnd.github+json" } (Gh "https://api.github.com/repos/$Repo/releases/latest")
        $zip = Join-Path $staging "src.zip"
        Invoke-WebRequest -UseBasicParsing (Gh "https://codeload.github.com/$Repo/zip/refs/tags/$($rel.tag_name)") -OutFile $zip
        Expand-Archive $zip (Join-Path $staging "x")
        $src = (Get-ChildItem (Join-Path $staging "x") -Directory | Select-Object -First 1).FullName
        Say "  最新版 $($rel.tag_name)"
    }
    New-Item -ItemType Directory -Force $App | Out-Null
    # data\ 是你的设置和缓存，永远不覆盖
    robocopy $src $App /E /XD data .git __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制程序文件失败（robocopy $LASTEXITCODE）" }
} finally {
    Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force $Data | Out-Null

# ---------------------------------------------------------------- 3. 依赖
Say "[3/8] 安装依赖（第一次要几分钟）…"
if (-not (Test-Path $Py)) { Run-Native { & $BasePy -m venv $Venv } | Out-Null }
if (-not (Test-Path $Py)) { throw "创建 Python 虚拟环境失败" }
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", (Join-Path $App "requirements.txt"))
if ($PipIndex) { $pipArgs += @("-i", $PipIndex) }
$r = Run-Native { & $Py @pipArgs }
if ($r.Code -ne 0) { throw "依赖安装失败：$($r.Out)`n公司网络慢的话，可以加参数 -PipIndex https://mirrors.aliyun.com/pypi/simple 再试。" }
$Mcp = Join-Path $Venv "Scripts\mcp-email-server.exe"

# ---------------------------------------------------------------- 4. 邮箱
Say "[4/8] 配置邮箱…"
$status = (Run-Native { & $Mcp config status }).Out
if ($status -match "not_configured") {
    $catalog = Join-Path $env:USERPROFILE ".config\mcp-email-server\catalog.sqlite3"
    $r = Run-Native { & $Mcp config init --database $catalog }
    if ($r.Code -ne 0) { throw "初始化邮件组件失败：$($r.Out)" }
}
$settingsPath = Join-Path $Data "settings.json"
$old = $null
if (Test-Path $settingsPath) { $old = Get-Content -Raw -Encoding UTF8 $settingsPath | ConvertFrom-Json }
$email = if ((Has $old "mail") -and (Has $old.mail "address")) { $old.mail.address } else { "" }
while ($email -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") { $email = Ask "你的工作邮箱" }
$email = $email.ToLower()
$account = if ((Has $old "mail") -and (Has $old.mail "account_name")) { $old.mail.account_name } else { ($email.Split("@")[0] -replace "[^A-Za-z0-9_-]", "") }
$fullName = if ((Has $old "identity") -and (Has $old.identity "name")) { $old.identity.name } else { "" }
while (-not $fullName) { $fullName = Ask "对外署名（英文名，会出现在签名里）" }
$accounts = (Run-Native { & $Mcp account list }).Out
if ($accounts -notmatch "(?m)\b$([regex]::Escape($account))\b") {
    if (-not $ImapHost) { $ImapHost = Ask "收信服务器（IMAP）" }
    if (-not $SmtpHost) { $SmtpHost = Ask "发信服务器（SMTP）" }
    Say "  下面会提示输入邮箱密码（输入时不显示），密码由邮件组件存进 Windows 凭据管理器。"
    & $Mcp account add $account --email $email --full-name $fullName --imap-host $ImapHost --smtp-host $SmtpHost `
        --imap-user $email --smtp-user $email
    if ($LASTEXITCODE -ne 0) { throw "添加邮箱失败，检查服务器地址和密码后重新运行本脚本。" }
    $t = Run-Native { & $Mcp account test $account }
    if ($t.Code -ne 0) { Warn "  邮箱连接测试没通过：$($t.Out)" }
}
# 回信、附件、移动邮件要用到的权限
$policy = (Run-Native { & $Mcp config policy }).Out
$rev = [regex]::Match($policy, "(?m)^revision=(\d+)").Groups[1].Value
$needSend = $policy -notmatch "(?m)^allowed_mutations=.*\bsend\b"
$needAtt = $policy -notmatch "(?m)^enable_attachment_download=true"
if ($rev -and ($needSend -or $needAtt)) {
    $r = Run-Native { & $Mcp config update-policy --expected-revision $rev --enable-attachment-download --enable-attachment-content `
        --allowed-mutations "draft,organize,send,append" --allowed-recipients "*" }
    if ($r.Code -ne 0) { Warn "  没能打开回信和附件权限：$($r.Out)" }
}

# ---------------------------------------------------------------- 5. AI
Say "[5/8] 选择 AI…"
$cm = Join-Path $env:USERPROFILE ".codemaker\bin\codemaker.exe"
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
$haveCm = Test-Path $cm
$default = if ((Has $old "llm") -and (Has $old.llm "backend")) { $old.llm.backend } elseif ($haveCm) { "codemaker" } else { "claude" }
Say ("  检测到：" + ($(if ($haveCm) { "CodeMaker " } else { "" }) + $(if ($claude) { "Claude Code" } else { "" })))
$backend = ""
while ($backend -ne "codemaker" -and $backend -ne "claude") { $backend = (Ask "用哪个 AI？输入 codemaker 或 claude" $default).ToLower() }
if ($backend -eq "codemaker") {
    if (-not $haveCm) { Warn "  没找到 CodeMaker 命令行。请先按公司指引安装 CodeMaker CLI，装好后 KOL Mail 就能用。" }
    elseif ((Run-Native { & $cm quota }).Code -ne 0) { Warn "  CodeMaker 还没登录：请在新窗口运行 codemaker auth login，登录后 KOL Mail 就能用。" }
} elseif (-not $claude) {
    Warn "  没找到 Claude Code 命令行（claude）。装好并登录一次后 KOL Mail 就能用。"
}

# ---------------------------------------------------------------- 6. 你的设置（只写安装器负责的键，其余保持原样）
Say "[6/8] 写设置…"
$answers = [ordered]@{
    mail = [ordered]@{ account_name = $account; address = $email; company_domain = "@" + $email.Split("@")[1]; mcp_exe = $Mcp }
    llm = [ordered]@{ backend = $backend }
    paths = [ordered]@{}
    identity = [ordered]@{ name = $fullName }
    update = [ordered]@{}
    feedback = [ordered]@{}
}
if (-not ((Has $old "identity") -and (Has $old.identity "signature"))) {
    $title = Ask "职位（英文）" "Marketing Manager"
    $company = ""
    while (-not $company) { $company = Ask "公司名（英文）" }
    $extra = Ask "签名里额外的一行（比如 Discord: xxx，没有就回车）"
    $site = Ask "签名里的网址（没有就回车）"
    $answers.identity.title = $title
    $answers.identity.company = $company
    $answers.identity.signature = (@($fullName, "$title | $company", $extra, $site) | Where-Object { $_ }) -join "`n"
}
if (-not ((Has $old "paths") -and (Has $old.paths "playbook"))) {
    $playbook = Ask "谈判手册（Markdown 文件）的完整路径，没有就回车用模板" (Join-Path $Data "谈判手册.md")
    if (-not (Test-Path $playbook)) { Copy-Item (Join-Path $App "templates\谈判手册.example.md") $playbook }
    $answers.paths.playbook = $playbook
}
if (-not ((Has $old "paths") -and (Has $old.paths "koc_knowledge"))) {
    $knowledge = Join-Path $Data "KOC机器人知识.md"
    if (-not (Test-Path $knowledge)) { Copy-Item (Join-Path $App "templates\KOC机器人知识.example.md") $knowledge }
    $answers.paths.koc_knowledge = $knowledge
}
if (-not ((Has $old "paths") -and (Has $old.paths "sheet_workbook"))) {
    $workbook = Ask "KOL 总表（xlsx）的完整路径，不用总表就回车" ""
    if ($workbook -and -not (Test-Path $workbook)) {
        Say "  总表不存在，生成一张空表（需要本机装有 Excel）…"
        $r = Run-Native { & $Py (Join-Path $App "sheet\build.py") --out $workbook }
        if ($r.Code -ne 0) { Warn "  空表没生成成功，KOL 总表联动先关闭：$($r.Out)"; $workbook = "" }
    }
    if ($workbook) {
        $answers.paths.sheet_workbook = $workbook
        $answers.paths.sheet_tool = Join-Path $App "sheet\kol.py"
        $answers.paths.sheet_python = $Py
    }
}
if (-not ((Has $old "paths") -and (Has $old.paths "autoreply_record"))) { $answers.paths.autoreply_record = Join-Path $Data "自动回复记录.md" }
if ($Repo) { $answers.update.repo = $Repo }
if ($Mirror) { $answers.update.mirror = $Mirror }
if ($Feedback) { $answers.feedback.popo_webhook = $Feedback }
$answersFile = Join-Path $env:TEMP ("kolmail-answers-" + [guid]::NewGuid().ToString("N") + ".json")
try {
    [IO.File]::WriteAllText($answersFile, ($answers | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    $r = Run-Native { & $Py -I (Join-Path $App "tools\merge_settings.py") $settingsPath $answersFile }
    if ($r.Code -ne 0) { throw "写设置失败：$($r.Out)" }
} finally { Remove-Item $answersFile -ErrorAction SilentlyContinue }
$autoreply = Join-Path $Data "autoreply.json"
if (-not (Test-Path $autoreply)) { Copy-Item (Join-Path $App "autoreply.example.json") $autoreply }

# ---------------------------------------------------------------- 7. 后台服务
Say "[7/8] 注册开机自动启动…"
$run = Join-Path $App "service\run.ps1"
$action = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$run`" -Python `"$Py`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$opts = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "kol-mail" -Action $action -Trigger $trigger -Settings $opts -Force | Out-Null

# ---------------------------------------------------------------- 8. 桌面图标，启动
Say "[8/8] 放桌面图标并启动…"
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "KOL Mail.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $App "start-kol-mail.cmd"
$s.WorkingDirectory = $App
$s.Save()
Start-ScheduledTask -TaskName "kol-mail"
Start-Process (Join-Path $App "start-kol-mail.cmd")
Say "装好了。以后双击桌面上的“KOL Mail”就能打开；每天第一次打开会自动检查更新。"
Say "下班 AI 回复需要在页面上方“手机审稿”里绑定你自己的 Telegram 机器人。"
