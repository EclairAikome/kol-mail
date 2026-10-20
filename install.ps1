# KOL Mail 一键安装（Windows 10/11，PowerShell 5.1 即可）
#
# 用法（维护者会在 POPO 群里发带参数的完整命令）：
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Repo owner/kol-mail -Feedback <反馈地址>
#
# 公司邮箱固定是 @lootbar.com（Amazon WorkMail），收发信服务器已经是默认值，不用再问；换公司时用 -Domain、
# -ImapHost、-SmtpHost、-Company 改。维护者在本机试装时加 -Source <源码目录> -Dir <别的目录> -NoService，
# 这样不注册开机任务、不放桌面图标、不启动服务（真服务会在下班时间自动回信）。
#
# 做的事：找到或安装 Python，下载最新版 KOL Mail，装依赖，配置你的邮箱和 AI（Claude Code 或 CodeMaker），
# 写好你的签名，注册开机自动启动的后台任务，在桌面放一个“KOL Mail”图标。重复运行是安全的，已有的设置会保留。
# 邮箱密码在一个小窗口里输入（可以 Ctrl+V 粘贴），经标准输入直接交给邮件组件存进 Windows 凭据管理器，
# 这个脚本不显示、不保存，也不把它放进命令行。

param(
    [string]$Repo = "",
    [string]$Feedback = "",
    [string]$Domain = "lootbar.com",
    [string]$Company = "LootBar",
    [string]$ImapHost = "imap.mail.us-east-1.awsapps.com",
    [string]$SmtpHost = "smtp.mail.us-east-1.awsapps.com",
    [string]$PipIndex = "",
    [string]$Mirror = "",  # GitHub 连不上时用的 https 代理前缀
    [string]$Dir = (Join-Path $env:LOCALAPPDATA "kol-mail"),
    [string]$Source = "",  # 本地源码目录（维护者测试用）；留空则从 GitHub 下载最新版
    [switch]$NoService     # 维护者测试用：不注册开机任务、不放桌面图标、不启动
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
# 最新版本号从 github.com/<仓库>/releases/latest 的跳转地址里读。GitHub API 每个 IP 每小时只给 60 次匿名查询，
# 公司网络大家共用一个出口 IP，经常早就用完了（报 API rate limit exceeded），所以 API 只在跳转读不到时兜底。
function Get-LatestTag {
    try {
        $req = [Net.HttpWebRequest]::Create((Gh "https://github.com/$Repo/releases/latest"))
        $req.Method = "HEAD"; $req.AllowAutoRedirect = $false; $req.Timeout = 30000; $req.UserAgent = "kol-mail-installer"
        $resp = $req.GetResponse()
        try { $location = "$($resp.Headers['Location'])" } finally { $resp.Close() }
        if ($location -match '/releases/tag/(v\d+\.\d+\.\d+)/?$') { return $Matches[1] }
    } catch { }
    try {
        $rel = Invoke-RestMethod -Headers @{ Accept = "application/vnd.github+json" } (Gh "https://api.github.com/repos/$Repo/releases/latest")
        if ("$($rel.tag_name)" -match '^v\d+\.\d+\.\d+$') { return "$($rel.tag_name)" }
    } catch { }
    throw "查不到 KOL Mail 的最新版本。多半是网络连不上 GitHub，换个网络（比如手机热点）再运行一次安装；还不行就把这个窗口截图发给维护者。"
}

# 邮箱密码用一个小窗口来问。控制台的隐藏输入在不少电脑上 Ctrl+V 和右键都粘贴不进去（只送进去一个控制字符），
# 而邮箱密码长、乱，基本只能粘贴，同事就卡在这一步。窗口里的密码框可以正常粘贴。取消返回 $null。
function Read-MailPassword($email, $note = "") {
    if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne "STA") {  # 窗口的粘贴要 STA；powershell.exe 默认就是
        $secure = Read-Host "$email 的邮箱密码" -AsSecureString
        return (New-Object Net.NetworkCredential("", $secure)).Password.Trim()
    }
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object Windows.Forms.Form -Property @{
        Text = "KOL Mail 邮箱密码"; FormBorderStyle = "FixedDialog"; MaximizeBox = $false; MinimizeBox = $false
        StartPosition = "CenterScreen"; TopMost = $true; AutoSize = $true; AutoSizeMode = "GrowAndShrink"
        Font = New-Object Drawing.Font("Microsoft YaHei UI", 10); Padding = New-Object Windows.Forms.Padding(12)
    }
    $panel = New-Object Windows.Forms.FlowLayoutPanel -Property @{ FlowDirection = "TopDown"; AutoSize = $true; WrapContents = $false }
    $panel.Controls.Add((New-Object Windows.Forms.Label -Property @{ Text = "$email 的密码`n先点一下下面的框，再 Ctrl+V 粘贴"; AutoSize = $true }))
    if ($note) {
        $panel.Controls.Add((New-Object Windows.Forms.Label -Property @{
            Text = $note; AutoSize = $true; ForeColor = [Drawing.Color]::Firebrick; MaximumSize = New-Object Drawing.Size(420, 0) }))
    }
    $box = New-Object Windows.Forms.TextBox -Property @{ UseSystemPasswordChar = $true; Width = 420 }
    $panel.Controls.Add($box)
    $buttons = New-Object Windows.Forms.FlowLayoutPanel -Property @{ FlowDirection = "RightToLeft"; AutoSize = $true; Width = 420 }
    $cancel = New-Object Windows.Forms.Button -Property @{ Text = "取消"; DialogResult = "Cancel"; AutoSize = $true }
    $ok = New-Object Windows.Forms.Button -Property @{ Text = "确定"; DialogResult = "OK"; AutoSize = $true }
    $buttons.Controls.AddRange(@($cancel, $ok))
    $panel.Controls.Add($buttons)
    $form.Controls.Add($panel)
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel
    $form.Add_Shown({ $form.Activate(); $box.Focus() })
    try {
        $result = $form.ShowDialog()
        # 窗口没抢到焦点时，粘贴会落进控制台的输入缓冲，下一个问题会把密码明文显示出来：清掉
        try { $Host.UI.RawUI.FlushInputBuffer() } catch { }
        if ($result -ne [Windows.Forms.DialogResult]::OK) { return $null }
        return $box.Text.Trim()
    } finally {
        $form.Dispose()
    }
}

