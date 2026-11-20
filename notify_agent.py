"""把工作电脑上 Teams / 飞书 / Outlook 等的新消息转发到 iPhone 上的 Bark。

两条检测通道：
1. Windows 通知数据库（wpndatabase.db）里的 toast —— Outlook 等走系统通知的应用
2. 窗口监测（win_watch.py）—— Teams、飞书等自己画弹窗的应用：
   弹窗出现时用 UI Automation 读内容，读不到就截图 OCR；任务栏闪烁、托盘变化作兜底

用法：
  python notify_agent.py --diagnose      列出最近的通知记录及来源应用 ID
  python notify_agent.py --test          发一条测试推送到 Bark
  python notify_agent.py                 常驻运行（建议用 install-task.ps1 后台自启）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Notifications/wpndatabase.db"
CONFIG_PATH = HERE / "config.json"

DEFAULT_CONFIG = {
    "bark_server": "https://api.day.app",
    "bark_key": "",
    "bark_level": "timeSensitive",   # active / timeSensitive / critical
    "bark_call": False,              # True = 持续响铃约 30 秒
    "bark_sound": "",                # 留空用默认铃声，可填 Bark 内置铃声名
    "show_body": True,               # False = 只推“谁发来消息”，不带正文
    "poll_seconds": 3,
    "idle_seconds": 300,             # 键鼠超过多少秒没动才转发；0 = 总是转发
    "min_interval": 3,               # 同一应用两条具体消息的最小间隔（秒）
    "generic_interval": 60,          # 同一应用两条“有新消息”兜底推送的最小间隔（秒）
    "forward_unknown_apps": False,   # 不在 apps 列表里的应用是否也转发
    # 系统通知（toast）的应用 ID 关键字（不区分大小写） -> 显示名
    "apps": {
        "OutlookForWindows": "Outlook",
        "MSTeams": "Teams",
        "Feishu": "飞书",
        "Lark": "飞书",
    },
    # 自绘弹窗的应用：进程名、托盘提示关键字、弹窗尺寸范围（物理像素）、解析器
    "window_watch": True,
    "window_apps": [
        {"name": "Teams", "processes": ["ms-teams.exe"], "tray_keywords": ["Microsoft Teams"],
         "parser": "teams", "popup_min": [300, 100], "popup_max": [700, 450]},
        {"name": "飞书", "processes": ["lark.exe", "feishu.exe"], "tray_keywords": ["ChintOA", "飞书", "Feishu", "Lark"],
         "parser": "generic", "popup_min": [150, 50], "popup_max": [800, 600]},
    ],
    "debug_apps": ["飞书"],          # 记录详细事件、UIA/OCR 原文和弹窗截图（debug_shots/）
    "ocr_fallback": True,
    "ocr_lang": "zh-Hans-CN",
    "generic_delay": 5,              # 兜底信号等几秒，若期间已推过具体内容则不再推
    "tray_poll_seconds": 3,
    # 标题或正文包含这些词时不推送
    "ignore_keywords": [],
}

log = logging.getLogger("notify_agent")


# ---------------------------------------------------------------- 工具函数
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    return cfg


def setup_logging(debug: bool = False) -> None:
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        HERE / "notify_agent.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:  # pythonw 下没有 stdout
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def filetime_to_dt(ft: int | None) -> datetime | None:
    if not ft:
        return None
    return datetime.fromtimestamp((ft - 116444736000000000) / 10_000_000, tz=timezone.utc).astimezone()


def idle_seconds() -> float:
    """距离上次键盘/鼠标输入的秒数（锁屏时也会持续增长）。非 Windows 返回极大值。"""
    if os.name != "nt":
        return 1e9

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def parse_payload(payload) -> tuple[str, str]:
    """从 toast XML 里取出 <text> 文本：第一行当标题，其余当正文。"""
    if payload is None:
        return "", ""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return "", payload[:200]
    texts = [(t.text or "").strip() for t in root.iter("text")]
    texts = [t for t in texts if t]
    if not texts:
        return "", ""
    return texts[0], "\n".join(texts[1:])


# ---------------------------------------------------------------- 通知数据库
@dataclass
class Toast:
    id: int
    app_id: str
    arrival: datetime | None
    title: str
    body: str


QUERY = """
SELECT n.Id, n.ArrivalTime, n.Payload, IFNULL(h.PrimaryId, '')
FROM Notification n
LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId
WHERE n.Type = 'toast' AND n.Id > ?
ORDER BY n.Id
"""


class NotificationDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._tmp = Path(tempfile.gettempdir()) / "notify_agent_db"

    def _connect(self) -> sqlite3.Connection:
        try:
            con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
            con.execute("SELECT 1 FROM Notification LIMIT 1")
            return con
        except sqlite3.Error as e:
            # 只读直连失败（被锁等）时，复制一份（连同 WAL）再读
            log.debug("直连失败，改用副本: %s", e)
            self._tmp.mkdir(exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.path) + suffix)
                if src.exists():
                    shutil.copy2(src, self._tmp / ("wpndatabase.db" + suffix))
            return sqlite3.connect(self._tmp / "wpndatabase.db", timeout=5)

    def fetch_after(self, last_id: int) -> list[Toast]:
        con = self._connect()
        try:
            rows = con.execute(QUERY, (last_id,)).fetchall()
        finally:
            con.close()
        out = []
        for rid, arrival, payload, app_id in rows:
            title, body = parse_payload(payload)
            out.append(Toast(rid, app_id, filetime_to_dt(arrival), title, body))
        return out

    def max_id(self) -> int:
        con = self._connect()
        try:
            return con.execute("SELECT IFNULL(MAX(Id), 0) FROM Notification").fetchone()[0]
        finally:
            con.close()


# ---------------------------------------------------------------- Bark
def bark_push(cfg: dict, title: str, body: str, group: str) -> bool:
    if not cfg["bark_key"]:
        log.warning("bark_key 未配置，跳过推送: %s | %s", title, body[:60])
        return False
    data = {
        "device_key": cfg["bark_key"],
        "title": title[:100],
        "body": (body or " ")[:500],
        "group": group,
        "level": cfg["bark_level"],
    }
    if cfg["bark_call"]:
        data["call"] = "1"
    if cfg["bark_sound"]:
        data["sound"] = cfg["bark_sound"]
    if cfg["bark_level"] == "critical":
        data["volume"] = "5"
    req = urllib.request.Request(
        cfg["bark_server"].rstrip("/") + "/push",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            return True
        except Exception as e:  # noqa: BLE001
            log.error("Bark 推送失败（第 %d 次）: %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    return False


class Notifier:
    """所有检测通道都调用 submit()：统一做空闲判断、去重、限流，再异步推送。

    generic=True 表示只知道“有新消息”的兜底信号：若该应用最近已推过具体内容，
    或者兜底推送过于频繁，就不推。
    """

    def __init__(self, cfg: dict, push=bark_push) -> None:
        self.cfg = cfg
        self.push = push
        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue()
        self._content_at: dict[str, float] = {}
        self._generic_at: dict[str, float] = {}
        self._recent: dict[tuple, float] = {}
        threading.Thread(target=self._worker, daemon=True).start()

    def last_content_at(self, app: str) -> float:
        with self.lock:
            return self._content_at.get(app, 0.0)

    def _worker(self) -> None:
        while True:
            title, body, group = self.q.get()
            ok = self.push(self.cfg, title, body, group)
            log.info("推送%s: %s | %s", "成功" if ok else "失败", title, body.replace("\n", " / ")[:80])

    def submit(self, app: str, title: str, body: str, source: str, generic: bool = False,
               now: float | None = None, idle: float | None = None) -> bool:
        cfg = self.cfg
        text = f"{title} {body}"
        if any(k in text for k in cfg["ignore_keywords"]):
            log.info("关键字忽略 [%s/%s] %s", app, source, title)
            return False
        now = time.time() if now is None else now
        with self.lock:
            if generic:
                since_content = now - self._content_at.get(app, 0)
                since_generic = now - self._generic_at.get(app, 0)
                if since_content < max(15, cfg["generic_delay"] + 10) or since_generic < cfg["generic_interval"]:
                    log.debug("兜底信号被抑制 [%s/%s]", app, source)
                    return False
            else:
                key = (app, title, body)
                if now - self._recent.get(key, 0) < 60:
                    log.info("重复消息跳过 [%s/%s] %s", app, source, title)
                    return False
                if now - self._content_at.get(app, 0) < cfg["min_interval"]:
                    log.info("限流跳过 [%s/%s] %s", app, source, title)
                    return False
            idle = idle_seconds() if idle is None else idle
            if idle < cfg["idle_seconds"]:
                log.info("在电脑前（空闲 %.0fs），不推送 [%s/%s] %s", idle, app, source, title)
                # 记录时间，避免离开后兜底信号又补推这一条
                (self._generic_at if generic else self._content_at)[app] = now
                if not generic:
                    self._recent[(app, title, body)] = now
                return False
            if generic:
                self._generic_at[app] = now
            else:
                self._content_at[app] = now
                self._recent[(app, title, body)] = now
                if len(self._recent) > 500:
                    self._recent = {k: v for k, v in self._recent.items() if now - v < 120}
        push_title = f"{app}: {title}" if title else app
        push_body = body if (cfg["show_body"] or generic) else "有新消息"
        log.info("提交推送 [%s/%s] %s", app, source, title or body)
        self.q.put((push_title, push_body or " ", app))
        return True


# ---------------------------------------------------------------- 主逻辑
def app_name(cfg: dict, app_id: str) -> str | None:
    low = app_id.lower()
    for key, name in cfg["apps"].items():
        if key.lower() in low:
            return name
    if cfg["forward_unknown_apps"]:
        return app_id.split("!")[-1] or "未知应用"
    return None


def diagnose(db: NotificationDB, limit: int) -> None:
    con = db._connect()
    try:
        rows = con.execute(
            "SELECT n.Id, n.ArrivalTime, n.Payload, IFNULL(h.PrimaryId, ''), n.Type FROM Notification n "
            "LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId ORDER BY n.Id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        apps = con.execute(
            "SELECT h.PrimaryId, n.Type, COUNT(n.Id) FROM NotificationHandler h "
            "JOIN Notification n ON n.HandlerId = h.RecordId "
            "GROUP BY h.RecordId, n.Type ORDER BY COUNT(n.Id) DESC"
        ).fetchall()
    finally:
        con.close()
    print(f"=== 最近 {len(rows)} 条通知记录（所有类型，新的在前）===")
    for rid, arrival, payload, app_id, typ in rows:
        title, body = parse_payload(payload)
        t = filetime_to_dt(arrival)
        print(f"[{rid}] {t:%m-%d %H:%M:%S} " if t else f"[{rid}] ", end="")
        print(f"type={typ}  App ID: {app_id}")
        if title or body:
            print(f"      标题: {title}")
            if body:
                print("      正文: " + body.replace("\n", " / ")[:150])
        else:
            raw = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
            print("      原始: " + raw.replace("\n", " ")[:200])
    print("\n=== 各应用的通知记录数（按类型）===")
    for app_id, typ, n in apps:
        print(f"{n:5d}  {typ:<6} {app_id}")


def db_loop(cfg: dict, db: NotificationDB, notifier: Notifier) -> None:
    last_id = db.max_id()  # 启动时不补发历史通知
    log.info("通知数据库监测已启动，从 Id>%d 开始", last_id)
    while True:
        try:
            toasts = db.fetch_after(last_id)
        except Exception as e:  # noqa: BLE001
            log.error("读取通知数据库失败: %s", e)
            time.sleep(10)
            continue
        for t in toasts:
            last_id = max(last_id, t.id)
            name = app_name(cfg, t.app_id)
            if not name:
                log.info("忽略 toast [%s] %s", t.app_id, t.title)
                continue
            notifier.submit(name, t.title, t.body, source="toast")
        time.sleep(cfg["poll_seconds"])


def run(cfg: dict, db: NotificationDB) -> None:
    notifier = Notifier(cfg)
    log.info("启动：idle_seconds=%s，window_watch=%s", cfg["idle_seconds"], cfg["window_watch"])
    if db.path.exists():
        threading.Thread(target=db_loop, args=(cfg, db, notifier), daemon=True).start()
    else:
        log.error("找不到通知数据库 %s，只做窗口监测", db.path)
    if cfg["window_watch"] and os.name == "nt":
        from win_watch import WindowWatcher  # 只在 Windows 上导入

        WindowWatcher(cfg, notifier).run()  # 阻塞：Windows 消息循环
    else:
        while True:
            time.sleep(3600)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diagnose", action="store_true", help="列出最近的通知记录和应用 ID")
    ap.add_argument("--limit", type=int, default=30, help="--diagnose 显示条数")
    ap.add_argument("--test", action="store_true", help="发送一条 Bark 测试推送")
    ap.add_argument("--debug", action="store_true", help="日志里也记录被抑制的信号等细节")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="通知数据库路径（调试用）")
    args = ap.parse_args()

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging(args.debug)
    cfg = load_config()
    db = NotificationDB(args.db)

    if args.test:
        ok = bark_push(cfg, "msg-notifier 测试", "如果你在 iPhone 上看到这条，说明 Bark 配置正确", "test")
        print("推送成功" if ok else "推送失败，看上面的日志")
        return
    if args.diagnose:
        if not args.db.exists():
            sys.exit(f"找不到通知数据库: {args.db}")
        diagnose(db, args.limit)
        return
    run(cfg, db)


if __name__ == "__main__":
    main()
