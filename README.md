# msg-notifier

Teams / 飞书 / 新版 Outlook 收到新消息时，推送到 iPhone（[Bark](https://github.com/Finb/Bark)）。人在电脑前（最近有键鼠操作）时不推送。

```
工作电脑（Windows 11）                    N150（Docker）                     iPhone
notify_agent.py ── HTTP 局域网 ──►  msg-notifier-hub :8101  ── HTTPS ──►  Bark App
  检测新消息、判断是否在电脑前          Bark 配置、开关、推送记录、日志
  上传全部日志 / 拉取运行参数
```

| 部分 | 运行位置 | 文件 |
|---|---|---|
| agent：检测新消息 | 工作电脑，Python（不用 Docker） | `notify_agent.py`、`win_watch.py`、`text_parse.py`、`ocr.ps1`、`probe.py` |
| hub：推送、记录、配置 | N150，Docker | `compose.yaml`、`hub/`、`.env`、`data/` |

## 怎么检测

| 应用 | 方式 | 能拿到 |
|---|---|---|
| 新版 Outlook | Windows 通知数据库 `wpndatabase.db` 里的 toast | 发件人、主题、预览 |
| Teams | 自绘弹窗出现时用 UI Automation 读取 | 发送人、消息 |
| 飞书（ChintOA / Lark.exe） | 同上，按通用规则解析；读不到就截图 OCR | **待实测**，目前是推测 |
| 兜底 | 任务栏闪烁、标题未读数上升、托盘提示文字/图标变化 | 只知道“有新消息” |

兜底信号会延迟 5 秒，若这期间同一应用已推过具体内容就不再推。

注意：新版 Outlook 关闭窗口后会彻底退出，就收不到邮件了。请在 Outlook 设置里打开“在后台运行”，或只最小化不关闭。

## 第一步：iPhone 上配置 Bark

1. App Store 搜索 **Bark**（作者 Fin，免费、开源），安装后打开，弹出“允许通知”时选**允许**。
2. 首页会显示一个示例链接，形如 `https://api.day.app/AbCdEf123456/这里改成你自己的推送内容`。
   中间那段 `AbCdEf123456` 就是你的 **设备 key**，复制下来（相当于手机的推送地址，别公开）。
3. 想先验证一下：在任何浏览器打开 `https://api.day.app/你的key/hello`，手机应立刻收到 “hello”。
4. 建议在 iPhone 设置里调整：
   - 设置 → 通知 → Bark：打开 **时效性通知**（专注模式下也能弹出，对应 `level=timeSensitive`，默认用这个）。
   - 如需静音/勿扰时也响铃：同一页打开 **重要提醒（Critical Alerts）**，再把 hub 的 `bark.level` 改成 `critical`。
   - 想像来电一样持续响 30 秒：hub 设置里 `bark.call` 改成 `true`。
   - 铃声：Bark App 里“铃声”页可以试听，挑中的名字填到 hub 的 `bark.sound`。
5. 推送历史在 Bark App 里能看到，按应用（Teams / 飞书 / Outlook）分组。

`api.day.app` 是 Bark 作者提供的免费公共服务器，消息内容会经过它（再经苹果 APNs 到手机）。介意的话以后可以在 N150 上自建 bark-server，只需改 `BARK_SERVER`。

## 第二步：N150 上配置 hub

hub 已在 N150 上运行（容器 `msg-notifier-hub`，端口 8101）。把 Bark key 填进 `.env`：

```bash
cd /home/zibo/docker/msg-notifier
nano .env          # 填 BARK_KEY=你的key（多台设备用逗号分隔）
docker compose up -d
```

测试 N150 → Bark：

```bash
curl -s -X POST http://192.168.0.119:8101/api/test -H "Authorization: Bearer $(grep ^HUB_TOKEN= .env | cut -d= -f2-)"
```

查看 `HUB_TOKEN`（工作电脑要填同一个）：`grep ^HUB_TOKEN= .env`

### 管理页面

浏览器打开 **http://192.168.0.119:8101/**（局域网内，手机也可以），首次输入 `HUB_TOKEN` 登录（令牌只存在该浏览器里）。

- **设置**：推送总开关；每个应用的开关、声音、提醒类型和“测试”按钮；默认提醒类型 / 铃声 / 持续响铃 / 是否显示正文；下发给工作电脑的“离开多久才推送”和调试日志开关；工作电脑在线状态。改动自动保存、立即生效（工作电脑参数约 1 分钟内生效）。
- **推送记录**：每条推送请求和结果（已推送 / 已静音 / 失败），可按应用、状态筛选。
- **日志**：工作电脑上传的全部日志，可搜索关键字、按级别和电脑筛选，自动刷新；`[DEBUG-飞书]` 调试行高亮。

设置保存在 `data/settings.json`，推送记录和日志在 `data/hub.db`（日志保留 30 天、推送记录 180 天，`.env` 可改）。所有接口见 `hub/app.py` 开头的说明。

## 第三步：工作电脑上运行 agent

1. 安装 Python 3.10+（勾选 *Add to PATH*）。
2. 获取代码（公开仓库，无需登录）：
   ```powershell
   git clone https://github.com/Zibo-Men/msg-notifier.git
   cd msg-notifier
   pip install -r requirements.txt
   ```
3. 复制 `config.example.json` 为 `config.json`，`hub_token` 填 N150 上 `.env` 里的 `HUB_TOKEN`。Bark key 不用在这里填（留空即可；填了的话 hub 连不上时会直推 Bark 作为备用）。
4. 测试 工作电脑 → N150 → iPhone：`python notify_agent.py --test`
5. 前台试跑：`python notify_agent.py`（试的时候可在管理页面把“离开多久才推送”临时改成“总是推送”）
6. 设为登录自启（后台运行、崩溃后 1 分钟自动重启）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
   ```
   更新代码后重启：`git pull`，然后 `Stop-ScheduledTask msg-notifier; Start-ScheduledTask msg-notifier`
   卸载：`Unregister-ScheduledTask -TaskName msg-notifier -Confirm:$false`

本地也会写 `notify_agent.log`；调试应用的弹窗截图在本地 `debug_shots/`（最多 100 张）。

## 诊断工具（工作电脑）

- `python notify_agent.py --diagnose`：列出通知数据库里最近的记录（toast / badge / tile）和来源应用 ID。
- `python probe.py --seconds 180`：记录飞书/Teams 的任务栏闪烁、窗口弹出、标题、托盘变化、弹窗文字和 OCR 结果，输出到 `probe_output.txt`。

OCR 用 Windows 自带的 `Windows.Media.Ocr`（`ocr.ps1`）。识别中文需要中文 OCR 语言包（管理员 PowerShell）：

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~zh-CN~0.0.1.0"
```

把飞书、Teams 的托盘图标设为常显（设置 → 个性化 → 任务栏 → 其他系统托盘图标），否则托盘兜底检测看不到。电脑要关闭自动睡眠（锁屏没关系）。
