#!/bin/bash
# KOL Mail 一键安装（macOS 13 以上，Apple 芯片和 Intel 都行）
#
# 用法（维护者会在 POPO 群里发带参数的完整命令，复制到“终端”里回车）：
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/kol-mail/main/install.sh)" -- \
#       --repo <owner>/kol-mail --imap-host <收信服务器> --smtp-host <发信服务器> --feedback <反馈地址>
#
# 做的事：找到或安装 Python，下载最新版 KOL Mail，装依赖，配置你的邮箱和 AI（Claude Code 或 CodeMaker），
# 写好你的签名，注册开机自动启动的后台服务（launchd），在“应用程序”里放一个“KOL Mail”。重复运行是安全的，
# 已有的设置会保留。邮箱密码只在邮件组件自己的提示里输入，这个脚本看不到也不保存，密码存进 Mac 的钥匙串。

set -eo pipefail

REPO=""; FEEDBACK=""; IMAP_HOST=""; SMTP_HOST=""; PIP_INDEX=""; MIRROR=""; SOURCE=""
DIR="$HOME/Library/Application Support/kol-mail"
# 维护者测试用（云主机上无人值守安装）：不提问，用下面的参数和默认值
YES=0; ASK_EMAIL=""; ASK_NAME=""; ASK_COMPANY=""; ASK_BACKEND=""; PASSWORD_STDIN=0; NO_OPEN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --feedback) FEEDBACK="$2"; shift 2 ;;
        --imap-host) IMAP_HOST="$2"; shift 2 ;;
        --smtp-host) SMTP_HOST="$2"; shift 2 ;;
        --pip-index) PIP_INDEX="$2"; shift 2 ;;
        --mirror) MIRROR="$2"; shift 2 ;;   # GitHub 连不上时用的 https 代理前缀
        --dir) DIR="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;   # 本地源码目录（维护者测试用）；留空则从 GitHub 下载最新版
        --yes) YES=1; shift ;;
        --email) ASK_EMAIL="$2"; shift 2 ;;
        --name) ASK_NAME="$2"; shift 2 ;;
        --company) ASK_COMPANY="$2"; shift 2 ;;
        --backend) ASK_BACKEND="$2"; shift 2 ;;
        --password-stdin) PASSWORD_STDIN=1; shift ;;
        --no-open) NO_OPEN=1; shift ;;
        *) echo "不认识的参数：$1" >&2; exit 2 ;;
    esac
done

LABEL="local.kolmail"
APP="$DIR/app"
VENV="$DIR/venv"
PY="$VENV/bin/python"
DATA="$APP/data"
LOG="$DIR/server.log"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"

say() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
# ask "问题" "默认值"：结果在 $REPLY。用 /dev/tty 读，所以“curl | bash”的写法也能问。
ask() {
    local hint=""
    [ -n "$2" ] && hint="（直接回车用：$2）"
    if [ "$YES" = 1 ]; then REPLY="$2"; return; fi
    printf '%s%s ' "$1" "$hint" > /dev/tty
    IFS= read -r REPLY < /dev/tty || REPLY=""
    REPLY="$(printf '%s' "$REPLY" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$REPLY" ] && REPLY="$2"
    return 0
}
gh_url() { if [ -n "$MIRROR" ]; then printf '%s/%s' "${MIRROR%/}" "$1"; else printf '%s' "$1"; fi; }

[ "$(uname -s)" = "Darwin" ] || die "这个脚本只用于 Mac。Windows 请用 install.ps1。"
case "$MIRROR" in ""|https://*) ;; *) die "--mirror 必须是 https 地址" ;; esac
mkdir -p "$DIR"

# 重复安装时先停掉正在运行的旧版本
launchctl bootout "$GUI/$LABEL" >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 1. Python
say "[1/8] 检查 Python…"
py_ok() {  # 3.11 以上才行。系统自带的 /usr/bin/python3 是 3.9，而且没装开发者工具时会弹窗，不碰它
    [ -x "$1" ] && [ "$1" != "/usr/bin/python3" ] &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}
