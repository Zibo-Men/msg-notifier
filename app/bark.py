"""Bark 推送（https://github.com/Finb/Bark）。"""
import logging
import os

import httpx

log = logging.getLogger("bark")


class Bark:
    def __init__(self) -> None:
        self.server = os.getenv("BARK_SERVER", "https://api.day.app").rstrip("/")
        self.key = os.getenv("BARK_KEY", "")
        self.level = os.getenv("BARK_LEVEL", "timeSensitive")
        self.call = os.getenv("BARK_CALL", "0") == "1"
        if not self.key:
            log.warning("BARK_KEY 未设置，只打印日志不推送")

    async def push(self, title: str, body: str, group: str, level: str | None = None) -> None:
        log.info("PUSH [%s] %s | %s", group, title, body[:80])
        if not self.key:
            return
        payload = {
            "device_key": self.key,
            "title": title[:100],
            "body": body[:500] or " ",
            "group": group,
            "level": level or self.level,
        }
        if self.call:
            payload["call"] = "1"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self.server}/push", json=payload)
                r.raise_for_status()
        except Exception as e:  # 推送失败不能让主循环崩
            log.error("Bark 推送失败: %s", e)
