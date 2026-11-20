"""msg-notifier hub：运行在 N150 上，接收工作电脑的通知和日志，统一推送到 Bark。

接口（除 /healthz 外都需要 Authorization: Bearer <HUB_TOKEN>）：
  POST /api/notify        工作电脑提交一条要推送的消息
  POST /api/logs          工作电脑批量上传日志
  GET  /api/agent-config  工作电脑定期拉取的运行参数（idle_seconds、debug_apps 等）
  GET  /api/settings      读取全部设置；PUT 覆盖保存（给以后的管理页面用）
  POST /api/test          发一条测试推送
  GET  /api/events        最近的推送记录
  GET  /api/logs          最近的日志（?q= 关键字，?host=）
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

DATA = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA / "hub.db"
SETTINGS_PATH = DATA / "settings.json"
HUB_TOKEN = os.getenv("HUB_TOKEN", "")
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
BARK_KEYS = [k.strip() for k in os.getenv("BARK_KEY", "").split(",") if k.strip()]
LOG_KEEP_DAYS = int(os.getenv("LOG_KEEP_DAYS", "30"))
EVENT_KEEP_DAYS = int(os.getenv("EVENT_KEEP_DAYS", "180"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hub")

DEFAULT_SETTINGS = {
    "enabled": True,                      # 总开关
    "apps": {                             # 每个应用的开关；未列出的应用默认开启
        "Teams": {"enabled": True},
        "飞书": {"enabled": True},
        "Outlook": {"enabled": True},
    },
    "bark": {
        "level": "timeSensitive",         # active / timeSensitive / critical / passive
        "call": False,                    # 持续响铃约 30 秒
        "sound": "",                      # Bark 内置铃声名，留空默认
        "show_body": True,                # False = 只推“谁发来消息”
    },
    "agent": {                            # 下发给工作电脑的参数，覆盖它本地 config.json
        "idle_seconds": 300,
        "debug_apps": ["飞书"],
    },
}

_settings_lock = threading.Lock()
_db_lock = threading.Lock()


# ---------------------------------------------------------------- 存储
def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    with _settings_lock:
        if SETTINGS_PATH.exists():
            return deep_merge(DEFAULT_SETTINGS, json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(s: dict) -> None:
    with _settings_lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with _db_lock, closing(db()) as con:
        con.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, ts REAL, host TEXT, app TEXT, title TEXT, body TEXT,
              source TEXT, generic INTEGER, status TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS logs(
              id INTEGER PRIMARY KEY, ts REAL, host TEXT, level TEXT, msg TEXT);
            CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
            """
        )
        prune(con)
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)


def prune(con: sqlite3.Connection) -> None:
    now = time.time()
    con.execute("DELETE FROM logs WHERE ts < ?", (now - LOG_KEEP_DAYS * 86400,))
    con.execute("DELETE FROM events WHERE ts < ?", (now - EVENT_KEEP_DAYS * 86400,))
    con.commit()


def record_event(host, app_, title, body, source, generic, status, detail="") -> None:
    with _db_lock, closing(db()) as con:
        con.execute(
            "INSERT INTO events(ts,host,app,title,body,source,generic,status,detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), host, app_, title, body, source, int(generic), status, detail),
        )
        con.commit()


# ---------------------------------------------------------------- Bark
def bark_push(title: str, body: str, group: str, bark: dict) -> tuple[bool, str]:
    if not BARK_KEYS:
        return False, "BARK_KEY 未配置"
    errors = []
    for key in BARK_KEYS:
        data = {"device_key": key, "title": title[:100], "body": (body or " ")[:1000],
                "group": group, "level": bark.get("level", "timeSensitive")}
        if bark.get("call"):
            data["call"] = "1"
        if bark.get("sound"):
            data["sound"] = bark["sound"]
        if data["level"] == "critical":
            data["volume"] = "5"
        for attempt in range(3):
            try:
                r = httpx.post(f"{BARK_SERVER}/push", json=data, timeout=10)
                if r.status_code == 200:
                    break
                err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                err = str(e)
            time.sleep(1 + attempt)
        else:
            errors.append(err)
    return (not errors), "; ".join(errors)


