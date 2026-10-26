#!/bin/bash
# KOL Mail 一键安装（macOS 13 以上，Apple 芯片和 Intel 都行）
#
# 用法（维护者会在 POPO 群里发带参数的完整命令，复制到“终端”里回车）：
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/kol-mail/main/install.sh)" -- \
#       --repo <owner>/kol-mail --imap-host <收信服务器> --smtp-host <发信服务器> --feedback <反馈地址> \
#       --google-client-id <OAuth 客户端 ID> --google-client-secret <客户端密钥>
# 带了 Google 客户端参数，才会问要不要接 KOL 总表（Mac 上总表是一张 Google 表格），同事也才能在页面的
# 设置 → KOL 总表 里连自己已有的 Google 表格。公司邮箱的域名用 --domain 改（默认 lootbar.com）。
#
# 做的事：找到或安装 Python，下载最新版 KOL Mail，装依赖，配置 AI（Claude Code 或 CodeMaker），注册开机自动启动的
# 后台服务（launchd），在“应用程序”里放一个“KOL Mail”。邮箱在装好后打开的网页登录页里登录。重复运行是安全的，
# 已有的设置会保留。邮箱密码只在登录页或邮件组件自己的提示里输入，这个脚本看不到也不保存，密码存进 Mac 的钥匙串。

set -eo pipefail

REPO=""; FEEDBACK=""; IMAP_HOST=""; SMTP_HOST=""; PIP_INDEX=""; MIRROR=""; SOURCE=""; GCLIENT_ID=""; GCLIENT_SECRET=""
DOMAIN="lootbar.com"
DIR="$HOME/Library/Application Support/kol-mail"
# 维护者测试用（云主机上无人值守安装）：不提问，用下面的参数和默认值
YES=0; ASK_EMAIL=""; ASK_NAME=""; ASK_COMPANY=""; ASK_BACKEND=""; PASSWORD_STDIN=0; NO_OPEN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --feedback) FEEDBACK="$2"; shift 2 ;;
        --imap-host) IMAP_HOST="$2"; shift 2 ;;
        --smtp-host) SMTP_HOST="$2"; shift 2 ;;
        --domain) DOMAIN="$2"; shift 2 ;;
        --pip-index) PIP_INDEX="$2"; shift 2 ;;
        --mirror) MIRROR="$2"; shift 2 ;;   # GitHub 连不上时用的 https 代理前缀
        --dir) DIR="$2"; shift 2 ;;
        --source) SOURCE="$2"; shift 2 ;;   # 本地源码目录（维护者测试用）；留空则从 GitHub 下载最新版
        --google-client-id) GCLIENT_ID="$2"; shift 2 ;;
        --google-client-secret) GCLIENT_SECRET="$2"; shift 2 ;;
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

STEP=""; RECENT=""; LAST_ERR=""; ERR_LINE=""; ERR_CMD=""
note() { RECENT="$(printf '%s\n%s' "$RECENT" "$*" | tail -n 30)"; }
say() { printf '\033[36m%s\033[0m\n' "$*"; note "$*"; case "$*" in "["[0-9]"/8]"*) STEP="${*%%]*}]" ;; esac; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; note "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; LAST_ERR="$*"; exit 1; }

