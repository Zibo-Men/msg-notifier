"""把 Windows 通知中心收到的通知（Teams / 飞书 / Outlook …）转发到 Bark。

原理：Windows 把所有发到通知中心的 toast 存在 SQLite 数据库
  %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db
本脚本以只读方式每隔几秒查询新记录，解析出应用、标题、正文，推送到 Bark。
只用 Python 标准库，无需 pip install。

用法：
  python notify_agent.py --diagnose      列出最近的通知及其来源应用 ID（先跑这个）
  python notify_agent.py --test          发一条测试推送到 Bark
  python notify_agent.py                 常驻运行（建议用 pythonw.exe 后台跑）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import tempfile
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
    "min_interval": 10,              # 同一应用两次推送最小间隔（秒）
    "forward_unknown_apps": False,   # 不在 apps 列表里的应用是否也转发
    # 应用 ID 关键字（不区分大小写，匹配 --diagnose 看到的 App ID） -> 显示名
    "apps": {
        "MSTeams": "Teams",
        "OutlookForWindows": "Outlook",
        "Feishu": "飞书",
        "Lark": "Lark",
        "Bytedance": "飞书",
    },
    # 标题或正文包含这些词时不推送（例如会议提醒、自己发的）
    "ignore_keywords": [],
}

log = logging.getLogger("notify_agent")


# ---------------------------------------------------------------- 工具函数
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        HERE / "notify_agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
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
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0


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


# ---------------------------------------------------------------- 数据库
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
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        log.error("Bark 推送失败: %s", e)
        return False


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
            QUERY.replace("n.Id > ?", "1=1").replace("ORDER BY n.Id", "ORDER BY n.Id DESC LIMIT ?"),
            (limit,),
        ).fetchall()
        apps = con.execute(
            "SELECT h.PrimaryId, COUNT(n.Id) FROM NotificationHandler h "
            "LEFT JOIN Notification n ON n.HandlerId = h.RecordId "
            "GROUP BY h.RecordId HAVING COUNT(n.Id) > 0 ORDER BY COUNT(n.Id) DESC"
        ).fetchall()
    finally:
        con.close()
    print(f"=== 最近 {len(rows)} 条 toast 通知（新的在前）===")
    for rid, arrival, payload, app_id in rows:
        title, body = parse_payload(payload)
        t = filetime_to_dt(arrival)
        print(f"[{rid}] {t:%m-%d %H:%M:%S} " if t else f"[{rid}] ", end="")
        print(f"App ID: {app_id}")
        print(f"      标题: {title}")
        if body:
            print("      正文: " + body.replace("\n", " / ")[:150])
    print("\n=== 通知中心里有通知的应用 ===")
    for app_id, n in apps:
        print(f"{n:5d}  {app_id}")


def run(cfg: dict, db: NotificationDB) -> None:
    last_id = db.max_id()  # 启动时不补发历史通知
    last_push: dict[str, float] = {}
    log.info("启动，从 Id>%d 开始监控，idle_seconds=%s", last_id, cfg["idle_seconds"])
    while True:
        try:
            toasts = db.fetch_after(last_id)
        except Exception as e:
            log.error("读取通知数据库失败: %s", e)
            time.sleep(10)
            continue
        for t in toasts:
            last_id = max(last_id, t.id)
            name = app_name(cfg, t.app_id)
            if not name:
                log.info("忽略 [%s] %s", t.app_id, t.title)
                continue
            text = f"{t.title} {t.body}"
            if any(k in text for k in cfg["ignore_keywords"]):
                log.info("关键字忽略 [%s] %s", name, t.title)
                continue
            idle = idle_seconds()
            if idle < cfg["idle_seconds"]:
                log.info("在电脑前（空闲 %.0fs），不推送 [%s] %s", idle, name, t.title)
                continue
            now = time.time()
            if now - last_push.get(name, 0) < cfg["min_interval"]:
                log.info("限流跳过 [%s] %s", name, t.title)
                continue
            last_push[name] = now
            title = f"{name}: {t.title}" if t.title else name
            body = t.body if cfg["show_body"] else "有新消息"
            ok = bark_push(cfg, title, body, group=name)
            log.info("推送%s [%s] %s", "成功" if ok else "失败", name, t.title)
        time.sleep(cfg["poll_seconds"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diagnose", action="store_true", help="列出最近的通知和应用 ID")
    ap.add_argument("--limit", type=int, default=30, help="--diagnose 显示条数")
    ap.add_argument("--test", action="store_true", help="发送一条 Bark 测试推送")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="通知数据库路径（调试用）")
    args = ap.parse_args()

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    cfg = load_config()
    db = NotificationDB(args.db)

    if args.test:
        ok = bark_push(cfg, "msg-notifier 测试", "如果你在 iPhone 上看到这条，说明 Bark 配置正确", "test")
        print("推送成功" if ok else "推送失败，看上面的日志")
        return
    if not args.db.exists():
        sys.exit(f"找不到通知数据库: {args.db}")
    if args.diagnose:
        diagnose(db, args.limit)
        return
    run(cfg, db)


if __name__ == "__main__":
    main()
