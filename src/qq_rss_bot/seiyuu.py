"""声优 / 官号 / 角色本人账号的库 —— **只读**，运行时唯一的数据来源。

数据在 `seiyuu.toml`（进 git，由 `scripts/fetch_seiyuu*.py` 生成 + 人工补）。
这个模块只做三件事：读文件、判断一个 handle 属于哪一类、给出署名用的标签。

## 三类作者

| 类别 | 判定 | 标签 |
|---|---|---|
| 官号 | handle 在 `[officials]` 里 | `公式` |
| 本人 | handle 是**这个角色自己**的号（`[character_accounts]`） | `本人` |
| 声优 | handle 是**这个角色的现任声优**的号 | `声優` |
| 画师 | 以上都不匹配 | `画师` |

⚠️ 声优判定是「人 × **当天这个角色** × 现任」三者同时成立。
A 角色的声优给 B 角色画贺图，那是画师；前任声优同理。

⚠️ 「本人」和「声优」是**同一类**（都占「声优」那一格）—— vα-liv 这种
虚拟主播没有「另一个配音的人」，角色自己开号。所以只有三个类别，四个标签。

## 读不到就退回画师

文件缺失 / 格式坏 → WARNING + 返回 None，调用方按「全部是画师」处理。
**不崩、也不静默** —— 日志里必须看得见。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).with_name("seiyuu.toml")

CATEGORY_OFFICIAL = "official"
CATEGORY_PERSON = "person"
CATEGORY_ARTIST = "artist"

LABEL_OFFICIAL = "公式"
LABEL_CHARACTER = "本人"
LABEL_SEIYUU = "声優"
LABEL_ARTIST = "画师"


@dataclass(frozen=True)
class Book:
    """角色 → 现任声优 / 声优 → handle / 角色 → 本人 handle / 官号集合。"""

    roles: dict[str, str]
    handles: dict[str, str]
    character_handles: dict[str, str]
    officials: frozenset[str]


def load(path: Path | None = None) -> Book | None:
    """读库。读不到返回 None（调用方退回「全部是画师」）。"""
    p = path or DEFAULT_PATH
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _log.warning("贺图：声优库不存在（%s）—— 全部按画师处理", p)
        return None
    except Exception as exc:  # noqa: BLE001
        _log.warning("贺图：声优库读不了（%s: %s）—— 全部按画师处理",
                     type(exc).__name__, exc)
        return None

    persons = raw.get("persons") or {}
    roles = {name: (v or {}).get("current", "")
             for name, v in (raw.get("roles") or {}).items()}
    handles = {name: str((v or {}).get("handle", "")).lower()
               for name, v in persons.items() if (v or {}).get("handle")}
    character_handles = {name: str((v or {}).get("handle", "")).lower()
                         for name, v in (raw.get("character_accounts") or {}).items()
                         if (v or {}).get("handle")}
    officials = frozenset(str(k).lower() for k in (raw.get("officials") or {}))
    roles = {k: v for k, v in roles.items() if v}
    return Book(roles=roles, handles=handles,
                character_handles=character_handles, officials=officials)


def classify(book: Book | None, handle: str, character: str) -> tuple[str, str]:
    """→ (类别, 标签)。`book` 为 None 时一律画师（官方关键词兜底在调用方做）。"""
    h = (handle or "").lower()
    if book is None or not h:
        return CATEGORY_ARTIST, LABEL_ARTIST
    if h in book.officials:
        return CATEGORY_OFFICIAL, LABEL_OFFICIAL
    if book.character_handles.get(character) == h:
        return CATEGORY_PERSON, LABEL_CHARACTER
    person = book.roles.get(character)
    if person and book.handles.get(person) == h:
        return CATEGORY_PERSON, LABEL_SEIYUU
    return CATEGORY_ARTIST, LABEL_ARTIST