# 按 Windows 命令行的规则给参数加引号（PowerShell 5.1 的 ProcessStartInfo 只收一整串参数）
function Join-NativeArgs([string[]]$items) {
    ($items | ForEach-Object {
        if ($_ -and $_ -notmatch '[\s"]') { $_ } else { '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
    }) -join " "
}

# 把密码（每行一个）经标准输入交给邮件组件，不经过命令行参数，两头都按 UTF-8（不带 BOM）。以前交给 PowerShell 的管道写，
# 5.1 不认改过的 $OutputEncoding，非英文字符变成 ?；.NET 启动子进程时还会按控制台输入编码先写一个 BOM（U+FEFF），
# 在 chcp 65001 的窗口里那就是带 BOM 的 UTF-8，第一行密码开头多一个看不见的字符。存进去的密码和真密码对不上，
# 服务器只会回“Authentication or provider policy rejected”。所以这里临时把控制台输入编码换成不带 BOM 的 UTF-8，
# 自己写标准输入。
function Invoke-McpWithSecret([string[]]$secrets, [string[]]$arguments) {
    $utf8 = New-Object Text.UTF8Encoding $false
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Mcp
    $psi.Arguments = Join-NativeArgs $arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"  # Python 默认按系统代码页读管道
    $savedIn = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = $utf8
        $p = [Diagnostics.Process]::Start($psi)
    } finally {
        [Console]::InputEncoding = $savedIn
    }
    try {
        $out, $err = $p.StandardOutput.ReadToEndAsync(), $p.StandardError.ReadToEndAsync()
        $stdin = New-Object IO.StreamWriter($p.StandardInput.BaseStream, $utf8)
        try { foreach ($s in $secrets) { $stdin.Write("$s`n") } } finally { $stdin.Close() }
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Out = ((@($out.Result, $err.Result) | Where-Object { $_ }) -join "`n").Trim() }
    } finally {
        $p.Dispose()
    }
}