find_python() {
    local c
    for c in $(command -v python3.13 python3.12 python3.11 python3 2>/dev/null) \
             /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
             /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
             /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
        if py_ok "$c"; then printf '%s' "$c"; return 0; fi
    done
    return 1
}
BASEPY="$(find_python || true)"
if [ -z "$BASEPY" ]; then
    BREW="$(command -v brew || true)"
    [ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
    [ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
    if [ -n "$BREW" ]; then
        say "没找到 Python 3.11 以上，正在用 Homebrew 安装 Python 3.13…"
        "$BREW" install python@3.13 || die "Homebrew 安装 Python 失败。请从 python.org 装 Python 3.13 后再运行本脚本。"
    else
        PYVER="3.13.16"
        say "没找到 Python 3.11 以上，正在从 python.org 下载 Python $PYVER（约 70 MB）…"
        PKG="$(mktemp -d)/python.pkg"
        curl -fL --retry 3 -o "$PKG" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-macos11.pkg" ||
            die "下载 Python 失败。请从 python.org 装 Python 3.13 后再运行本脚本。"
        say "  下面会要你输入开机密码（输入时不显示），用来安装 Python。"
        sudo installer -pkg "$PKG" -target / || die "安装 Python 失败。"
        rm -f "$PKG"
        # python.org 的 Python 自带的 OpenSSL 没有根证书，不装的话连不上邮箱服务器
        CERTS="/Applications/Python ${PYVER%.*}/Install Certificates.command"
        [ -f "$CERTS" ] && /bin/bash "$CERTS" >/dev/null 2>&1 || true
    fi
    BASEPY="$(find_python || true)"
    [ -n "$BASEPY" ] || die "Python 安装后还是找不到，请从 python.org 装 Python 3.13 后再运行本脚本。"
fi
say "  用 $BASEPY"

# ---------------------------------------------------------------- 2. 程序本体
say "[2/8] 获取 KOL Mail…"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
if [ -n "$SOURCE" ]; then
    SRC="$(cd "$SOURCE" && pwd)"
else
    if [ -z "$REPO" ]; then ask "GitHub 仓库（owner/name）" ""; REPO="$REPLY"; fi
    TAG="$(curl -fsSL -H "Accept: application/vnd.github+json" "$(gh_url "https://api.github.com/repos/$REPO/releases/latest")" |
        "$BASEPY" -I -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])')" || die "查不到最新版本，检查网络或 --repo 参数。"
    curl -fsSL -o "$STAGING/src.zip" "$(gh_url "https://codeload.github.com/$REPO/zip/refs/tags/$TAG")" || die "下载 KOL Mail 失败。"
    (cd "$STAGING" && /usr/bin/unzip -q src.zip -d x)
    SRC="$(find "$STAGING/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    say "  最新版 $TAG"
fi
mkdir -p "$APP"
# data/ 是你的设置和缓存，永远不覆盖
/usr/bin/rsync -a --exclude data --exclude .git --exclude __pycache__ --exclude .venv --exclude venv "$SRC/" "$APP/" ||
    die "复制程序文件失败"
mkdir -p "$DATA"
chmod 700 "$DATA"

# ---------------------------------------------------------------- 3. 依赖
say "[3/8] 安装依赖（第一次要几分钟）…"
[ -x "$PY" ] || "$BASEPY" -m venv "$VENV" || die "创建 Python 虚拟环境失败"
PIP_ARGS=(-m pip install --disable-pip-version-check -q -r "$APP/requirements.txt")
[ -n "$PIP_INDEX" ] && PIP_ARGS+=(-i "$PIP_INDEX")
"$PY" "${PIP_ARGS[@]}" || die "依赖安装失败。公司网络慢的话，可以加参数 --pip-index https://mirrors.aliyun.com/pypi/simple 再试。"
MCP="$VENV/bin/mcp-email-server"

# ---------------------------------------------------------------- 4. 邮箱
say "[4/8] 配置邮箱…"
SETTINGS="$DATA/settings.json"
# old a.b：settings.json 里已有的值（没有就是空）
old() {
    "$PY" -I -c 'import json, sys
try:
    v = json.load(open(sys.argv[1], encoding="utf-8-sig"))
    for k in sys.argv[2].split("."):
        v = v[k]
    print(v if isinstance(v, str) else "")
except Exception:
    pass' "$SETTINGS" "$1"
}
MCP_STATUS="$("$MCP" config status 2>&1 || true)"
if printf '%s\n' "$MCP_STATUS" | grep -q "not_configured"; then
    "$MCP" config init --database "$HOME/.config/mcp-email-server/catalog.sqlite3" >/dev/null || die "初始化邮件组件失败"
fi
EMAIL="$(old mail.address)"
[ -z "$EMAIL" ] && EMAIL="$ASK_EMAIL"
until printf '%s' "$EMAIL" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; do
    [ "$YES" = 1 ] && die "无人值守安装需要 --email"
    ask "你的工作邮箱" ""; EMAIL="$REPLY"
