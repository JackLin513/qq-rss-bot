"""按群的「功能开关」—— 每个群单独决定收不收某个功能的推送。

## 为什么落库，而不是写在 `.env` 里

`.env` 是 **env_file**，环境变量在**创建容器时**烘进去
（`docker-compose.yml` 的 `env_file: - .env`，见 CLAUDE.md「改 .env 用 up -d」那条）
—— 所以改一次群权限就得 `up -d --force-recreate` 重建容器。

而「在群里点一下就把某个功能开了」在 `.env` 上**根本做不到**：机器人没法重建
自己的容器（要 root，而且那等于自杀）。落库之后**下一拍就生效**，
不用重启、不用碰 NAS 上的文件。

## `.env` 变成「首次灌入的默认值」

每个功能在库里**一行都没有**时，按 `.env` 灌一遍 `enabled=1`。
跑过一次之后 `.env` 就不再是事实来源。

## ⚠️ 灌过之后改 `.env` 不再生效

这正是这个项目被咬过好几次的「静默失败」形态 —— 改了 `MORNING_GROUPS`、
重启、什么都没变，**日志一行错都没有**。所以三件事缺一不可：

1. 启动时打一行说清群列表来自哪（`on_ready` 里那条「功能开关」）
2. `/set` 能看能改
3. 文档写死（`docs/group-features.md`）

## ⚠️ 关掉 = 写 `enabled=0`，**不是删行**

见 `db.feature_seeded()` 的说明 —— 删行会被判成「没灌过」，下次启动又按 `.env`
灌回来，用户会以为开关失灵。`db.set_group_feature()` 只 upsert。
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

_log = logging.getLogger("qq_rss_bot.features")

# 所有「按群开关」的功能。**这些名字是稳定标识**，写进库里 ——
# 改名等于数据迁移，别随手改。
FEATURES: tuple[str, ...] = (
    "morning",
    "monday",
    "tickets",
    "birthday",
    "birthday_art",
    "repeat",
)

# 功能名 → 人话。日志和 `/set` 用。**key 必须和 FEATURES 完全一致**（smoke 钉了）。
NAMES: dict[str, str] = {
    "morning": "早安语音",
    "monday": "月曜が近いよ",
    "tickets": "抽票提醒",
    "birthday": "生日推送",
    "birthday_art": "誕生祭贺图",
    "repeat": "复读机",
}


def groups_for(db, feature: str, default: Sequence[str]) -> tuple[str, ...]:
    """这个功能该发到哪些群。**每次调用都重新查库。**

    重新查是**故意的**：`/set` 改完要在下一拍就生效，不能构造时定死。

    库里没灌过就按 `default`（= `.env` 里的值）灌一遍。灌入是幂等的；
    `default` 为空时什么都不灌，下次调用还会再试 —— 这正好让
    「先把 `.env` 填上再重启」这条路留着。
    """
    if feature not in NAMES:
        # 名字写错会静默变成「这个功能一个群都不发」，所以宁可炸。
        raise ValueError(f"未知的功能名 {feature!r}，见 features.FEATURES")
    db.seed_feature(feature, default)
    return db.feature_groups(feature)


def describe(db, feature: str) -> str:
    """给启动日志用：这个功能的群列表现在是什么状态。"""
    if not db.feature_seeded(feature):
        return "库里还没灌过（.env 也没配？）"
    n = len(db.feature_groups(feature))
    return f"{n} 个群"


def render_settings(db, group_openid: str) -> str:
    """`/set` 的纯文本形态：这个群每个功能开没开。

    拿不到卡片通道时退回它（和 `menu.build_rss_text()` 同一个口径）。
    """
    state = db.all_group_features().get(group_openid, {})
    lines = ["本群的功能开关：", ""]
    for i, feature in enumerate(FEATURES, 1):
        mark = "✅ 开" if state.get(feature) else "⬜ 关"
        lines.append(f"{i}. {mark}  {NAMES[feature]}")
    lines += ["", "改：/set <编号> on|off（例：/set 1 off）"]
    return "\n".join(lines)


def resolve_feature(db, token: str) -> str | None:
    """`/set` 的参数 → 功能名。认**编号**（`1`）和**英文名**（`morning`）。

    不认中文名 —— 面板和按钮都放不下（`render_data.label` ≤10 显示宽度），
    与其做半套不如只留两种能写全的形态。
    """
    token = (token or "").strip()
    if not token:
        return None
    if token.isdigit():
        n = int(token)
        if 1 <= n <= len(FEATURES):
            return FEATURES[n - 1]
        return None
    low = token.lower()
    return low if low in NAMES else None


def parse_bool(token: str) -> bool | None:
    """`on`/`off`（也认 `开`/`关`、`1`/`0`、`true`/`false`）。认不出返回 None。"""
    t = (token or "").strip().lower()
    if t in ("on", "开", "1", "true", "yes", "y"):
        return True
    if t in ("off", "关", "0", "false", "no", "n"):
        return False
    return None


def defaults_from(cfg) -> dict[str, tuple[str, ...]]:
    """各功能在 `.env` 里的群列表 —— **只用于首次灌入**。

    集中在这一处，是因为启动自报状态和 `/set` 都要它；散在两处的话
    加一个新功能很容易漏掉一边（漏了的那边会静默变成「一个群都不发」）。
    """
    return {
        "morning": cfg.morning_groups,
        "monday": cfg.monday_groups,
        "tickets": cfg.ticket_groups,
        "birthday": cfg.birthday_groups,
        "birthday_art": cfg.birthday_art_groups,
        "repeat": cfg.repeat_groups,
    }


def all_groups(db, defaults: dict[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """一次把所有功能的群列表查出来。`on_ready` 自报状态用。"""
    return {f: groups_for(db, f, defaults.get(f, ())) for f in FEATURES}


def summarize(db, defaults: dict[str, Sequence[str]]) -> str:
    """一行说完所有功能的群数。启动日志用 —— **这行是「.env 灌过之后不生效」
    的唯一提示**，别删。"""
    parts = []
    for f in FEATURES:
        parts.append(f"{NAMES[f]} {describe(db, f)}")
    return "；".join(parts)
