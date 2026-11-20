"""弹窗文字清洗等纯函数（不依赖 Windows，便于测试）。"""
from __future__ import annotations

import re

SKIP_NAMES = {
    "chrome legacy window", "more options", "send reaction", "dismiss notification", "reply",
    "send a quick reply", "close", "关闭", "回复", "快速回复", "更多", "忽略", "查看",
}
SKIP_TYPES = {"Button", "Edit", "Pane", "Document", "Window", "Image", "ScrollBar", "MenuItem", "TitleBar"}


def clean_lines(items: list[tuple[str, str]], extra_skip: set[str] = frozenset()) -> list[str]:
    """items: [(控件类型, 文字)]。去掉按钮、URL、框架文字，以及把其它几段拼在一起的汇总文字。"""
    skip = {s.lower() for s in SKIP_NAMES | set(extra_skip)}
    lines: list[str] = []
    for typ, name in items:
        n = (name or "").strip()
        low = n.lower()
        if not n or typ in SKIP_TYPES or low in skip or low.startswith("http") or "web content" in low:
            continue
        if n not in lines:
            lines.append(n)
    # 去掉“汇总”行：包含了另外至少两行的文字（Teams 的 Group 名字就是这样拼出来的）
    return [x for x in lines if sum(1 for o in lines if o != x and o in x) < 2]


def clean_ocr(text: str, extra_skip: set[str] = frozenset()) -> list[str]:
    skip = {s.lower() for s in SKIP_NAMES | set(extra_skip)}
    out = []
    for line in text.replace("\r", "").split("\n"):
        s = line.strip()
        if not s or s.startswith("## ") or s.lower() in skip:
            continue
        out.append(s)
    return out


UNREAD_RE = re.compile(r"[\(（\[](\d+)[\)）\]]")


def unread_in_title(title: str) -> int:
    m = UNREAD_RE.search(title or "")
    return int(m.group(1)) if m else 0