# 出了问题自动发到维护者的 POPO 反馈群（和页面上“反馈”按钮同一个机器人，消息里必须有 KOL Mail 这个关键词）：
# 安装失败，以及装完了但 AI、邮箱用不了。地址来自 --feedback，重装时也读旧设置里的。发不出去不影响安装。
report_problem() {  # report_problem "哪里" "错误"
    local url="$FEEDBACK" py="" text body version="${TAG:-?}"
    [ -z "$url" ] && [ -x "${PY:-}" ] && [ -f "${SETTINGS:-}" ] && url="$(old feedback.popo_webhook 2>/dev/null || true)"
    case "$url" in https://*|http://127.0.0.1:*) ;; *) return 0 ;; esac  # loopback: the tests' stand-in
    [ -n "${SRC:-}" ] && [ -f "$SRC/VERSION" ] && version="$(cat "$SRC/VERSION")"
    text="$(printf '【KOL Mail 自动报错 · %s】\n错误：%s\n卡在：%s\n环境：macOS %s (%s) · Python %s · AI %s · 邮箱 %s · %s\n最近输出：\n%s\n\n(版本 %s)' \
        "$1" "$2" "${STEP:-开始之前}" "$(sw_vers -productVersion 2>/dev/null)" "$(uname -m)" \
        "$("${BASEPY:-false}" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo 没找到)" \
        "${BACKEND:-还没选}" "${EMAIL:-还没填}" "$(id -un)" "$RECENT" "$version")"
    if [ -x "${PY:-}" ]; then py="$PY"; elif [ -x "${BASEPY:-}" ]; then py="$BASEPY"; fi
    if [ -n "$py" ]; then
        body="$(printf '%s' "$text" | "$py" -I -c 'import json, sys; print(json.dumps({"message": sys.stdin.read()}, ensure_ascii=False))')"
    else  # no Python yet: escape by hand
        body="{\"message\": \"$(printf '%s' "$text" | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' -e 's/\r//g' |
            awk 'BEGIN { ORS = "\\n" } { print }')\"}"
    fi
    printf '%s' "$body" | curl -sS -m 15 -o /dev/null -H "Content-Type: application/json; charset=utf-8" \
        --data-binary @- "$url" >/dev/null 2>&1 || true
}
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
STAGING="$(mktemp -d)"
WAS_LOADED=0; DONE=0
on_exit() {
    local code=$?
    # 安装失败（不是同事自己按 Ctrl+C）：发给维护者（先发，下载下来的源码里有版本号）
    if [ "$code" -ne 0 ] && [ "$code" -ne 130 ]; then
        report_problem "安装失败 Mac" "${LAST_ERR:-脚本出错退出（第 $ERR_LINE 行：$ERR_CMD，退出码 $code）}"
    fi
    rm -rf "$STAGING"
    # 中途失败时把原来在跑的服务拉回来，不让同事的后台一直停着
    if [ "$DONE" != 1 ] && [ "$WAS_LOADED" = 1 ] && [ -f "$PLIST" ]; then
        launchctl enable "$GUI/$LABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "$GUI" "$PLIST" >/dev/null 2>&1 || true
    fi
}
trap on_exit EXIT
trap 'ERR_LINE=$LINENO; ERR_CMD=$BASH_COMMAND' ERR

# ---------------------------------------------------------------- 1. Python
say "[1/8] 检查 Python…"
py_ok() {  # 3.11 以上才行。系统自带的 /usr/bin/python3 是 3.9，而且没装开发者工具时会弹窗，不碰它
    [ -x "$1" ] && [ "$1" != "/usr/bin/python3" ] &&
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}
find_python() {
    local c
    for c in $(command -v python3.13 python3.12 python3.14 python3.11 python3 2>/dev/null) \
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
if [ -n "$SOURCE" ]; then
    SRC="$(cd "$SOURCE" && pwd)"
else
    if [ -z "$REPO" ]; then ask "GitHub 仓库（owner/name）" ""; REPO="$REPLY"; fi
    # 版本号从 releases/latest 的跳转地址里读：GitHub API 每个 IP 每小时只给 60 次匿名查询，公司网络共用出口 IP 时
    # 经常早就用完了。API 只在跳转读不到时兜底。
    TAG="$(curl -sS -I -o /dev/null -w '%{redirect_url}' --max-time 30 "$(gh_url "https://github.com/$REPO/releases/latest")" |
        sed -nE 's#.*/releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)/?$#\1#p')" || TAG=""
    if [ -z "$TAG" ]; then
        TAG="$(curl -fsSL -H "Accept: application/vnd.github+json" "$(gh_url "https://api.github.com/repos/$REPO/releases/latest")" |
            "$BASEPY" -I -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null)" || TAG=""
    fi
    printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' ||
        die "查不到 KOL Mail 的最新版本。多半是网络连不上 GitHub，换个网络（比如手机热点）再运行一次安装；还不行就把这个窗口截图发给维护者。"
    curl -fsSL -o "$STAGING/src.zip" "$(gh_url "https://codeload.github.com/$REPO/zip/refs/tags/$TAG")" || die "下载 KOL Mail 失败。"
    /usr/bin/ditto -x -k "$STAGING/src.zip" "$STAGING/x" || die "解压 KOL Mail 失败。"
    SRC="$(find "$STAGING/x" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    say "  最新版 $TAG"
fi
# 换文件前先停掉正在运行的旧版本（重复安装时）
if launchctl print "$GUI/$LABEL" >/dev/null 2>&1; then WAS_LOADED=1; fi
launchctl bootout "$GUI/$LABEL" >/dev/null 2>&1 || true
mkdir -p "$APP"
# data/ 是你的设置和缓存，永远不覆盖
/usr/bin/rsync -a --exclude data --exclude .git --exclude __pycache__ --exclude .venv --exclude venv "$SRC/" "$APP/" ||
    die "复制程序文件失败"
