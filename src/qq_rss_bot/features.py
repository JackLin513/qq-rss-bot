"""按群的「功能开关」—— 每个群单独决定收不收某个功能的推送。

**群列表的唯一来源是数据库**（`group_features` 表）。`.env` 里**没有**群参数，
`Config` 里也**没有** `*_groups` 字段。

## 为什么不能放 `.env`

`.env` 是 compose 的 `env_file`，环境变量在**创建容器时**烘进去。三条都堵死了：

| 事实 | 位置 |
|---|---|
| `env_file: - .env` —— 变量在创建容器时烘进去 | `docker-compose.yml:41` |
| `load_dotenv()` **默认不覆盖**已存在的环境变量 | `config.py` |
| `Config` 是 `@dataclass(frozen=True)`，进程内改不了 | `config.py` |

所以改一次群权限就得 `up -d --force-recreate` 重建容器 —— 而**机器人没法重建自己
的容器**（要 root，而且那等于自杀）。「在群里点一下就把功能开了」在 `.env` 上
根本做不到。落库之后**下一拍就生效**，不用重启、不用碰 NAS 上的文件。

## 曾经有过「`.env` 只用于首次灌入」的方案，**已经废掉了**

那个方案里 `.env` 还留着 `*_GROUPS`，只是降级成灌入用的默认值。它的毛病是
**灌过之后改 `.env` 不再生效，而日志不会报错** —— 这个项目被这种静默失败咬过
好几次。既然群列表本来就该由库管，就别留半个在 `.env` 里。

现在：**库是唯一来源**，`.env` 里一个群参数都没有，那个陷阱也就不存在了。

代价：**全新部署（空库）默认一个群都不开**，得靠 `/set` 或 sqlite3 开。
"""

from __future__ import annotations

import logging

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


def check_feature(feature: str) -> str:
    """功能名不合法就抛。**别静默** —— 拼错了会变成「这个功能一个群都不发」，
    而症状是「今天没推」，从日志上看和「这个群关了」一模一样。"""
    if feature not in NAMES:
        raise ValueError(f"未知的功能名 {feature!r}，见 features.FEATURES")
    return feature


def groups_for(db, feature: str) -> tuple[str, ...]:
    """这个功能该发到哪些群。**每次调用都重新查库。**

    重新查是**故意的**：`/set` 改完要在下一拍就生效，不能构造时定死。
    """
    return db.feature_groups(check_feature(feature))


def describe(db, feature: str) -> str:
    """给启动日志用。"""
    return f"{len(groups_for(db, feature))} 个群"


def summarize(db) -> str:
    """一行说完所有功能的群数。**启动日志那行的唯一来源。**

    这行是「为什么这个功能没推」的第一判据：群数不对就是开关的问题，
    不是代码的问题。别删。
    """
    return "；".join(f"{NAMES[f]} {describe(db, f)}" for f in FEATURES)


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


def resolve_feature(token: str) -> str | None:
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
