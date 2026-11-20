"""常驻浏览器：打开各平台网页版，捕获网页通知 + 标题未读数，转发到 Bark。

两条检测通道：
1. hook.js 拦截 Notification / showNotification —— 能拿到发送人和内容（首选）
2. 轮询 document.title 里的未读数，如 "(3) Teams" —— 兜底，只知道有新消息
另外检测是否被踢回登录页，登录失效时推送一次提醒。
"""
import asyncio
import logging
import os
import pathlib
import re
import time

from playwright.async_api import BrowserContext, Page, async_playwright

import platforms as P
from bark import Bark

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

PROFILE_DIR = "/data/profile"
HOOK_JS = (pathlib.Path(__file__).parent / "hook.js").read_text()
MIN_INTERVAL = int(os.getenv("MIN_INTERVAL", "20"))
POLL_SECONDS = 10
UNREAD_RE = re.compile(r"\((\d+)\)")

bark = Bark()
plats = P.enabled()
last_push: dict[str, float] = {}       # 平台 -> 上次推送时间
last_unread: dict[str, int] = {}       # 平台 -> 上次标题未读数
login_alerted: dict[str, float] = {}   # 平台 -> 上次登录失效提醒时间


async def push(p: P.Platform, title: str, body: str, force: bool = False) -> None:
    now = time.time()
    if not force and now - last_push.get(p.name, 0) < MIN_INTERVAL:
        log.info("限流跳过 [%s] %s", p.name, title)
        return
    last_push[p.name] = now
    await bark.push(f"{p.name}: {title}", body, group=p.name)


async def on_notify(source, data: dict) -> None:
    p = P.match(source["page"].url, plats)
    if not p:
        return
    await push(p, data.get("title") or "新消息", data.get("body") or "")


async def check_page(page: Page, p: P.Platform) -> None:
    url = page.url
    if any(h in url for h in p.login_hosts):
        if time.time() - login_alerted.get(p.name, 0) > 3600:
            login_alerted[p.name] = time.time()
            await bark.push(f"{p.name} 登录失效", "请打开 noVNC 重新登录", group="system", level="active")
        return
    try:
        title = await page.title()
    except Exception:
        return
    m = UNREAD_RE.search(title)
    n = int(m.group(1)) if m else 0
    prev = last_unread.get(p.name, 0)
    last_unread[p.name] = n
    # 未读数上升，且最近没有通过 hook 推过 -> 兜底推送
    if n > prev and time.time() - last_push.get(p.name, 0) > MIN_INTERVAL:
        await push(p, f"{n} 条未读", title)


async def open_pages(ctx: BrowserContext) -> dict[str, Page]:
    pages: dict[str, Page] = {}
    existing = list(ctx.pages)
    for p in plats:
        page = next((pg for pg in existing if P.match(pg.url, [p])), None)
        if page is None:
            page = existing.pop(0) if existing and existing[0].url == "about:blank" else await ctx.new_page()
            await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
        pages[p.name] = page
        log.info("已打开 %s: %s", p.name, page.url)
    return pages


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,  # 跑在 Xvfb 上，可通过 noVNC 看到和操作
            no_viewport=True,
            locale="zh-CN",
            timezone_id=os.getenv("TZ", "Asia/Shanghai"),
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--start-maximized",
                # 页面在后台时也不要节流定时器，否则消息到达会延迟
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        await ctx.expose_binding("__relayNotify", on_notify)
        await ctx.add_init_script(HOOK_JS)
        pages = await open_pages(ctx)
        log.info("启动完成，监控平台: %s", ", ".join(pages))

        hb = pathlib.Path("/tmp/heartbeat")
        while True:
            for p in plats:
                page = pages.get(p.name)
                if page is None or page.is_closed():
                    page = await ctx.new_page()
                    await page.goto(p.url, wait_until="domcontentloaded", timeout=60_000)
                    pages[p.name] = page
                await check_page(page, p)
            hb.touch()
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