mkdir -p "$DATA"
chmod 700 "$DATA"

# ---------------------------------------------------------------- 3. 依赖
say "[3/8] 安装依赖（第一次要几分钟）…"
# 虚拟环境依赖的 Python 被卸载或升级后，里面的 python 会失效，这时整个重建
if ! "$PY" -c "" >/dev/null 2>&1; then
    rm -rf "$VENV"
    "$BASEPY" -m venv "$VENV" || die "创建 Python 虚拟环境失败"
fi
PIP_ARGS=(-m pip install --disable-pip-version-check -q -r "$APP/requirements.txt")
[ -n "$PIP_INDEX" ] && PIP_ARGS+=(-i "$PIP_INDEX")
"$PY" "${PIP_ARGS[@]}" || die "依赖安装失败。公司网络慢的话，可以加参数 --pip-index https://mirrors.aliyun.com/pypi/simple 再试。"
MCP="$VENV/bin/mcp-email-server"
# python.org 的 Python 不带根证书时，让它用 certifi 的（安装时测邮箱和后台服务都要用）
CAFILE=""
if ! "$PY" -I -c 'import os, ssl, sys; p = ssl.get_default_verify_paths(); sys.exit(0 if (p.cafile and os.path.exists(p.cafile)) or (p.capath and os.path.isdir(p.capath) and os.listdir(p.capath)) else 1)'; then
    CAFILE="$("$PY" -I -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
    [ -n "$CAFILE" ] && export SSL_CERT_FILE="$CAFILE"
fi

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
# 新版程序在网页上登录邮箱（登录页）：第一次装、又没用 --email 指定时不在这里问，装好后浏览器打开的就是登录页。
# 已经配好邮箱的（重复运行本脚本）、无人值守安装和装旧版程序的，还是在这里配置、测试邮箱。
WEB_LOGIN=0
if [ -z "$EMAIL" ] && [ "$YES" != 1 ] && grep -q 'id="loginView"' "$APP/static/index.html" 2>/dev/null; then WEB_LOGIN=1; fi
ACCOUNT=""
FULLNAME="$(old identity.name)"
if [ "$WEB_LOGIN" = 1 ]; then
    say "  邮箱在 KOL Mail 的页面上登录：装好后浏览器会打开登录页，在那里填邮箱和密码（授权码）。"
else
    until printf '%s' "$EMAIL" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; do
        [ "$YES" = 1 ] && die "无人值守安装需要 --email"
        ask "你的工作邮箱" ""; EMAIL="$REPLY"
    done
    EMAIL="$(printf '%s' "$EMAIL" | tr '[:upper:]' '[:lower:]')"
    ACCOUNT="$(old mail.account_name)"
    [ -z "$ACCOUNT" ] && ACCOUNT="$(printf '%s' "${EMAIL%%@*}" | tr -cd 'A-Za-z0-9_-')"
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
    fi
    # 连接测试。密码输错了可以当场重输（重跑安装命令时也会再测一次）
    MAIL_OK=0
    for try in 1 2 3; do
        if TEST_OUT="$("$MCP" account test "$ACCOUNT" 2>&1)"; then say "  邮箱连接正常"; MAIL_OK=1; break; fi
        warn "  邮箱连接测试没通过：$TEST_OUT"
        [ "$YES" = 1 ] && break
        ask "要重新输入邮箱密码吗？输入 y 重输，直接回车跳过" ""
        case "$REPLY" in y|Y|yes) ;; *) warn "  先跳过。服务器地址填错的话，把上面这段提示截图发给维护者。"; break ;; esac
        say "  先输收信（IMAP）密码，再输发信（SMTP）密码，一般两次都是邮箱密码。"
        "$MCP" account set-secret "$ACCOUNT" incoming < /dev/tty || warn "  收信密码没改成"
        "$MCP" account set-secret "$ACCOUNT" outgoing < /dev/tty || warn "  发信密码没改成"
    done
    [ "$MAIL_OK" = 1 ] || report_problem "安装：邮箱没连通" "${TEST_OUT:-邮箱连接测试没通过}"
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
AI_PROBLEM=""
if [ "$BACKEND" = "codemaker" ]; then
    if [ -z "$CM" ]; then AI_PROBLEM="没找到 CodeMaker 命令行"
        warn "  没找到 CodeMaker 命令行。请先按公司指引安装 CodeMaker CLI，装好后重新运行本脚本，KOL Mail 才能用上它。"
    elif ! QUOTA_OUT="$("$CM" quota 2>&1)"; then AI_PROBLEM="CodeMaker quota 失败（多半没登录）：$(printf '%s' "$QUOTA_OUT" | tail -n 3)"
        warn "  CodeMaker 还没登录：请在新的终端窗口运行 codemaker auth login，登录后 KOL Mail 就能用。"; fi
