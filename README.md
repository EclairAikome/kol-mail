# KOL Mail

给做海外 KOL 合作的同学用的本机网页邮箱。外语来信自动翻成中文，左右对照着看。回信时 AI 按你们团队的谈判手册先给出两三种回法，你挑一种，它起草，你提意见它再改，每一段都附中文回译，你核对无误才发得出去。下班时间来的消息，AI 起草后推到你手机上的 Telegram，你在手机上和它商量、批准，才会发出。

AI 可以用 CodeMaker，也可以用 Claude Code，安装时选一个。程序只在你自己电脑上运行，网页地址是 http://127.0.0.1:8765/ ，别的电脑打不开。

## 安装

维护者会在 POPO 群里发一条带参数的安装命令，复制到 PowerShell 里运行就行。大致是这样

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/<owner>/kol-mail/main/install.ps1 -OutFile $env:TEMP\kolmail-install.ps1; & $env:TEMP\kolmail-install.ps1 -Repo <owner>/kol-mail -ImapHost <收信服务器> -SmtpHost <发信服务器> -Feedback <反馈地址>"
```

脚本会一步步问你几件事，邮箱、署名、用哪个 AI、谈判手册放在哪。邮箱密码只在邮件组件自己的提示里输入，安装脚本看不到。装完桌面上会多一个“KOL Mail”图标，以后开机自动在后台运行，双击图标打开网页。

用 CodeMaker 的话，先确认命令行版装好并登录过（在终端运行 `codemaker auth login`）。用 Claude Code 的话，先装好并登录一次。

公司网络装依赖慢，可以在命令后面加 `-PipIndex https://mirrors.aliyun.com/pypi/simple`。

### Mac

