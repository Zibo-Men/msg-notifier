# msg-notifier

常驻 Docker 容器：用 Playwright 驱动一个**有界面**的 Chromium（跑在 Xvfb 虚拟显示上），同时打开 Lark / Teams / Gmail 网页版，捕获“有人给我发消息”，经 Bark 推送到 iPhone。

> 状态：**原型 / 可行性验证阶段**，还没构建运行过。

## 架构

```
┌──────────────── msg-notifier 容器 ────────────────┐
│ Xvfb :99 ── Chromium (持久 profile: ./data/profile)│
│   ├─ Lark 标签页  ┐                                │
│   ├─ Teams 标签页 ├─ hook.js 拦截 Notification ──┐ │
│   └─ Gmail 标签页 ┘   + 轮询标题未读数 "(3) …"   │ │
│ x11vnc + noVNC :6080 ← 首次登录/排错             ▼ │
│                                  main.py → Bark ───┼──► iPhone
└────────────────────────────────────────────────────┘
```

- **检测通道 1（首选）**：注入 `hook.js` 改写 `window.Notification` 和 `ServiceWorkerRegistration.showNotification`，网页想弹桌面通知时把标题/正文交给 Python。
- **检测通道 2（兜底）**：每 10 秒读页面标题里的未读数，上升就推“N 条未读”。
- **登录失效**：页面被重定向到登录域名时推送“请重新登录”（每小时最多一次）。
- **登录方式**：不自动填密码。首次启动后用浏览器打开 noVNC，手动登录（含 2FA），登录态保存在 `data/profile`。

## 端口

| 端口 | 用途 |
|---|---|
| 8101 (`NOVNC_PORT`) | noVNC：`http://192.168.0.119:8101/vnc.html` |

## 启动

```bash
cp .env.example .env   # 填 BARK_KEY、VNC_PASSWORD
docker compose up -d --build
docker compose logs -f --tail=100 msg-notifier
```

然后打开 noVNC，在各标签页里登录。

## 目录

- `app/main.py` 主循环；`app/hook.js` 通知拦截；`app/platforms.py` 平台列表；`app/bark.py` 推送
- `data/profile/` Chromium 用户数据（含登录 cookie，**勿提交、勿删除**）