# ---------------------------------------------------------------- API
app = FastAPI(title="msg-notifier hub")


def auth(authorization: str = Header(default="")) -> None:
    if not HUB_TOKEN:
        raise HTTPException(500, "HUB_TOKEN 未配置")
    if authorization != f"Bearer {HUB_TOKEN}":
        raise HTTPException(401, "bad token")


class Notify(BaseModel):
    app: str
    title: str = ""
    body: str = ""
    source: str = ""
    generic: bool = False
    host: str = ""


class LogLine(BaseModel):
    ts: float
    level: str = "INFO"
    msg: str


class LogBatch(BaseModel):
    host: str = ""
    lines: list[LogLine]


@app.on_event("startup")
def _startup() -> None:
    init_db()
    log.info("hub 启动：Bark 设备 %d 个，服务器 %s", len(BARK_KEYS), BARK_SERVER)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "bark_keys": len(BARK_KEYS)}


@app.post("/api/notify", dependencies=[Depends(auth)])
def notify(n: Notify) -> dict:
    s = load_settings()
    app_cfg = s["apps"].get(n.app, {"enabled": True})
    if not s["enabled"]:
        status, detail = "muted", "总开关关闭"
    elif not app_cfg.get("enabled", True):
        status, detail = "muted", f"{n.app} 已关闭"
    else:
        bark = deep_merge(s["bark"], app_cfg.get("bark", {}))  # 允许按应用覆盖铃声/级别
        title = f"{n.app}: {n.title}" if n.title else n.app
        body = n.body if (bark.get("show_body", True) or n.generic) else "有新消息"
        ok, err = bark_push(title, body or "有新消息", n.app, bark)
        status, detail = ("pushed", "") if ok else ("error", err)
    record_event(n.host, n.app, n.title, n.body, n.source, n.generic, status, detail)
    log.info("notify [%s/%s] %s -> %s %s", n.app, n.source, n.title or n.body[:30], status, detail)
    return {"status": status, "detail": detail}


@app.post("/api/logs", dependencies=[Depends(auth)])
def post_logs(b: LogBatch) -> dict:
    with _db_lock, closing(db()) as con:
        con.executemany(
            "INSERT INTO logs(ts,host,level,msg) VALUES(?,?,?,?)",
            [(x.ts, b.host, x.level, x.msg[:4000]) for x in b.lines],
        )
        con.commit()
    return {"saved": len(b.lines)}


@app.get("/api/agent-config", dependencies=[Depends(auth)])
def agent_config() -> dict:
    return load_settings()["agent"]


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings() -> dict:
    return load_settings()


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(s: dict) -> dict:
    merged = deep_merge(DEFAULT_SETTINGS, s)
    save_settings(merged)
    return merged


@app.post("/api/test", dependencies=[Depends(auth)])
def test_push() -> dict:
    ok, err = bark_push("msg-notifier 测试", "看到这条说明 N150 → Bark → iPhone 链路正常", "test",
                        load_settings()["bark"])
    record_event("hub", "test", "测试", "", "api-test", False, "pushed" if ok else "error", err)
    return {"ok": ok, "detail": err}


@app.get("/api/events", dependencies=[Depends(auth)])
def events(limit: int = 50) -> list[dict]:
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/logs", dependencies=[Depends(auth)])
def get_logs(limit: int = 200, q: str = "", host: str = "") -> list[dict]:
    sql, args = "SELECT * FROM logs WHERE 1=1", []
    if q:
        sql += " AND msg LIKE ?"
        args.append(f"%{q}%")
    if host:
        sql += " AND host = ?"
        args.append(host)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 5000))
    with closing(db()) as con:
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]