elif [ -z "$CLAUDE" ]; then AI_PROBLEM="没找到 Claude Code 命令行"
    warn "  没找到 Claude Code 命令行（claude）。装好并登录一次后，重新运行本脚本。"
fi
[ -z "$AI_PROBLEM" ] || report_problem "安装：AI 用不了" "$AI_PROBLEM"

# ---------------------------------------------------------------- 6. 你的设置（只写安装器负责的键，其余保持原样）
say "[6/8] 写设置…"
export KM_ACCOUNT="$ACCOUNT" KM_EMAIL="$EMAIL" KM_MCP="$MCP" KM_BACKEND="$BACKEND" KM_NAME="$FULLNAME"
export KM_CLAUDE="$CLAUDE" KM_CM="$CM" KM_REPO="$REPO" KM_MIRROR="$MIRROR" KM_FEEDBACK="$FEEDBACK"
export KM_DOMAIN="$DOMAIN" KM_IMAP="$IMAP_HOST" KM_SMTP="$SMTP_HOST" KM_GCLIENT_ID="$GCLIENT_ID" KM_GCLIENT_SECRET="$GCLIENT_SECRET"
export KM_TITLE="" KM_COMPANY="" KM_EXTRA="" KM_SITE="" KM_PLAYBOOK="" KM_KNOWLEDGE="" KM_RECORD="" KM_COMPANY_ONLY=""
# web login: no signature questions here, but the drafts still need the company's name (as -Company on Windows)
[ "$WEB_LOGIN" = 1 ] && [ -z "$(old identity.company)" ] && KM_COMPANY_ONLY="${ASK_COMPANY:-LootBar}"
if [ -z "$(old identity.signature)" ] && [ "$WEB_LOGIN" != 1 ]; then  # web login: the page asks for the signature
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
    if [ "$WEB_LOGIN" = 1 ]; then  # the page asks for one's own way of negotiating instead
        REPLY="$DATA/谈判手册.md"
    else
        ask "谈判手册（Markdown 文件）的完整路径，没有就回车用模板" "$DATA/谈判手册.md"
    fi
    # 从访达拖进终端的路径会带引号或反斜杠转义的空格
    KM_PLAYBOOK="$(printf '%s' "$REPLY" | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/' -e 's/\\\(.\)/\1/g')"
    KM_PLAYBOOK="${KM_PLAYBOOK/#\~/$HOME}"
    if [ ! -f "$KM_PLAYBOOK" ]; then
        if ! { mkdir -p "$(dirname "$KM_PLAYBOOK")" && cp "$APP/templates/谈判手册.example.md" "$KM_PLAYBOOK"; } 2>/dev/null; then
            warn "  $KM_PLAYBOOK 放不进去，先用模板，放在 $DATA/谈判手册.md"
            KM_PLAYBOOK="$DATA/谈判手册.md"
            [ -f "$KM_PLAYBOOK" ] || cp "$APP/templates/谈判手册.example.md" "$KM_PLAYBOOK"
        fi
    fi