# 测收信和发信，返回没通过的那些（"收信：<邮件组件的一行说明>"）；都通过时什么也不返回。
function Test-MailAccount($account) {
    Say "  正在测试收信和发信，网络慢的话要等一两分钟…"
    foreach ($role in "incoming", "outgoing") {
        $t = Run-Native { & $Mcp account test $account $role }
        if ($t.Code -ne 0) { "$(if ($role -eq 'incoming') { '收信' } else { '发信' })：$(($t.Out -split "`n")[0])" }
    }
}

# 邮件组件把失败分成认证被拒、凭据丢失、网络/TLS 不通、超时几类；只有前两类重输密码有用
function Test-PasswordProblem($failed) {
    return ($failed -join "`n") -match "Authentication or provider policy rejected|Credential is unavailable"
}

function Write-ConnectionProblem($failed) {
    Warn "  连不上邮件服务器。这不是密码的问题，多半是网络（比如没连公司 VPN）或服务器地址不对："
    Warn "    $($failed -join "`n    ")"
    Warn "  网络好了以后重新运行本脚本；还是不行就把上面这几行发给维护者。"
}

# 问密码、存进邮件组件、测收发信；密码没通过验证就把服务器的回复显示在窗口里重新问，最多 3 次。
# 网络/地址问题不再问密码（重输也没用）。$addArgs 非空表示账户还不存在，第一次用 account add 建；
# 否则只换密码。返回是否测试通过。
function Set-MailPassword($account, $email, [string[]]$addArgs) {
    $note = ""
    $stored = -not $addArgs
    for ($try = 1; $try -le 3; $try++) {
        $pw = Read-MailPassword $email $note
        if ($null -eq $pw) { break }
        if (-not $pw) { $note = "密码是空的，请重新输入。"; continue }
        if (-not $stored) {
            $r = Invoke-McpWithSecret @($pw, $pw) ($addArgs + "--password-stdin")
            if ($r.Code -ne 0) { throw "添加邮箱失败：$($r.Out)`n检查服务器地址后重新运行本脚本。" }
            $stored = $true
        } else {
            foreach ($role in "incoming", "outgoing") {
                $r = Invoke-McpWithSecret @($pw) @("account", "set-secret", $account, $role, "--password-stdin")
                if ($r.Code -ne 0) { throw "保存邮箱密码失败：$($r.Out)" }
            }
        }
        $pw = $null
        $failed = @(Test-MailAccount $account)
        if (-not $failed) { return $true }
        if (-not (Test-PasswordProblem $failed)) { Write-ConnectionProblem $failed; return $false }
        $note = "密码没通过邮件服务器的验证，请重新输入（第 $($try + 1) 次，共 3 次）。如果确定密码没错，" +
            "可能要用邮箱的客户端专用密码，或者邮箱还没开通收发信（IMAP/SMTP），请问维护者。`n" + ($failed -join "`n")
    }
    if (-not $stored) { throw "没有输入邮箱密码，邮箱没有配置。重新运行本脚本即可。" }
    return $false
}

if ($Mirror -and -not $Mirror.StartsWith("https://")) { throw "-Mirror 必须是 https 地址" }

$App = Join-Path $Dir "app"
$Venv = Join-Path $Dir "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Data = Join-Path $App "data"
New-Item -ItemType Directory -Force $Dir | Out-Null

