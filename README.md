# msg-notifier

运行在 **Windows 11 工作电脑**上的小程序：Teams / 飞书 / 新版 Outlook 收到新消息时，转发到 iPhone 上的 [Bark](https://github.com/Finb/Bark)。人在电脑前（最近有键鼠操作）时不推送。

本仓库放在 N150 上只是为了统一用 git 管理，**程序本身不在 N150 上运行**。

## 怎么检测

| 应用 | 方式 | 能拿到 |
|---|---|---|
| 新版 Outlook | Windows 通知数据库 `wpndatabase.db` 里的 toast | 发件人、主题、预览 |
| Teams | 自绘弹窗出现时用 UI Automation 读取 | 发送人、消息 |
| 飞书（ChintOA / Lark.exe） | 同上，按通用规则解析；读不到就截图 OCR | **待实测**，目前是推测 |
| 兜底 | 任务栏闪烁、标题未读数上升、托盘提示文字/图标变化 | 只知道“有新消息” |

兜底信号会延迟 5 秒，若这期间同一应用已推过具体内容就不再推，所以一条消息通常只推一次。

注意：新版 Outlook 关闭窗口后会彻底退出，就收不到邮件了。请在 Outlook 设置里打开“在后台运行”，或只最小化不关闭。

## 安装（工作电脑上）

1. 安装 Python 3.10+（勾选 *Add to PATH*）。
2. 获取代码（公开仓库，无需登录）：
   ```powershell
   git clone https://github.com/Zibo-Men/msg-notifier.git
   cd msg-notifier
   pip install -r requirements.txt
   ```
3. 复制 `config.example.json` 为 `config.json`，填 `bark_key`。其他配置项见 `notify_agent.py` 里的 `DEFAULT_CONFIG`，只需在 `config.json` 里写想改的项。
4. 测试推送：`python notify_agent.py --test`
5. 前台试跑：`python notify_agent.py`（试的时候把 `idle_seconds` 临时设成 0）
6. 设为登录自启（后台运行、崩溃后 1 分钟自动重启）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
   ```
   更新代码后重启任务：`git pull`，然后
   `Stop-ScheduledTask msg-notifier; Start-ScheduledTask msg-notifier`
   卸载：`Unregister-ScheduledTask -TaskName msg-notifier -Confirm:$false`

日志：`notify_agent.log`（自动轮转）。`debug_apps` 里的应用（默认飞书）会记录 `[DEBUG-飞书]` 详细事件、UIA/OCR 原文，弹窗截图存到 `debug_shots/`（最多 100 张）。

## 常用配置项（config.json）

| 键 | 说明 |
|---|---|
| `bark_server` / `bark_key` | Bark 服务器（默认官方 `https://api.day.app`，HTTPS 443）和设备 key |
| `bark_level` | `active` / `timeSensitive`（专注模式下也弹出）/ `critical`（静音也响） |
| `bark_call` | `true` = 持续响铃约 30 秒 |
| `show_body` | `false` = 只推“谁发来消息”，不带正文 |
| `idle_seconds` | 键鼠多久没动才转发（默认 300 秒）；0 = 总是转发 |
| `debug_apps` | 记录详细调试信息的应用，飞书调好后可改成 `[]` |
| `ignore_keywords` | 标题/正文含这些词就不推 |
| `window_apps` | 自绘弹窗应用的进程名、托盘关键字、弹窗尺寸范围 |

## 诊断工具

- `python notify_agent.py --diagnose`：列出通知数据库里最近的记录（toast / badge / tile）和来源应用 ID。
- `python probe.py --seconds 180`：记录这段时间里飞书/Teams 的任务栏闪烁、窗口弹出、标题、托盘变化、弹窗文字和 OCR 结果，输出到 `probe_output.txt`，截图在 `probe_shots/`。

OCR 用 Windows 自带的 `Windows.Media.Ocr`（`ocr.ps1`）。识别中文需要中文 OCR 语言包，没有的话用管理员 PowerShell 安装：

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"
```

把飞书、Teams 的托盘图标设为常显（设置 → 个性化 → 任务栏 → 其他系统托盘图标），否则托盘兜底检测看不到。

电脑要关闭自动睡眠（锁屏没关系）。消息内容会经过 Bark 服务器，介意的话设 `show_body: false`，或以后在 N150 上自建 bark-server。