fi
[ -z "$(old paths.autoreply_record)" ] && KM_RECORD="$DATA/自动回复记录.md"
# KOL 总表：Mac 上是同事自己云端硬盘里的一张 Google 表格，KOL Mail 只能碰它自己建的这张
KM_SHEET=""
GSHEET="$APP/sheet/gsheet.py"
CURRENT_SHEET="$(old paths.sheet_workbook)"
case "$CURRENT_SHEET" in
    gsheet:*)
        if ! CHECK_OUT="$("$PY" -I "$GSHEET" check --state "$DATA/sheet" --id "${CURRENT_SHEET#gsheet:}" 2>&1)"; then
            warn "  现有的 Google 总表现在读不了：$CHECK_OUT"
            if [ -n "$GCLIENT_ID" ] && [ "$YES" != 1 ]; then
                ask "要重新授权 Google 表格吗？输入 y 重新授权，直接回车跳过" ""
                case "$REPLY" in
                    y|Y|yes)
                        "$PY" -I "$GSHEET" auth --state "$DATA/sheet" --client-id "$GCLIENT_ID" --client-secret "$GCLIENT_SECRET" &&
                            "$PY" -I "$GSHEET" check --state "$DATA/sheet" --id "${CURRENT_SHEET#gsheet:}" >/dev/null 2>&1 &&
                            say "  Google 总表又能用了" || warn "  还是不行，把上面的提示截图发给维护者。" ;;
                esac
            fi
        fi ;;
    "")
        if [ -n "$GCLIENT_ID" ] && [ "$YES" != 1 ]; then
            ask "要不要接 KOL 总表（Google 表格）？收发邮件会自动记进去。输入 y 接，直接回车不接" ""
            case "$REPLY" in
                y|Y|yes)
                    mkdir -p "$DATA/sheet"
                    if "$PY" -I "$GSHEET" auth --state "$DATA/sheet" --client-id "$GCLIENT_ID" --client-secret "$GCLIENT_SECRET" &&
                       SHEET_OUT="$("$PY" -I "$GSHEET" create --state "$DATA/sheet")"; then
                        KM_SHEET="gsheet:$(printf '%s\n' "$SHEET_OUT" | tail -n 1)"
                        say "  $(printf '%s\n' "$SHEET_OUT" | head -n 1)"
                    else
                        warn "  Google 总表没接上，先不用。以后重新运行安装命令可以再接。"
                    fi ;;
            esac
        fi
        # 同事自己的 xlsx 总表，格式各不相同：这里只记下文件，第一次打开页面时核对各列对应什么，对好之前不往表里写。
        # 新建的是程序自己的简单模板，列对应直接定好。
        if [ -z "$KM_SHEET" ] && [ "$YES" != 1 ]; then
            say "  KOL 总表：连上你自己的 xlsx 总表后，打开邮件会显示 KOL 的频道链接，收发信的进展会记进表里（只写对得上的列，不改你的格式）。"
            say "  写表要用 Microsoft Excel。第一次写表时系统会问是否允许 KOL Mail 控制 Excel，请点“好”。"
            while true; do
                ask "总表文件的完整路径（可以把文件拖进这个窗口），输入 new 新建一张，不用就直接回车（以后在页面右上角 设置 → KOL 总表 里也能接）" ""
                # 拖进“终端”的文件路径带引号，或者空格前带反斜杠
                SHEET_PATH="$(printf '%s' "$REPLY" | sed -e "s/^'\(.*\)'\$/\1/" -e 's/^"\(.*\)"$/\1/' -e 's/\\\(.\)/\1/g')"
                [ -z "$SHEET_PATH" ] && break
                case "$SHEET_PATH" in
                    http://docs.popo.*|https://docs.popo.*)
                        say "  POPO 在线表格要先在这台 Mac 上装好并登录 popo-cli。装好 KOL Mail 后，在页面右上角 设置 → KOL 总表 里粘贴这个链接连接。"
                        break ;;
                esac
                CREATE=0
                if [ "$SHEET_PATH" = "new" ]; then CREATE=1; SHEET_PATH="$HOME/Documents/KOL总表.xlsx"; fi
                case "$SHEET_PATH" in "~/"*) SHEET_PATH="$HOME/${SHEET_PATH#??}" ;; esac
                case "$SHEET_PATH" in
                    *.xlsx|*.XLSX) ;;
                    *) warn "  只支持 .xlsx 格式（.xls、.csv、Numbers 请先在 Excel 里另存为 .xlsx）"; continue ;;
                esac
                if [ -f "$SHEET_PATH" ]; then
                    [ "$CREATE" = 1 ] && warn "  $SHEET_PATH 已经有文件了，直接用它"
                    KM_SHEET="$SHEET_PATH"
                    say "  第一次打开页面时会请你核对表里各列对应什么，对好之后才开始同步。"
                    break
                fi
                if [ "$CREATE" != 1 ]; then
                    ask "  这个文件不存在。输入 y 在这里新建一张，直接回车重新输入" ""
                    [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ] || continue
                fi
                if MADE="$("$PY" -I "$APP/sheet/usersheet.py" create --path "$SHEET_PATH" 2>/dev/null)"; then
                    KM_SHEET="$SHEET_PATH"
                    KM_SHEET_MAP="$(printf '%s' "$MADE" | "$PY" -I -c 'import json, sys; print(json.dumps(json.loads(sys.stdin.read())["map"], ensure_ascii=False))')"
                    say "  新建了 $SHEET_PATH，已经连上"
                    break
                fi
                warn "  新建失败：$MADE"
            done
        fi ;;