done
EMAIL="$(printf '%s' "$EMAIL" | tr '[:upper:]' '[:lower:]')"
ACCOUNT="$(old mail.account_name)"
[ -z "$ACCOUNT" ] && ACCOUNT="$(printf '%s' "${EMAIL%%@*}" | tr -cd 'A-Za-z0-9_-')"
FULLNAME="$(old identity.name)"
[ -z "$FULLNAME" ] && FULLNAME="$ASK_NAME"
while [ -z "$FULLNAME" ]; do
    [ "$YES" = 1 ] && die "无人值守安装需要 --name"
    ask "对外署名（英文名，会出现在签名里）" ""; FULLNAME="$REPLY"
done
ACCOUNTS="$("$MCP" account list 2>&1 || true)"
if ! printf '%s\n' "$ACCOUNTS" | grep -Eq "(^|[^A-Za-z0-9_-])$ACCOUNT([^A-Za-z0-9_-]|$)"; then
    [ -z "$IMAP_HOST" ] && { ask "收信服务器（IMAP）" ""; IMAP_HOST="$REPLY"; }
    [ -z "$SMTP_HOST" ] && { ask "发信服务器（SMTP）" ""; SMTP_HOST="$REPLY"; }
    ADD=(account add "$ACCOUNT" --email "$EMAIL" --full-name "$FULLNAME" --imap-host "$IMAP_HOST" --smtp-host "$SMTP_HOST"
         --imap-user "$EMAIL" --smtp-user "$EMAIL")
    if [ "$PASSWORD_STDIN" = 1 ]; then
        "$MCP" "${ADD[@]}" --password-stdin || die "添加邮箱失败"
    else
        say "  下面会提示输入邮箱密码（输入时不显示），密码由邮件组件存进 Mac 的钥匙串。"
        "$MCP" "${ADD[@]}" < /dev/tty || die "添加邮箱失败，检查服务器地址和密码后重新运行本脚本。"
    fi
    if ! TEST_OUT="$("$MCP" account test "$ACCOUNT" 2>&1)"; then warn "  邮箱连接测试没通过：$TEST_OUT"; fi
fi
# 回信、附件、移动邮件要用到的权限
POLICY="$("$MCP" config policy 2>&1 || true)"
REV="$(printf '%s\n' "$POLICY" | sed -n 's/^revision=\([0-9][0-9]*\).*/\1/p' | head -n 1)"
if [ -n "$REV" ] && { ! printf '%s\n' "$POLICY" | grep -Eq '^allowed_mutations=(.*,)?send(,|$)' ||
                      ! printf '%s\n' "$POLICY" | grep -q '^enable_attachment_download=true'; }; then
    "$MCP" config update-policy --expected-revision "$REV" --enable-attachment-download --enable-attachment-content \
        --allowed-mutations "draft,organize,send,append" --allowed-recipients "*" >/dev/null 2>&1 ||
        warn "  没能打开回信和附件权限"
fi

# ---------------------------------------------------------------- 5. AI
say "[5/8] 选择 AI…"
# 后台服务拿不到终端里的 PATH，所以把找到的完整路径写进设置
CM="$(command -v codemaker || true)"
[ -z "$CM" ] && [ -x "$HOME/.codemaker/bin/codemaker" ] && CM="$HOME/.codemaker/bin/codemaker"
CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ]; then
    for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude \
             "$HOME/.npm-global/bin/claude"; do
        if [ -x "$c" ]; then CLAUDE="$c"; break; fi
    done
fi
FOUND=""
[ -n "$CM" ] && FOUND="CodeMaker "
[ -n "$CLAUDE" ] && FOUND="${FOUND}Claude Code"
say "  检测到：${FOUND:-都没有}"
DEFAULT_BACKEND="$(old llm.backend)"
[ -z "$DEFAULT_BACKEND" ] && DEFAULT_BACKEND="$ASK_BACKEND"
if [ -z "$DEFAULT_BACKEND" ]; then if [ -n "$CM" ]; then DEFAULT_BACKEND=codemaker; else DEFAULT_BACKEND=claude; fi; fi
BACKEND=""
while [ "$BACKEND" != "codemaker" ] && [ "$BACKEND" != "claude" ]; do
    ask "用哪个 AI？输入 codemaker 或 claude" "$DEFAULT_BACKEND"
    BACKEND="$(printf '%s' "$REPLY" | tr '[:upper:]' '[:lower:]')"
    [ "$YES" = 1 ] && [ "$BACKEND" != "codemaker" ] && [ "$BACKEND" != "claude" ] && die "--backend 只能是 codemaker 或 claude"