macOS 13 以上，Apple 芯片和 Intel 都行。维护者发的 Mac 命令复制到“终端”里回车，大致是这样

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/kol-mail/main/install.sh)" -- --repo <owner>/kol-mail --imap-host <收信服务器> --smtp-host <发信服务器> --feedback <反馈地址>
```

问的问题和 Windows 一样。电脑上没有 Python 3.11 以上时，脚本会用 Homebrew 装，没有 Homebrew 就从 python.org 下载安装包，这一步要输一次开机密码。邮箱密码存进 Mac 的钥匙串，第一次收信时系统可能弹窗问是否允许访问钥匙串，点“始终允许”。装完“应用程序”里会多一个“KOL Mail”，点开就是网页。后台服务开机自动运行，程序退出后 15 秒内会自己重启。

Mac 版和 Windows 版的区别

- 新邮件通知是系统右上角的通知横幅，点它打不开那封信，要自己切到网页。第一次弹出时系统可能问是否允许“脚本编辑器”发通知，点允许。
- Mac 上没有能被程序调用的杀毒软件，附件只做格式、宏、脚本和压缩包检查，不做杀毒扫描，保存前的提示里会写明“未经杀毒扫描”。保存下来的文件带“来自互联网”标记，打开时系统会照常提示。
- 安装目录是 `~/Library/Application Support/kol-mail`，日志是里面的 `server.log`。
- 用 CodeMaker 的话，命令行版装在 `~/.codemaker/bin/codemaker`，装好后在终端运行 `codemaker auth login` 登录。CodeMaker 或 Claude Code 是装完 KOL Mail 之后才装的，重新运行一次安装命令，让它记下 AI 命令行的位置。

## 每天第一次打开会自动更新

打开网页时程序会去 GitHub 看有没有新版本，一天只看一次。有新版就自动下载、装好、重启，页面右上角会提示“已更新到 vX.Y.Z”，十几秒后自动刷新。你的设置、缓存和草稿都在 `data` 文件夹里，更新不会碰。GitHub 连不上的时候安静跳过，第二天再试。

## 回信怎么用

1. 打开一封来信，在回复框点“按手册起草”。输入框里可以先写几句要点，也可以留空。
2. AI 读完这封信和之前的往来，给出局面分析和两三个方案，每个方案写清楚怎么回、为什么、有什么风险。点“用这个方案起草”。
3. 草稿下面是逐段中文对照，每一段都是单独回译的，用来核对实际要发出去的外语。回译不可信的段落会标红，不会拿原文冒充。
4. 不满意就在“修改意见”里写，比如“语气热情一点”“价格改成两条 150 美元”，点“按意见修改”。可以来回改很多轮。
5. 也可以直接改外语正文。改过以后对照会过期，要点“更新对照”重新核对，核对完才能发送。
6. 写好了完整中文回复的话，点“翻译我写的中文”，跳过选方案这一步。
7. 要带附件，点草稿下面的“添加附件”，或者把文件拖进回复区。截图直接在正文框里 Ctrl+V 粘贴，会自动变成一个图片附件。单个附件和全部附件加起来都最多 15 MB，一封最多 10 个，程序、脚本、带宏的 Office 文件和网页文件不能发。邮件组件不支持把图片嵌进正文，图片都作为附件发出，对方的 Gmail、Outlook 会直接显示缩略图。

## 下班时间用手机审稿

点页面上方的“手机审稿”。

1. 在 Telegram 里找 @BotFather，发 `/newbot`，按提示起个名字，它会给你一串令牌。
2. 把令牌粘贴到“手机审稿”窗口，点保存。
3. 点窗口里的“在 Telegram 里配对”，Telegram 打开你的机器人后点底部的 START。配对后这个机器人只听你一个人的。用手机配对的话，把窗口里显示的链接发到手机上打开。
4. 下班回复方式选“AI 起草，手机审批”。

之后下班时间来的 KOL 邮件（和 Telegram 私聊，如果开了），机器人会把来信中文、局面分析和方案推给你。点方案它就起草，草稿以中文对照显示。回复那条消息可以提问，比如“这人之前报价多少”，也可以提修改意见。点“发送”后还要再点一次“确认发送”才会发出。你一直没批的，什么都不会发，第二天打开那封信时回复框上方会提示“手机审稿 #N”，可以把手机上的草稿载入接着改。

在机器人那里还可以发几个固定指令，点输入框旁的菜单就能选：`/template` 改发固定模板自动回复（适合休长假），`/ai` 改回 AI 起草、手机审批，`/status` 看现在的开关和等你处理的来信，`/holiday 10-25 11-02` 把这几天设成假期（假期全天算下班），`/unholiday 10-25 11-02` 去掉。也可以直接打中文“模板”“状态”“休假 10-25 11-02”。这些指令由程序直接执行，不经过 AI，只认配对过的你。

工作时间是周一到周五 10:00 到 18:00（GMT+8），法定假日在 `data\autoreply.json` 的 holidays 里，按需要补。

## 右下角的反馈按钮

想吐槽或者想要新功能，点右下角“反馈”。内容直接推到维护者的 POPO，不带你的名字、邮箱和电脑信息。

## 设置在哪

所有个人设置都在 `%LOCALAPPDATA%\kol-mail\app\data\settings.json`，格式见 `settings.example.json`。常用的几项

| 设置 | 作用 |
|---|---|
| `llm.backend` | `codemaker` 或 `claude` |
| `llm.models` | 不填就用默认的，翻译用 Sonnet 5.5，策略和起草用 Opus 5.5 |
| `identity.signature` | 每封回信末尾的签名，一字不差 |
| `paths.playbook` | 谈判手册，AI 的策略和草稿都照它来，改了立刻生效 |
| `paths.koc_knowledge` | KOC 机器人能用的全部知识 |
| `paths.sheet_workbook` | KOL 总表，不用就留空 |
| `koc` | KOC 机器人回复里允许出现的数字、比例和网站 |

改完 settings.json 要重启一次程序。在 PowerShell 里运行 `Stop-ScheduledTask kol-mail`，再双击桌面图标。Mac 上设置在 `~/Library/Application Support/kol-mail/app/data/settings.json`，改完在终端运行 `launchctl kickstart -k gui/$(id -u)/local.kolmail` 重启。

Mac 上要卸载的话，在终端依次运行 `launchctl bootout gui/$(id -u)/local.kolmail`、`rm ~/Library/LaunchAgents/local.kolmail.plist`，再删掉“应用程序”里的 KOL Mail 和 `~/Library/Application Support/kol-mail` 文件夹。

谈判手册和 KOC 知识卡的模板在 `templates` 文件夹。团队的正式版本找维护者要，不要传到网上。

## 其他功能

- 收件箱进来 KOL 的新邮件，屏幕右下角弹 Windows 通知，点一下打开那封信。
- 工作时间的 KOC 机器人。只回 KOL 总表里、不在重点名单上、只问联盟计划标准问题的人，回复内容只能来自知识卡，数字和链接还要过白名单检查。顶栏“KOC 机器人”按钮开关。
- KOL 总表联动。收信、回信自动记进总表的沟通记录，Excel 开着也能写。
- 附件防护。只收常见文档、图片和视频，逐个检查内容和宏，再用 Windows Defender 扫描。

## 安全

- 只监听 127.0.0.1，并检查请求来源，别的网站调用不了它。
- 调用 AI 时关掉所有工具，邮件内容只能当作文字处理。来信内容用随机标记隔开，AI 被要求不执行里面的任何指令，回复里不透露手册内容。
- 每一封信都要你本人点确认才会发出，自动化的部分只有 KOC 机器人和固定模板的下班回复，两者都可以关。

## 开发

```powershell
python -m pytest tests
$env:KOL_DEV = "1"; $env:KOL_MAIL_PORT = "8766"; python app.py   # 测试副本，不跑任何自动任务，也不会发信
```

有 `.git` 文件夹的副本被当作开发副本，不会自动更新。
