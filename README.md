# KOL Mail

给做海外 KOL 合作的人用的本机网页邮箱。外语来信自动翻成中文，回信由 AI 按你自己的谈判方式起草，每一段都附中文回译，你核对过才发得出去。

程序只在你自己的电脑上运行，网页地址是 http://127.0.0.1:8765/ ，别的电脑打不开。现在是公测版。

## 能做什么

| 功能 | 说明 |
|---|---|
| 来信翻译 | 外语邮件自动译成中文，原文和译文左右对照 |
| AI 起草回信 | 先给两三种回法让你挑，再起草、按你的意见改，逐段中文回译核对后才能发 |
| Telegram、Discord 私信 | 和 KOL 的私聊收进同一个页面，回法和邮件一样 |
| 下班值守 | 下班后的来信由 AI 起草，推到你手机的 Telegram 上，你批准了才发 |
| KOL 总表 | 收信、回信自动记进你自己的 Excel、Google 表格或 POPO 在线表格 |
| 多个邮箱 | 公司邮箱和个人邮箱可以同时放，随时切换 |

## 安装

需要 Windows 10/11 或 macOS 13 以上，还要一个 AI 命令行工具，Claude Code 和 CodeMaker 二选一。

Windows 在 PowerShell 里运行

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/EclairAikome/kol-mail/main/install.ps1 -OutFile $env:TEMP\kolmail-install.ps1; & $env:TEMP\kolmail-install.ps1 -Repo EclairAikome/kol-mail"
```

Mac 在“终端”里运行

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/EclairAikome/kol-mail/main/install.sh)" -- --repo EclairAikome/kol-mail
```

脚本会准备好 Python 和依赖，连上 AI，注册开机自动启动，再放一个 KOL Mail 图标（Windows 在桌面，Mac 在“应用程序”）。中途只问两件事，用哪个 AI，要不要接 KOL 总表。AI 没装或没登录时它会停下来带你处理。重复运行是安全的，已有的设置都保留。

团队统一分发时可以在命令后面加参数，都是可选的。

| Windows | Mac | 作用 |
|---|---|---|
| `-Feedback <地址>` | `--feedback <地址>` | 页面右下角“反馈”按钮把内容推到这个机器人地址 |
| `-GoogleClientId`、`-GoogleClientSecret` | `--google-client-id`、`--google-client-secret` | 让大家能把自己的 Google 表格连成 KOL 总表 |
| `-Company <名字>` | `--company <名字>` | 公司或品牌名，AI 起草时用来说明你的身份 |
| `-PipIndex <地址>` | `--pip-index <地址>` | 装依赖慢时换一个 PyPI 镜像 |
| `-Mirror <地址>` | `--mirror <地址>` | GitHub 连不上时用的 https 代理前缀 |

## 登录邮箱

装完浏览器会打开登录页。填完整的邮箱地址、密码和对外署名，点登录就行。

收发信服务器不用填。程序按邮箱地址自己找，先查域名的 MX 记录认出邮箱服务商，认不出再读 autoconfig 配置，最后试常见的主机名，找到后确认服务器有回应才用。实在找不到时页面会直接说明，这时再点开“手动填写服务器”。

| 邮箱 | 密码一栏填什么 |
|---|---|
| 公司邮箱（阿里、腾讯企业邮、Google Workspace、Amazon WorkMail 等） | 邮箱密码。公司要求客户端专用密码的，填那个 |
| 163、126、QQ | 网页版设置里开通 IMAP/SMTP 后生成的授权码 |
| Gmail | 应用专用密码 |
| Outlook、Hotmail、微软 365 | 微软只允许网页登录，暂时接不了 |

登录时会先测一遍收信和发信，密码不对当场就说。密码交给邮件组件存进系统的凭据管理器（Mac 是钥匙串），页面和设置文件里都不保存。

第一次登录后页面会请你写两样东西，一是签名，二是“我的谈判方式”，也就是默认推什么合作、固定费大概给多少、哪些不能答应。AI 出方案和起草都照它来，以后在右上角“设置”里随时改。

## 日常使用

### 回信

1. 打开来信，点“按手册起草”。可以先写几句要点，也可以留空。
2. AI 读完这封信和之前的往来，给出局面分析和两三个方案。选一个让它起草。
3. 草稿下面是逐段中文对照，每段单独回译，回译不可信的段落会标红。
4. 不满意就写修改意见让它改。看不懂这门语言时，直接改中文那一栏，再点“按改过的中文改原文”。
5. 对照核对无误后点发送。正文改过而对照没更新时，发送按钮是灰的。

自己写好了整段中文的话，点“翻译我写的中文”，跳过选方案。附件拖进回复区就行。图片可以插在正文里，点“插入图片”或者直接粘贴截图，再点“预览”看对方收到的样子。草稿自动保存，切走再回来还在。

往来很长的会话，早的那些折叠成一行，点开才展开，没读过的不会被折进去。

### 写新邮件

点页面上方的“写邮件”，填收件人，用中文写这封信要说什么，点“AI 起草”。后面的对照、修改和发送与回信相同。