done
if [ "$BACKEND" = "codemaker" ]; then
    if [ -z "$CM" ]; then warn "  没找到 CodeMaker 命令行。请先按公司指引安装 CodeMaker CLI，装好后重新运行本脚本，KOL Mail 才能用上它。"
    elif ! "$CM" quota >/dev/null 2>&1; then warn "  CodeMaker 还没登录：请在新的终端窗口运行 codemaker auth login，登录后 KOL Mail 就能用。"; fi
elif [ -z "$CLAUDE" ]; then
    warn "  没找到 Claude Code 命令行（claude）。装好并登录一次后，重新运行本脚本。"
fi

# ---------------------------------------------------------------- 6. 你的设置（只写安装器负责的键，其余保持原样）
say "[6/8] 写设置…"
export KM_ACCOUNT="$ACCOUNT" KM_EMAIL="$EMAIL" KM_MCP="$MCP" KM_BACKEND="$BACKEND" KM_NAME="$FULLNAME"
export KM_CLAUDE="$CLAUDE" KM_CM="$CM" KM_REPO="$REPO" KM_MIRROR="$MIRROR" KM_FEEDBACK="$FEEDBACK"
export KM_TITLE="" KM_COMPANY="" KM_EXTRA="" KM_SITE="" KM_PLAYBOOK="" KM_KNOWLEDGE="" KM_RECORD=""
if [ -z "$(old identity.signature)" ]; then
    ask "职位（英文）" "Marketing Manager"; KM_TITLE="$REPLY"
    KM_COMPANY="$ASK_COMPANY"
    while [ -z "$KM_COMPANY" ]; do
        [ "$YES" = 1 ] && die "无人值守安装需要 --company"
        ask "公司名（英文）" ""; KM_COMPANY="$REPLY"
    done
    ask "签名里额外的一行（比如 Discord: xxx，没有就回车）" ""; KM_EXTRA="$REPLY"
    ask "签名里的网址（没有就回车）" ""; KM_SITE="$REPLY"
fi
if [ -z "$(old paths.playbook)" ]; then
    ask "谈判手册（Markdown 文件）的完整路径，没有就回车用模板" "$DATA/谈判手册.md"
    KM_PLAYBOOK="${REPLY/#\~/$HOME}"
    [ -f "$KM_PLAYBOOK" ] || cp "$APP/templates/谈判手册.example.md" "$KM_PLAYBOOK"
fi
if [ -z "$(old paths.koc_knowledge)" ]; then
    KM_KNOWLEDGE="$DATA/KOC机器人知识.md"
    [ -f "$KM_KNOWLEDGE" ] || cp "$APP/templates/KOC机器人知识.example.md" "$KM_KNOWLEDGE"
fi
[ -z "$(old paths.autoreply_record)" ] && KM_RECORD="$DATA/自动回复记录.md"
export KM_TITLE KM_COMPANY KM_EXTRA KM_SITE KM_PLAYBOOK KM_KNOWLEDGE KM_RECORD
ANSWERS="$STAGING/answers.json"
"$PY" -I - "$ANSWERS" <<'PYEOF'
import json, os, sys
e = os.environ.get
email = e("KM_EMAIL")
answers = {
    "mail": {"account_name": e("KM_ACCOUNT"), "address": email, "company_domain": "@" + email.split("@")[1],
             "mcp_exe": e("KM_MCP")},
    "llm": {"backend": e("KM_BACKEND")},
    "identity": {"name": e("KM_NAME")},
    "paths": {}, "update": {}, "feedback": {},
}
if e("KM_CLAUDE"):
    answers["llm"]["claude_bin"] = e("KM_CLAUDE")
if e("KM_CM"):
    answers["llm"]["codemaker_bin"] = e("KM_CM")
if e("KM_COMPANY"):
    title, company = e("KM_TITLE"), e("KM_COMPANY")
    answers["identity"].update(title=title, company=company, signature="\n".join(
        x for x in (e("KM_NAME"), f"{title} | {company}", e("KM_EXTRA"), e("KM_SITE")) if x))
for key, env in (("playbook", "KM_PLAYBOOK"), ("koc_knowledge", "KM_KNOWLEDGE"), ("autoreply_record", "KM_RECORD")):
    if e(env):
        answers["paths"][key] = e(env)
