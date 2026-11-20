# msg-notifier

运行在 **Windows 11 工作电脑**上的小脚本：把 Teams / 飞书 / 新版 Outlook 等桌面应用发到 Windows 通知中心的通知，转发到 iPhone 上的 [Bark](https://github.com/Finb/Bark)。

- 只用 Python 标准库，不需要 `pip install`
- 不调用任何应用的接口，也不需要登录；应用自己保持登录、自己弹通知即可
- 本仓库放在 N150 上只是为了统一用 git 管理，**脚本本身不在 N150 上运行**

## 原理

Windows 把通知中心里的所有 toast 存在 SQLite 数据库：

```
%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db
```

脚本以只读方式每 3 秒查一次新记录，解析出来源应用、标题、正文，再推送到 Bark。开了勿扰模式时，通知照样会写进数据库，所以也能转发。

## 安装（工作电脑上）

1. 安装 Python 3.10+（python.org 安装包，勾选 *Add to PATH*）。
2. 把本目录复制到电脑上，比如 `C:\Users\<你>\msg-notifier\`。
3. 先诊断：让别人在 Teams、飞书各给你发一条消息、发一封邮件，然后运行
   ```powershell
   python notify_agent.py --diagnose
   ```
   看各应用的通知有没有出现，并记下 `App ID`。
4. 复制 `config.example.json` 为 `config.json`，填 `bark_key`。如果诊断时看到的 App ID 没被 `apps` 里的关键字匹配上（尤其是飞书），就加一项。
5. 测试推送：`python notify_agent.py --test`
6. 前台试跑：`python notify_agent.py`（试的时候把 `idle_seconds` 临时设成 0）
7. 设为开机/登录自启（后台运行、崩溃后 1 分钟自动重启）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
   ```
   卸载：`Unregister-ScheduledTask -TaskName msg-notifier -Confirm:$false`

日志：脚本同目录的 `notify_agent.log`（自动轮转）。

## 配置项（config.json）

| 键 | 说明 |
|---|---|
| `bark_server` / `bark_key` | Bark 服务器（默认官方 `https://api.day.app`，HTTPS 443）和设备 key |
| `bark_level` | `active` / `timeSensitive`（专注模式下也弹出）/ `critical`（静音也响） |
| `bark_call` | `true` = 持续响铃约 30 秒 |
| `bark_sound` | Bark 内置铃声名，留空用默认 |
| `show_body` | `false` = 只推“谁发来消息”，不带正文 |
| `idle_seconds` | 键鼠多久没动才转发（默认 300 秒），坐在电脑前时不推；0 = 总是转发 |
| `min_interval` | 同一应用两次推送最小间隔（秒） |
| `apps` | App ID 关键字 → 显示名；只转发匹配上的应用 |
| `forward_unknown_apps` | 是否也转发 `apps` 之外的应用 |
| `ignore_keywords` | 标题/正文含这些词就不推 |

## 注意

- 电脑要关闭自动睡眠（锁屏没关系）。
- 应用如果用自己画的弹窗而不走系统通知（部分国产软件会这样），`--diagnose` 里就看不到它，需要另想办法。
- 消息内容会经过 Bark 服务器。介意的话设 `show_body: false`，或以后在 N150 上自己搭 bark-server。
