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


def parse_teams_items(items: list[tuple[str, str, int]]) -> tuple[str, str] | None:
    """Teams 弹窗：items 为 [(控件类型, 文字, 深度)]。

    实测结构（2026-11）：Text@16 是“消息+标签+发送人”拼起来的汇总；Group@17 是
    “External unfamiliar”之类的标签组，里面 Text@18 是标签；Text@17 依次是发送人、消息。
    规则：去掉汇总文字后，取深度最浅的那一层 Text：第一段是发送人，其余是消息。
    """
    texts = [(n.strip(), d) for t, n, d in items if t == "Text" and n and n.strip()]
    names = [n for n, _ in texts]
    leaf = [(n, d) for n, d in texts if sum(1 for o in names if o != n and o in n) < 2]
    if not leaf:
        return None
    top = min(d for _, d in leaf)
    layer = []
    for n, d in leaf:
        if d == top and n not in layer:
            layer.append(n)
    if len(layer) < 2:
        return None
    return layer[0], "\n".join(layer[1:])


def is_ignored_popup(texts: list[str], words: list[str], min_hits: int = 2) -> bool:
    """弹窗是否是应忽略的界面（如飞书划词工具栏 “AI Search / Translate / Explain / Copy”）。

    texts：UIA 读到的各段文字（整段比较），或 OCR 的各行（子串比较，OCR 常把 l 认成 I，所以先统一）。
    命中 words 中至少 min_hits 个不同的词就算。
    """
    norm = lambda s: s.lower().replace("i", "l").replace(" ", "")  # noqa: E731
    wanted = {norm(w) for w in words if w}
    if not wanted:
        return False
    items = [norm(t) for t in texts if t and t.strip()]
    exact = {t for t in items if t in wanted}
    if len(exact) >= min_hits:
        return True
    joined = " ".join(items)
    return sum(1 for w in wanted if w in joined) >= max(min_hits, 3)