if e("KM_REPO"):
    answers["update"]["repo"] = e("KM_REPO")
if e("KM_MIRROR"):
    answers["update"]["mirror"] = e("KM_MIRROR")
if e("KM_FEEDBACK"):
    answers["feedback"]["popo_webhook"] = e("KM_FEEDBACK")
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(answers, f, ensure_ascii=False)
PYEOF
"$PY" -I "$APP/tools/merge_settings.py" "$SETTINGS" "$ANSWERS" || die "写设置失败"
chmod 600 "$SETTINGS"
[ -f "$DATA/autoreply.json" ] || cp "$APP/autoreply.example.json" "$DATA/autoreply.json"

# ---------------------------------------------------------------- 7. 后台服务
say "[7/8] 注册开机自动启动…"
# launchd 给的 PATH 只有系统目录。AI 命令行可能是 node 脚本，把 node 和它们所在的目录也加进去。
SVC_PATH="$HOME/.local/bin:$HOME/.codemaker/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for tool in "$(command -v node || true)" "$CLAUDE" "$CM"; do
    [ -z "$tool" ] && continue
    case ":$SVC_PATH:" in *":$(dirname "$tool"):"*) ;; *) SVC_PATH="$(dirname "$tool"):$SVC_PATH" ;; esac
done
# python.org 的 Python 不带根证书时，让它用 certifi 的
CAFILE=""
if ! "$PY" -I -c 'import os, ssl, sys; p = ssl.get_default_verify_paths(); sys.exit(0 if (p.cafile and os.path.exists(p.cafile)) or (p.capath and os.path.isdir(p.capath) and os.listdir(p.capath)) else 1)'; then
    CAFILE="$("$PY" -I -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
fi
mkdir -p "$HOME/Library/LaunchAgents"
"$PY" -I - "$PLIST" "$LABEL" "$APP/service/run.sh" "$PY" "$LOG" "$SVC_PATH" "$CAFILE" <<'PYEOF'
import plistlib, sys
plist, label, run, py, log, path, cafile = sys.argv[1:8]
env = {"PATH": path, "PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
if cafile:
    env["SSL_CERT_FILE"] = cafile
with open(plist, "wb") as f:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": ["/bin/bash", run, py],
        "RunAtLoad": True,
        "KeepAlive": True,          # restarts the app whenever it exits, which is also how a self-update takes effect
        "ThrottleInterval": 15,
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": log + ".launchd",
        "StandardErrorPath": log + ".launchd",
    }, f)
PYEOF
launchctl bootstrap "$GUI" "$PLIST" 2>/dev/null || { launchctl bootout "$GUI/$LABEL" >/dev/null 2>&1 || true; launchctl bootstrap "$GUI" "$PLIST"; } ||
    die "注册后台服务失败"
launchctl enable "$GUI/$LABEL" 2>/dev/null || true

# ---------------------------------------------------------------- 8. 应用图标，启动
say "[8/8] 放“KOL Mail”图标并启动…"
APPS="/Applications"
[ -w "$APPS" ] || { APPS="$HOME/Applications"; mkdir -p "$APPS"; }
OPEN_SH="$APP/service/open.sh"
OPEN_AS="$(printf '%s' "$OPEN_SH" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
rm -rf "$APPS/KOL Mail.app"
/usr/bin/osacompile -o "$APPS/KOL Mail.app" \
    -e "try" \
    -e "do shell script \"/bin/bash \" & quoted form of \"$OPEN_AS\"" \
    -e "on error msg" \
    -e "display dialog msg buttons {\"好\"} default button 1 with title \"KOL Mail\" with icon caution" \
    -e "end try" || warn "  没能生成“KOL Mail”应用，以后可以在浏览器里直接打开 http://127.0.0.1:8765/"
if [ "$NO_OPEN" = 1 ]; then
    /bin/bash "$OPEN_SH" --no-browser || die "KOL Mail 没有启动起来，日志在 $LOG"
else
    /bin/bash "$OPEN_SH" || die "KOL Mail 没有启动起来，日志在 $LOG"
fi
say "装好了。以后在“应用程序”（或启动台）里点“KOL Mail”就能打开；每天第一次打开会自动检查更新。"
say "下班 AI 回复需要在页面上方“手机审稿”里绑定你自己的 Telegram 机器人。"
say "新邮件通知第一次弹出时，如果系统问要不要允许“脚本编辑器”发通知，请点允许。"