# 重复安装时先停掉正在运行的旧版本，不然 pip 换不了被占用的文件
if (-not $NoService -and (Get-ScheduledTask -TaskName "kol-mail" -ErrorAction SilentlyContinue)) {
    Stop-ScheduledTask -TaskName "kol-mail" -ErrorAction SilentlyContinue  # 只停任务本身，不会结束它启动的 python
    # venv 里的 python.exe 在 Windows 上只是个启动器，真正跑程序、占着端口的是它启动的基础 Python 子进程：两个都要停
    $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*app.py*" })
    $launchers = @($running | Where-Object { $_.ExecutablePath -like "$Venv*" } | ForEach-Object { $_.ProcessId })
    $running | Where-Object { $launchers -contains $_.ProcessId -or $launchers -contains $_.ParentProcessId } |
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
        $tag = Get-LatestTag
        $zip = Join-Path $staging "src.zip"
        Invoke-WebRequest -UseBasicParsing (Gh "https://codeload.github.com/$Repo/zip/refs/tags/$tag") -OutFile $zip
        Expand-Archive $zip (Join-Path $staging "x")
        $src = (Get-ChildItem (Join-Path $staging "x") -Directory | Select-Object -First 1).FullName
        Say "  最新版 $tag"
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
while ($email -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") {
    $email = Ask $(if ($Domain) { "你的工作邮箱（只填 @$Domain 前面的部分就行）" } else { "你的工作邮箱" })
    if ($email -and $Domain -and $email -notmatch "@") { $email = "$email@$Domain" }
}
$email = $email.ToLower()
$account = if ((Has $old "mail") -and (Has $old.mail "account_name")) { $old.mail.account_name } else { ($email.Split("@")[0] -replace "[^A-Za-z0-9_-]", "") }
$fullName = if ((Has $old "identity") -and (Has $old.identity "name")) { $old.identity.name } else { "" }
# 默认用邮箱前缀当名字：eclair -> Eclair，john.doe -> John Doe
$guess = (($email.Split("@")[0] -split "[._-]+") | Where-Object { $_ } |
    ForEach-Object { $_.Substring(0, 1).ToUpper() + $_.Substring(1) }) -join " "
while (-not $fullName) { $fullName = Ask "对外署名（英文名）" $guess }
$accounts = (Run-Native { & $Mcp account list }).Out
if ($accounts -notmatch "(?m)\b$([regex]::Escape($account))\b") {
    if (-not $ImapHost) { $ImapHost = Ask "收信服务器（IMAP）" }
    if (-not $SmtpHost) { $SmtpHost = Ask "发信服务器（SMTP）" }
    Say "  马上会弹出一个小窗口，在里面输入邮箱密码（可以 Ctrl+V 粘贴）。密码由邮件组件存进 Windows 凭据管理器。"
    $mailOk = Set-MailPassword $account $email @("account", "add", $account, "--email", $email, "--full-name", $fullName,
        "--imap-host", $ImapHost, "--smtp-host", $SmtpHost, "--imap-user", $email, "--smtp-user", $email)
} else {
    $failed = @(Test-MailAccount $account)
    $mailOk = -not $failed
    if ($failed -and (Test-PasswordProblem $failed)) {
        # 已经配过但密码不对，多半是上次粘贴没成功：重新运行本脚本就能改
        Warn "  邮箱已经配置过，但密码没通过验证，弹出窗口重新输入一次。"
        $mailOk = Set-MailPassword $account $email @()
    } elseif ($failed) {
        Write-ConnectionProblem $failed
    }
}
if ($mailOk) { Say "  邮箱连接测试通过" } else { Warn "  邮箱还没连通，KOL Mail 暂时收不到信。处理好后重新运行本脚本即可，已有的设置都会保留。" }
# 回信、附件、移动邮件要用到的权限
$policy = (Run-Native { & $Mcp config policy }).Out
$rev = [regex]::Match($policy, "(?m)^revision=(\d+)").Groups[1].Value
$needSend = $policy -notmatch "(?m)^allowed_mutations=.*\bsend\b"
$needAtt = $policy -notmatch "(?m)^enable_attachment_download=true"
# 收件人白名单为空时，回信会报 Recipient(s) not in allowlist
$needRecipients = $policy -match "(?m)^allowed_recipients=none\b"
if ($rev -and ($needSend -or $needAtt -or $needRecipients)) {
    # '*' 必须和参数名连写：分开传的话，mcp-email-server 在 Windows 上会把它展开成当前目录的文件名
    $r = Run-Native { & $Mcp config update-policy --expected-revision $rev --enable-attachment-download --enable-attachment-content `
        --allowed-mutations "draft,organize,send,append" '--allowed-recipients=*' }
    if ($r.Code -ne 0) { Warn "  没能打开回信和附件权限：$($r.Out)" }
}

# ---------------------------------------------------------------- 5. AI
Say "[5/8] 连接 AI…"
$cm = Join-Path $env:USERPROFILE ".codemaker\bin\codemaker.exe"
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
$haveCm = (Test-Path $cm) -or [bool](Get-Command codemaker -ErrorAction SilentlyContinue)
$default = if ((Has $old "llm") -and (Has $old.llm "backend")) { $old.llm.backend } elseif ($haveCm) { "codemaker" } else { "claude" }
Say ("  检测到：" + ($(if ($haveCm) { "CodeMaker " } else { "" }) + $(if ($claude) { "Claude Code" } else { "" })))
$backend = ""
while ($backend -ne "codemaker" -and $backend -ne "claude") { $backend = (Ask "用哪个 AI？输入 codemaker 或 claude" $default).ToLower() }

# 用刚装好的这版 KOL Mail 自己的 llm.py 来检测：找命令行、看版本、用翻译和起草两个模型各真调一次。
# 新旧版本的 llm.py 都能这样用，所以这个脚本（总是从 main 下载）也能装旧版程序。
$ClaudeMin = [version]"2.1.280"  # 起草默认用的 Opus 5.5 要求的最低 Claude Code 版本
$script:ClaudeBin = ""           # 这次装的原生 Claude Code，或者验证通过的那个；写进设置，后台服务就用同一个
$script:CodemakerBin = ""
$script:LoginOpened = $false
# CodeMaker 没有 Claude 模型。旧版程序（v0.1.1 及以前）给 CodeMaker 的默认还是 Claude 模型，这里替它定下来
$CmModels = [ordered]@{ translate = "gpt-5.6-terra"; draft = "gpt-6.1-sol" }
$appDefaultsCm = [bool](Select-String -Quiet -SimpleMatch -Pattern 'gpt-6.1-sol' -LiteralPath (Join-Path $App "llm.py"))
$oldModels = (Has $old "llm") -and (Has $old.llm "models") -and @($old.llm.models.PSObject.Properties).Count
$pinCmModels = ($backend -eq "codemaker") -and -not $appDefaultsCm -and -not $oldModels
$AiCheck = Join-Path $env:TEMP ("kolmail-aicheck-" + [guid]::NewGuid().ToString("N") + ".py")
$aiCheckCode = @'
import asyncio, json, os, subprocess, sys, tempfile, threading
from pathlib import Path


def watchdog():  # whatever hangs, the installer gets an answer and the colleague can retry or skip
    print(json.dumps({"errors": [["error", "检测超过 11 分钟没有结束"]]}), flush=True)  # ASCII escapes, like below
    os._exit(1)


threading.Timer(660, watchdog).start()  # the slowest honest run: two model lookups (120 s) and two calls (180 s)

app, backend, mode = sys.argv[1], sys.argv[2], sys.argv[3]
claude_bin = sys.argv[4] if len(sys.argv) > 4 else ""
sys.path.insert(0, app)
import llm

llm_cfg = {}
try:
    llm_cfg = json.loads((Path(app) / "data" / "settings.json").read_text(encoding="utf-8-sig")).get("llm") or {}
except (OSError, ValueError):
    pass
s = llm.Settings(backend=backend, models=llm_cfg.get("models") or {},
                 claude_bin=claude_bin or llm_cfg.get("claude_bin") or "",
                 codemaker_bin=llm_cfg.get("codemaker_bin") or "",
                 workdir=Path(tempfile.gettempdir()) / "kol-mail-install-check")
out = {"binary": s.binary(), "models": s.models, "errors": []}
if mode == "version":
    if out["binary"]:
        try:
            r = subprocess.run([out["binary"], "--version"], capture_output=True, text=True, timeout=60)
            out["version"] = (r.stdout or r.stderr).strip()[:200]
        except (OSError, subprocess.SubprocessError):
            out["version"] = ""
    print(json.dumps(out))
    sys.exit(0)
schema = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]}
for role in llm.ROLES:
    try:
        asyncio.run(llm.complete(s, 'Answer with the JSON object {"ok": "ok"}.', "ping", schema, role, 180))
    except llm.LLMUnavailable as e:
        out["errors"].append(["unavailable", f"{s.models[role]}: {e}"])
    except Exception as e:
        out["errors"].append(["error", f"{s.models[role]}: {e}"])
print(json.dumps(out), flush=True)
# Leave at once: interpreter shutdown must never be what keeps the installer waiting (it once hung for good in
# asyncio's clean-up on Python 3.12 for Windows).
os._exit(0 if not out["errors"] else 2 if any(k == "unavailable" for k, _ in out["errors"]) else 1)
'@

function Invoke-AiCheck($mode) {
    $saved = $env:KOL_TRANSLATE_MODEL, $env:KOL_DRAFT_MODEL
    if ($pinCmModels) { $env:KOL_TRANSLATE_MODEL, $env:KOL_DRAFT_MODEL = $CmModels.translate, $CmModels.draft }
    try { $r = Run-Native { & $Py -I $AiCheck $App $backend $mode $script:ClaudeBin } }
    finally { $env:KOL_TRANSLATE_MODEL, $env:KOL_DRAFT_MODEL = $saved }
    $line = @($r.Out -split "`n" | Where-Object { $_.TrimStart().StartsWith("{") }) | Select-Object -Last 1
    $data = $null
    if ($line) { try { $data = $line | ConvertFrom-Json } catch { } }
    return @{ Code = $r.Code; Data = $data; Out = $r.Out }
}

# 回车 = 重新检测，s = 跳过
function Wait-Retry($what) {
    $a = Ask "${what}按回车重新检测（输入 s 跳过，以后在页面上点“重新检测 AI”）"
    return ($a -notmatch "^[sS]$")
}

function Install-ClaudeCode {
    Say "  正在用官方安装器安装最新版 Claude Code（装在你自己的用户目录）…"
    $r = Run-Native { powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex" }
    $exe = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if ($r.Code -eq 0 -and (Test-Path $exe)) {
        $script:ClaudeBin = $exe
        $env:Path = (Split-Path $exe) + ";" + $env:Path
        Say "  装好了：$exe"
    } else {
        Warn "  安装没成功：$(($r.Out -split "`n" | Select-Object -Last 3) -join ' ')"
    }
}

# 在新窗口里登录：登录要开浏览器，可能还要粘贴验证码，安装窗口不适合
# 每次运行最多自动弹一次，之后只提示（网络不通等别的原因也会走到这里，不能一直弹窗）
function Open-AiLogin($bin) {
    if ($script:LoginOpened) { return }
    $script:LoginOpened = $true
    $quoted = "'" + ($bin -replace "'", "''") + "'"  # 用户名里可能有撇号
    if ($backend -eq "codemaker") {
        Start-Process powershell.exe -ArgumentList @("-NoExit", "-Command", "& $quoted auth login")
    } else {
        Start-Process powershell.exe -ArgumentList @("-NoExit", "-Command",
            "Write-Host '输入 /login 回车，在浏览器里登录；登录好后输入 /exit，再回到安装窗口' -ForegroundColor Yellow; & $quoted")
    }
}

function Connect-AI {
    $label = if ($backend -eq "codemaker") { "CodeMaker" } else { "Claude Code" }
    while ($true) {
        $v = Invoke-AiCheck "version"
        if (-not $v.Data) {  # 检测脚本自己出错了，不是没装
            Warn "  检测 $label 时出错："
            ($v.Out -split "`n" | Select-Object -Last 3) | ForEach-Object { Warn "    $_" }
            if (-not (Wait-Retry "  ")) { return $false }
            continue
        }
        $bin = $v.Data.binary
        if (-not $bin) {
            if ($backend -eq "claude") {
                if ((Ask "  没找到 Claude Code。现在用官方安装器装上吗？(Y/n)" "Y") -match "^[Yy]") { Install-ClaudeCode; continue }
            } else {
                Warn "  没找到 CodeMaker 命令行。请先按公司指引安装 CodeMaker CLI。"
            }
            if (-not (Wait-Retry "  装好后")) { return $false }
            continue
        }
        if ($backend -eq "claude") {
            $m = [regex]::Match("$($v.Data.version)", "\d+\.\d+\.\d+")
            if ($m.Success -and [version]$m.Value -lt $ClaudeMin) {
                Warn "  Claude Code 是 $($m.Value)，太旧了：起草用的模型要 $ClaudeMin 以上。"
                if ((Ask "  现在用官方安装器装最新版吗？(Y/n)" "Y") -match "^[Yy]") { Install-ClaudeCode; continue }
                if (-not (Wait-Retry "  自己升级好后")) { return $false }
                continue
            }
        } elseif ((Run-Native { & $bin quota }).Code -ne 0) {
            Warn "  CodeMaker 好像还没登录（也可能是网络不通，比如没连公司 VPN）。"
            if (-not $script:LoginOpened) { Say "  马上打开一个新窗口运行 codemaker auth login，登录好后回到这个窗口。" }
            else { Say "  需要登录的话，在 PowerShell 里运行 codemaker auth login。" }
            Open-AiLogin $bin
            if (-not (Wait-Retry "  登录好后")) { return $false }
            continue
        }
        Say "  用 $($v.Data.models.translate)（翻译）和 $($v.Data.models.draft)（起草）各试一次，要半分钟到一分钟…"
        $t = Invoke-AiCheck "smoke"
        if ($t.Code -eq 0 -and $t.Data) {
            Say "  AI 连接测试通过"
            # 后台服务的环境（PATH）和这个窗口可能不一样：把验证过的这个命令行写进设置，跑的就是验过的
            if ($backend -eq "claude") { $script:ClaudeBin = $bin } else { $script:CodemakerBin = $bin }
            return $true
        }
        $errs = if ($t.Data -and $t.Data.errors) { @($t.Data.errors | ForEach-Object { $_[1] }) } else { @(($t.Out -split "`n") | Select-Object -Last 3) }
        Warn "  $label 还不能用："
        $errs | ForEach-Object { Warn "    $_" }
        if (($errs -join "`n") -match "没登录|登录已过期|authenticat|\b401\b|not logged") {
            if (-not $script:LoginOpened) { Say "  马上打开一个新窗口让你登录 $label，登录好后回到这个窗口。" }
            else { Say "  需要登录的话，在 PowerShell 里运行 $(if ($backend -eq 'codemaker') { 'codemaker auth login' } else { 'claude，再输入 /login' })。" }
            Open-AiLogin $bin
        }
        if (-not (Wait-Retry "  处理好后")) { return $false }
    }
}

try {
    [IO.File]::WriteAllText($AiCheck, $aiCheckCode, (New-Object Text.UTF8Encoding $false))
    $aiOk = Connect-AI
} finally { Remove-Item $AiCheck -ErrorAction SilentlyContinue }
if (-not $aiOk) { Warn "  AI 还没连上：翻译和起草暂时用不了。处理好后在页面上方点“重新检测 AI”，或者重新运行本脚本。" }

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
if ($script:ClaudeBin) { $answers.llm.claude_bin = $script:ClaudeBin }
if ($script:CodemakerBin) { $answers.llm.codemaker_bin = $script:CodemakerBin }
if ($pinCmModels) { $answers.llm.models = $CmModels }
# 新版程序第一次打开页面时，会请同事自己写签名和“我的谈判方式”（每人签名格式和打法都不一样）；
# 旧版程序（v0.1.1 及以前）没有这一步，还是在这里把签名问全，否则签名只有名字。
$setupInApp = [bool](Select-String -Quiet -SimpleMatch -Pattern 'id="setupDlg"' -LiteralPath (Join-Path $App "static\index.html"))
if (-not ((Has $old "identity") -and (Has $old.identity "company"))) { $answers.identity.company = $Company }
if (-not ((Has $old "identity") -and (Has $old.identity "signature"))) {
    if ($setupInApp) {
        $answers.identity.signature = $fullName
    } else {
        $title = Ask "职位（英文）" "Marketing Manager"
        $extra = Ask "签名里额外的一行（比如 Discord: xxx，没有就回车）"
        $site = Ask "签名里的网址（没有就回车）" $(if ($Domain) { "https://$Domain/" } else { "" })
        $answers.identity.title = $title
        $answers.identity.signature = (@($fullName, "$title | $Company", $extra, $site) | Where-Object { $_ }) -join "`n"
    }
}
if (-not ((Has $old "paths") -and (Has $old.paths "playbook"))) {
    $playbook = Join-Path $Data "谈判手册.md"
    if (-not $setupInApp) { $playbook = Ask "谈判手册（Markdown 文件）的完整路径，没有就回车用模板" $playbook }
    if (-not (Test-Path $playbook)) { Copy-Item (Join-Path $App "templates\谈判手册.example.md") $playbook }
    $answers.paths.playbook = $playbook
}
if (-not ((Has $old "paths") -and (Has $old.paths "koc_knowledge"))) {
    $knowledge = Join-Path $Data "KOC机器人知识.md"
    if (-not (Test-Path $knowledge)) { Copy-Item (Join-Path $App "templates\KOC机器人知识.example.md") $knowledge }
    $answers.paths.koc_knowledge = $knowledge
}
# KOL 总表：同事自己的 xlsx，格式各不相同。这里只记下文件；第一次打开页面时请同事核对各列对应什么，
# 对好之前不往表里写。新建的表是程序自己的简单模板，列对应直接定好。
if (-not ((Has $old "paths") -and (Has $old.paths "sheet_workbook"))) {
    Say "  KOL 总表：连上你自己的 xlsx 总表后，打开邮件会显示 KOL 的频道链接，收发信的进展会自动记进表里（只写对得上的列，不改你的格式）。"
    $workbook = ""
    while ($true) {
        $ans = Ask "总表文件的完整路径（可以把文件拖进这个窗口），输入 new 新建一张，不用就直接回车（以后在页面上方的 KOL 总表按钮里也能接）" ""
        $ans = "$ans".Trim().Trim('"').Trim("'")
        if (-not $ans) { break }
        if ($ans -match '^https?://docs\.popo\.') {
            Say "  POPO 在线表格要先在这台电脑上装好并登录 popo-cli。装好 KOL Mail 后，在页面上方的“KOL 总表”里粘贴这个链接连接。"
            break
        }
        $create = $ans -eq "new"
        if ($create) { $ans = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "KOL总表.xlsx" }
        if ($ans -notmatch '\.xlsx$') { Warn "  只支持 .xlsx 格式（.xls、.csv 请先在 Excel 里另存为 .xlsx）"; continue }
        if (Test-Path -LiteralPath $ans) {
            if ($create) { Warn "  $ans 已经有文件了，直接用它" }
            $workbook = $ans
            Say "  第一次打开页面时会请你核对表里各列对应什么，对好之后才开始同步。"
            break
        }
        if (-not $create -and (Ask "  这个文件不存在。输入 y 在这里新建一张，直接回车重新输入" "") -ne "y") { continue }
        $r = Run-Native { & $Py -I (Join-Path $App "sheet\usersheet.py") create --path $ans }
        $line = (($r.Out -split "`n") | Where-Object { $_.Trim().StartsWith("{") } | Select-Object -Last 1)
        $made = $null
        if ($line) { try { $made = $line | ConvertFrom-Json } catch { } }
        if ($r.Code -eq 0 -and $made -and $made.map) {
            $workbook = $made.path
            $answers.sheet_map = $made.map
            Say "  新建了 $workbook，已经连上"
            break
        }
        Warn "  新建失败：$(if ($made -and $made.error) { $made.error } else { $r.Out })"
    }
    if ($workbook) {
        $answers.paths.sheet_workbook = $workbook
        $answers.paths.sheet_tool = Join-Path $App "sheet\usersheet.py"
        $answers.paths.sheet_python = $Py
    }
}
if (-not ((Has $old "paths") -and (Has $old.paths "autoreply_record"))) { $answers.paths.autoreply_record = Join-Path $Data "自动回复记录.md" }
if ($Repo) { $answers.update.repo = $Repo }
if ($Mirror) { $answers.update.mirror = $Mirror }
if ($Feedback) { $answers.feedback.popo_webhook = $Feedback }
elseif (-not ((Has $old "feedback") -and (Has $old.feedback "popo_webhook"))) {
    Warn "  安装命令里没有反馈地址（-Feedback），页面右下角的“反馈”按钮会是灰的。需要的话问维护者要完整的安装命令。"
}
$answersFile = Join-Path $env:TEMP ("kolmail-answers-" + [guid]::NewGuid().ToString("N") + ".json")
try {
    [IO.File]::WriteAllText($answersFile, ($answers | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    $r = Run-Native { & $Py -I (Join-Path $App "tools\merge_settings.py") $settingsPath $answersFile }
    if ($r.Code -ne 0) { throw "写设置失败：$($r.Out)" }
} finally { Remove-Item $answersFile -ErrorAction SilentlyContinue }
$autoreply = Join-Path $Data "autoreply.json"
if (-not (Test-Path $autoreply)) { Copy-Item (Join-Path $App "autoreply.example.json") $autoreply }

if ($NoService) {
    Say "[7/8] [8/8] -NoService：不注册开机任务、不放桌面图标、不启动。程序装在 $App"
    return
}

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
if ($setupInApp) { Say "第一次打开页面时，会请你写自己的签名和“我的谈判方式”，AI 出策略和起草都照它来。" }
Say "下班 AI 回复需要在页面上方“手机审稿”里绑定你自己的 Telegram 机器人。"
