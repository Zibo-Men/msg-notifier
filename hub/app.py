"""msg-notifier hub：运行在 N150 上，接收工作电脑的通知和日志，统一推送到 Bark。

页面：GET /  管理页面（设置、推送记录、日志）

接口（除 / 和 /healthz 外都需要 Authorization: Bearer <HUB_TOKEN>）：
  POST /api/notify        工作电脑提交一条要推送的消息
  POST /api/logs          工作电脑批量上传日志
  GET  /api/agent-config  工作电脑定期拉取的运行参数（?host= 顺便登记在线状态）
  GET  /api/settings      读取全部设置；PUT 覆盖保存
  GET  /api/status        Bark 设备数、各工作电脑最近上报时间、今日推送统计
  GET  /api/apps          已知应用列表（设置里的 + 推送记录里出现过的）
  POST /api/test          发一条测试推送（可选 {"app": "Teams"} 用该应用的声音/类型）
  GET  /api/events        推送记录（?limit= ?before_id= ?app= ?status=）
  GET  /api/logs          日志（?limit= ?before_id= ?q= ?host= ?level=）
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA / "hub.db"
SETTINGS_PATH = DATA / "settings.json"
STATIC = Path(__file__).resolve().parent / "static"
HUB_TOKEN = os.getenv("HUB_TOKEN", "")
BARK_SERVER = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
BARK_KEYS = [k.strip() for k in os.getenv("BARK_KEY", "").split(",") if k.strip()]
LOG_KEEP_DAYS = int(os.getenv("LOG_KEEP_DAYS", "30"))
EVENT_KEEP_DAYS = int(os.getenv("EVENT_KEEP_DAYS", "180"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("hub")

DEFAULT_SETTINGS = {
    "enabled": True,                      # 总开关
    "apps": {                             # 每个应用：enabled + 可选 bark 覆盖；未列出的应用默认开启
        "Teams": {"enabled": True},
        "飞书": {"enabled": True},
        "Outlook": {"enabled": True},
    },
    "bark": {
        "level": "timeSensitive",         # passive / active / timeSensitive / critical
        "call": False,                    # 持续响铃约 30 秒
        "sound": "",                      # Bark 内置铃声名，留空默认
        "volume": 5,                      # critical 时的音量 0-10
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
            CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, last_seen REAL, ip TEXT);
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


def _prune_loop() -> None:
    while True:
        time.sleep(6 * 3600)
        try:
            with _db_lock, closing(db()) as con:
                prune(con)
        except Exception as e:  # noqa: BLE001
            log.warning("清理旧数据失败: %s", e)


def record_event(host, app_, title, body, source, generic, status, detail="") -> None:
    with _db_lock, closing(db()) as con:
        con.execute(
            "INSERT INTO events(ts,host,app,title,body,source,generic,status,detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), host, app_, title, body, source, int(generic), status, detail),
        )
        con.commit()


def touch_host(host: str, request: Request) -> None:
    if not host:
        return
    ip = request.client.host if request.client else ""
    with _db_lock, closing(db()) as con:
        con.execute(
            "INSERT INTO hosts(host,last_seen,ip) VALUES(?,?,?) "
            "ON CONFLICT(host) DO UPDATE SET last_seen=excluded.last_seen, ip=excluded.ip",
            (host, time.time(), ip),
        )
        con.commit()


# ---------------------------------------------------------------- Bark
def effective_bark(s: dict, app_name: str) -> dict:
    """全局 bark 设置 + 该应用的覆盖（只覆盖填了值的项）。"""
    over = s["apps"].get(app_name, {}).get("bark", {}) or {}
    return deep_merge(s["bark"], {k: v for k, v in over.items() if v not in ("", None)})


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
            data["volume"] = str(bark.get("volume", 5))
        err = ""
        for attempt in range(3):
            try:
                r = httpx.post(f"{BARK_SERVER}/push", json=data, timeout=10)
                if r.status_code == 200:
                    err = ""
                    break
                err = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                err = str(e)
            time.sleep(1 + attempt)
        if err:
            errors.append(err)
    return (not errors), "; ".join(errors)


# ---------------------------------------------------------------- API
app = FastAPI(title="msg-notifier hub")


SESSION_COOKIE = "hub_session"
SESSION_MAX_AGE = 10 * 365 * 86400  # 10 年：登录一次长期有效


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def session_value() -> str:
    """Cookie 里存的是由令牌派生的值，不是令牌本身。改 HUB_TOKEN 会让所有登录失效。"""
    return hmac.new(HUB_TOKEN.encode(), b"msg-notifier-session", hashlib.sha256).hexdigest()


def set_session(response: Response) -> None:
    response.set_cookie(SESSION_COOKIE, session_value(), max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="strict", path="/")


def auth(response: Response, authorization: str = Header(default=""), hub_session: str = Cookie(default="")) -> None:
    """工作电脑用 Authorization: Bearer <令牌>；管理页面用登录后的长期 Cookie（每次使用自动续期）。"""
    if not HUB_TOKEN:
        raise HTTPException(500, "HUB_TOKEN 未配置")
    if same(authorization, f"Bearer {HUB_TOKEN}"):
        return
    if hub_session and same(hub_session, session_value()):
        set_session(response)  # 续期：浏览器可能把有效期截短（Chrome 最长 400 天）
        return
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


class TestReq(BaseModel):
    app: str = ""


class LoginReq(BaseModel):
    token: str


@app.on_event("startup")
def _startup() -> None:
    init_db()
    threading.Thread(target=_prune_loop, daemon=True).start()
    log.info("hub 启动：Bark 设备 %d 个，服务器 %s", len(BARK_KEYS), BARK_SERVER)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.post("/api/login")
def login(req: LoginReq, response: Response) -> dict:
    if not HUB_TOKEN or not same(req.token.strip(), HUB_TOKEN):
        time.sleep(1)  # 减缓暴力尝试
        raise HTTPException(401, "bad token")
    set_session(response)
    return {"ok": True}


@app.post("/api/logout")
def logout() -> JSONResponse:
    r = JSONResponse({"ok": True})
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "bark_keys": len(BARK_KEYS)}


@app.post("/api/notify", dependencies=[Depends(auth)])
def notify(n: Notify, request: Request) -> dict:
    touch_host(n.host, request)
    s = load_settings()
    app_cfg = s["apps"].get(n.app, {"enabled": True})
    if not s["enabled"]:
        status, detail = "muted", "总开关关闭"
    elif not app_cfg.get("enabled", True):
        status, detail = "muted", f"{n.app} 已关闭"
    else:
        bark = effective_bark(s, n.app)
        title = f"{n.app}: {n.title}" if n.title else n.app
        body = n.body if (bark.get("show_body", True) or n.generic) else "有新消息"
        ok, err = bark_push(title, body or "有新消息", n.app, bark)
        status, detail = ("pushed", "") if ok else ("error", err)
    record_event(n.host, n.app, n.title, n.body, n.source, n.generic, status, detail)
    log.info("notify [%s/%s] %s -> %s %s", n.app, n.source, n.title or n.body[:30], status, detail)
    return {"status": status, "detail": detail}


@app.post("/api/logs", dependencies=[Depends(auth)])
def post_logs(b: LogBatch, request: Request) -> dict:
    touch_host(b.host, request)
    with _db_lock, closing(db()) as con:
        con.executemany(
            "INSERT INTO logs(ts,host,level,msg) VALUES(?,?,?,?)",
            [(x.ts, b.host, x.level, x.msg[:4000]) for x in b.lines],
        )
        con.commit()
    return {"saved": len(b.lines)}


@app.get("/api/agent-config", dependencies=[Depends(auth)])
def agent_config(request: Request, host: str = "") -> dict:
    touch_host(host, request)
    return load_settings()["agent"]


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings() -> dict:
    return load_settings()


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(s: dict) -> dict:
    merged = deep_merge(DEFAULT_SETTINGS, s)
    # 用页面提交的 apps 原样替换（这样可以删除某个应用的覆盖项）
    if isinstance(s.get("apps"), dict):
        merged["apps"] = s["apps"]
    save_settings(merged)
    log.info("设置已更新")
    return merged


@app.get("/api/status", dependencies=[Depends(auth)])
def status() -> dict:
    day_start = time.time() - 86400
    with closing(db()) as con:
        hosts = [dict(r) for r in con.execute("SELECT * FROM hosts ORDER BY last_seen DESC")]
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM events WHERE ts >= ? GROUP BY status", (day_start,))}
        last = con.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
    return {"bark_keys": len(BARK_KEYS), "bark_server": BARK_SERVER, "hosts": hosts, "now": time.time(),
            "last24h": counts, "last_event_ts": last["ts"] if last else None}


@app.get("/api/apps", dependencies=[Depends(auth)])
def apps() -> list[str]:
    names = list(load_settings()["apps"].keys())
    with closing(db()) as con:
        for r in con.execute("SELECT DISTINCT app FROM events WHERE app NOT IN ('test','测试')"):
            if r["app"] and r["app"] not in names:
                names.append(r["app"])
    return names


@app.post("/api/test", dependencies=[Depends(auth)])
def test_push(t: TestReq | None = None) -> dict:
    s = load_settings()
    name = (t.app if t else "") or ""
    bark = effective_bark(s, name) if name else s["bark"]
    title = f"{name}: 测试推送" if name else "msg-notifier 测试"
    ok, err = bark_push(title, "这是从管理页面发出的测试，用于试听声音和提醒类型", name or "test", bark)
    record_event("hub", "test", title, "", "page-test", False, "pushed" if ok else "error", err)
    return {"ok": ok, "detail": err}


@app.get("/api/events", dependencies=[Depends(auth)])
def events(limit: int = 50, before_id: int = 0, app: str = "", status: str = "") -> list[dict]:
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if before_id:
        sql += " AND id < ?"
        args.append(before_id)
    if app:
        sql += " AND app = ?"
        args.append(app)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with closing(db()) as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


@app.get("/api/logs", dependencies=[Depends(auth)])
def get_logs(limit: int = 200, before_id: int = 0, q: str = "", host: str = "", level: str = "") -> list[dict]:
    sql, args = "SELECT * FROM logs WHERE 1=1", []
    if before_id:
        sql += " AND id < ?"
        args.append(before_id)
    if q:
        sql += " AND msg LIKE ?"
        args.append(f"%{q}%")
    if host:
        sql += " AND host = ?"
        args.append(host)
    if level:
        sql += " AND level IN (" + ",".join("?" * len(level.split(","))) + ")"
        args.extend(level.split(","))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 5000))
    with closing(db()) as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
