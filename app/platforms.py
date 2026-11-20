"""各平台的入口 URL、域名和登录页特征。新增平台只需在这里加一项。"""
import os
from dataclasses import dataclass


@dataclass
class Platform:
    name: str
    url: str
    hosts: tuple[str, ...]        # 属于该平台的域名（用于把通知归到平台）
    login_hosts: tuple[str, ...]  # 跳到这些域名说明登录失效


PLATFORMS = {
    "lark": Platform(
        "Lark",
        os.getenv("LARK_URL", "https://www.larksuite.com/messenger/"),
        ("larksuite.com", "larkoffice.com", "feishu.cn"),
        ("accounts.larksuite.com", "accounts.feishu.cn", "passport.larksuite.com"),
    ),
    "teams": Platform(
        "Teams",
        os.getenv("TEAMS_URL", "https://teams.microsoft.com/v2/"),
        ("teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com"),
        ("login.microsoftonline.com", "login.live.com"),
    ),
    "gmail": Platform(
        "Gmail",
        os.getenv("GMAIL_URL", "https://mail.google.com/mail/u/0/#inbox"),
        ("mail.google.com",),
        ("accounts.google.com",),
    ),
}


def enabled() -> list[Platform]:
    names = [n.strip() for n in os.getenv("PLATFORMS", "lark,teams,gmail").split(",") if n.strip()]
    return [PLATFORMS[n] for n in names if n in PLATFORMS]


def match(url: str, plats: list[Platform]) -> Platform | None:
    for p in plats:
        if any(h in url for h in p.hosts):
            return p
    return None