esac
export KM_TITLE KM_COMPANY KM_COMPANY_ONLY KM_EXTRA KM_SITE KM_PLAYBOOK KM_RECORD KM_SHEET KM_SHEET_MAP KM_APP="$APP" KM_PY="$PY"
ANSWERS="$STAGING/answers.json"
"$PY" -I - "$ANSWERS" <<'PYEOF'
import json, os, sys
e = os.environ.get
email = e("KM_EMAIL")
mail = {"mcp_exe": e("KM_MCP")}
if email:  # signed in here; otherwise on the page's login screen
    mail.update(account_name=e("KM_ACCOUNT"), address=email, company_domain="@" + email.split("@")[1])
elif e("KM_DOMAIN"):
    mail["company_domain"] = "@" + e("KM_DOMAIN")
if e("KM_DOMAIN") and e("KM_IMAP") and e("KM_SMTP"):  # the login screen needs no servers for a company address
    mail["company_servers"] = {"domain": e("KM_DOMAIN"), "imap_host": e("KM_IMAP"), "smtp_host": e("KM_SMTP")}
answers = {
    "mail": mail,
    "llm": {"backend": e("KM_BACKEND")},
    "identity": {"name": e("KM_NAME")} if e("KM_NAME") else {},
    "paths": {}, "update": {}, "feedback": {},
}
if e("KM_CLAUDE"):
    answers["llm"]["claude_bin"] = e("KM_CLAUDE")
if e("KM_CM"):
    answers["llm"]["codemaker_bin"] = e("KM_CM")
if e("KM_COMPANY_ONLY"):
    answers["identity"]["company"] = e("KM_COMPANY_ONLY")
if e("KM_COMPANY"):
    title, company = e("KM_TITLE"), e("KM_COMPANY")
    answers["identity"].update(title=title, company=company, signature="\n".join(
        x for x in (e("KM_NAME"), f"{title} | {company}", e("KM_EXTRA"), e("KM_SITE")) if x))
for key, env in (("playbook", "KM_PLAYBOOK"), ("autoreply_record", "KM_RECORD")):
    if e(env):
        answers["paths"][key] = e(env)
if e("KM_SHEET"):
    tool = "kol.py" if e("KM_SHEET").startswith("gsheet:") else "usersheet.py"  # own xlsx: mapped columns
    answers["paths"].update(sheet_workbook=e("KM_SHEET"), sheet_tool=os.path.join(e("KM_APP"), "sheet", tool),
                            sheet_python=e("KM_PY"))
if e("KM_SHEET_MAP"):
    answers["sheet_map"] = json.loads(e("KM_SHEET_MAP"))
if e("KM_REPO"):
    answers["update"]["repo"] = e("KM_REPO")
if e("KM_MIRROR"):
    answers["update"]["mirror"] = e("KM_MIRROR")
if e("KM_FEEDBACK"):
    answers["feedback"]["popo_webhook"] = e("KM_FEEDBACK")
if e("KM_GCLIENT_ID"):  # the page's Google sign-in for a colleague's own Google sheet
    answers["google"] = {"client_id": e("KM_GCLIENT_ID"), "client_secret": e("KM_GCLIENT_SECRET")}
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
# 同事在“系统设置 → 登录项”里关掉过后台项的话，不先 enable 就 bootstrap 不进去
launchctl enable "$GUI/$LABEL" 2>/dev/null || true
launchctl bootstrap "$GUI" "$PLIST" 2>/dev/null || { launchctl bootout "$GUI/$LABEL" >/dev/null 2>&1 || true; launchctl bootstrap "$GUI" "$PLIST"; } ||
    die "注册后台服务失败。打开“系统设置 → 通用 → 登录项与扩展”，把 KOL Mail 相关的后台项打开，再重新运行本脚本。"
DONE=1

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
[ "$WEB_LOGIN" = 1 ] && say "浏览器里打开的是 KOL Mail 的登录页：先登录你的邮箱，然后写自己的签名和“我的谈判方式”。"
say "下班 AI 回复需要在页面上方“手机审稿”里绑定你自己的 Telegram 机器人。"
say "新邮件通知第一次弹出时，如果系统问要不要允许“脚本编辑器”发通知，请点允许。"