### Telegram 和 Discord 私信

页头的“私信”里是你和 KOL 的一对一私聊，对方的话下面带中文译文。草稿每一段发成一条消息，同样要核对过中文对照才能发。

| 渠道 | 怎么接 | 怎么发 |
|---|---|---|
| Telegram | 设置 → 私信渠道 里登录你自己的账号 | KOL Mail 直接发出 |
| Discord | 按 设置 → 私信渠道 里的步骤给 Chrome 装上自带的扩展，保持 Discord 网页版开着 | 扩展把内容填进输入框，你检查后自己按回车 |

每个会话可以关联到 KOL 总表里的一行，关联后消息会记进总表。KOL Mail 更新后，要到 chrome://extensions 点一下扩展的刷新按钮。

### 下班时间

默认下班后什么都不自动回。点页面上方的“手机审稿”，按提示建一个自己的 Telegram 机器人并配对，之后下班时间的来信会由 AI 起草后推到手机上。你可以在手机上提问、提修改意见，点“发送”再点“确认发送”才会发出，没批的什么都不发。

想改成自动回一段固定的“我现在下班了”，或者彻底不回，也在这个窗口里选。工作时间是周一到周五 10:00 到 18:00（GMT+8），假期在 `data/autoreply.json` 里补。开着下班回复时，插着电的电脑不会自己睡眠，锁屏就可以离开。

### KOL 总表

在 设置 → KOL 总表 里连你自己的表，可以是本机的 xlsx、Google 表格或 POPO 在线表格。对应好列以后，收信和回信自动记进去，表里的频道链接也会显示在邮件旁边。

### 其他

| 功能 | 说明 |
|---|---|
| 标记 | 一时回不了的信点 ⚑，左边“已标记”里汇总，和邮箱自己的标记同步 |
| 文件夹 | 右键文件夹可以新建、改名、移动、删除，邮件可以拖进去 |
| 邮件格式 | 设置 → 邮件格式 里选字体、手写署名和签名横幅 |
| 桌宠 | 桌面右下角的鲸鱼娘，来新邮件时提醒你，点她手里的信打开 KOL Mail |
| 外观 | 配色、明暗和壁纸都能换 |
| 附件防护 | 只收常见的文档、图片和视频，逐个检查内容和宏，Windows 上再用 Defender 扫一遍 |

## 更新

每天第一次打开页面时，程序去 GitHub 看有没有新版本。有就自动下载、校验签名、装好并重启，十几秒后页面自己刷新。设置、缓存和草稿都在 `data` 文件夹里，更新不会碰。

## 隐私和安全

- 只监听 127.0.0.1，并检查请求来源，别的网站和别的电脑都调用不了它。
- 邮箱密码只存在系统的凭据管理器里。
- 找邮箱服务器时，只有邮箱的域名（@ 后面的部分）会被拿去查 DNS、这个域名自己的网站和 Thunderbird 的公开配置库。@ 前面的部分和密码不会发给它们。
- 翻译和起草时，邮件内容会发给你选的 AI。调用时关掉了 AI 的全部工具，来信只当作文字处理，里面的指令不会被执行。
- 每一封信都要你本人确认才发出。唯一自动发的是固定模板的下班回复，默认关着。
- Discord 扩展凭设置页里的令牌连接 KOL Mail，网页拿不到这个令牌。扩展从不替你按发送。

## 设置文件

个人设置在 `data/settings.json`，格式见 `settings.example.json`，改完要重启程序。

| 系统 | 安装目录 | 重启 |
|---|---|---|
| Windows | `%LOCALAPPDATA%\kol-mail\app` | PowerShell 里运行 `Stop-ScheduledTask kol-mail`，再双击桌面图标 |
| Mac | `~/Library/Application Support/kol-mail/app` | `launchctl kickstart -k gui/$(id -u)/local.kolmail` |

常用的几项

| 设置 | 作用 |
|---|---|
| `llm.backend` | `codemaker` 或 `claude` |
| `llm.models` | 不填用默认的。Claude Code 是翻译 Sonnet 5.5、起草 Opus 5.5，CodeMaker 是翻译 GPT-5.6 Terra、起草 GPT-6.1 Sol |
| `identity.signature` | 每封回信末尾的签名，在页面“设置”里改 |
| `paths.playbook` | 谈判手册，模板在 `templates` 文件夹 |

Mac 上卸载，在终端依次运行 `launchctl bootout gui/$(id -u)/local.kolmail` 和 `rm ~/Library/LaunchAgents/local.kolmail.plist`，再删掉“应用程序”里的 KOL Mail 和上面的安装目录。

## 开发

```powershell
python -m pytest tests
$env:KOL_DEV = "1"; $env:KOL_MAIL_PORT = "8766"; python app.py
```

第二行启动一个测试副本，不跑任何自动任务，也不会发信。有 `.git` 文件夹的副本被当作开发副本，不会自动更新。

试装安装脚本用 `install.ps1 -Source <源码目录> -Dir <别的目录> -NoService`，这样不注册开机任务、不放桌面图标，也不会停掉本机正在跑的 KOL Mail。
