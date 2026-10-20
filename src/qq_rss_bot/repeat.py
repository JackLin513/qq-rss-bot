"""群复读机 —— 连着两个人发同样的话，机器人跟着复读一句。

用户 2026-10-20 定的规则：**第 2 个人**发出来才跟。这样机器人不会自己起头，
只在群里真的开始复读时才加入；也避免了「每条都跟」那种刷屏。

## 触发条件（`Repeater.consider`，五条全中才跟）

1. 群在 `REPEAT_GROUPS` 里
2. 这一条和**上一条**归一化之后完全相同
3. 但**不是同一个人**发的 —— 一个人连发两遍不算复读
4. 这一句**还没被跟过**（跟过就不再跟，直到群里出现别的文本）
5. 距上次复读 ≥ `REPEAT_GAP_SEC`

## 不跟的东西

- 空消息 / 纯空白
- 超过 `REPEAT_MAX_LEN` 的长文 —— 复读起来像刷屏
- 以 `/` 开头的指令（跟着复读一条指令很怪）
- **预览已经处理过的**：分享的 bilibili / YouTube 链接，机器人已经回过卡片了，
  再复读一遍很傻。由调用方传 `skip=True`。

## ⚠️ 防自激循环靠第 4 条

机器人自己发的消息**有可能被平台推回来**（取决于协议）。推回来的话：

    某人: X  →  机器人复读 X  →  这条又回来  →  又看见「另一个人」发了 X  →  再复读 …

第 4 条（`_repeated`）在这里是**唯一**的刹车 —— 同一句跟过一次就不再跟，
所以最坏情况也只多一句，不会无限循环。**别把那条去掉。**

## 状态只在内存里

复读是转瞬即逝的群行为，重启后忘掉上一条完全没问题。落库还要考虑清理，不值当。
代价：重启后群里正在复读的那一句要重新攒够两个人。
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

_log = logging.getLogger("qq_rss_bot.repeat")

# 长文复读起来像刷屏；而且 QQ 单条消息本来就有长度限制。
DEFAULT_MAX_LEN = 200
# 同一个群两次复读之间的最小间隔。防的是「一群人反复刷同一句」。
DEFAULT_GAP_SEC = 20.0

# @ 的两种形态都要剥掉 —— 同一句话里 @ 了不同的人，剥完才算「同一句」：
# `<@OPENID>` 是**指令面板按钮**补的那种（占位符里是 openid 不是 QQ 号），
# `@显示名` 是手工 @ 的。两种在群里都常见。
_PLACEHOLDER = re.compile(r"<@!?[0-9A-Za-z_-]+>")
_AT_NAME = re.compile(r"@\S+\s*")


def normalize(text: str) -> str:
    """判「是不是同一句」用的归一化：剥掉 @、把连续空白压成一个空格。

    ⚠️ 剥 `@xxx` 是有代价的：`@latiechi 这个不错` 会变成 `这个不错`。
    但对复读来说这是**想要**的 —— 两个人分别 @ 了不同的人、说了同一句话，
    群里看起来就是在复读。
    """
    body = _PLACEHOLDER.sub("", text or "")
    body = _AT_NAME.sub("", body)
    return " ".join(body.split())


class Repeater:
    """一个群一份状态。**纯逻辑，不碰网络** —— 发送由调用方负责。"""

    def __init__(
        self,
        groups: Iterable[str],
        *,
        max_len: int = DEFAULT_MAX_LEN,
        gap_sec: float = DEFAULT_GAP_SEC,
    ) -> None:
        self.groups = {g for g in groups if g}
        self.max_len = max_len
        self.gap_sec = gap_sec
        self._last: dict[str, tuple[str, str]] = {}   # 群 → (归一化文本, 发送人)
        self._repeated: dict[str, str] = {}           # 群 → 刚跟过的那句（刹车）
        self._at: dict[str, float] = {}               # 群 → 上次复读的时刻

    @property
    def enabled(self) -> bool:
        return bool(self.groups)

    def consider(
        self, group: str, text: str, sender: str, *, now: float, skip: bool = False,
    ) -> str | None:
        """要复读就返回**归一化之后**的那句，否则 None。

        **有副作用**（推进状态），所以同一个群里每条消息都要喂进来一次 ——
        不能只在「看起来像复读」的时候调。
        """
        if group not in self.groups:
            return None
        if skip:
            # 预览处理过的（分享链接）不参与复读，也**不能留在状态里** ——
            # 留着的话下一条正常消息会拿它当「上一条」，复读判定就错位了。
            self._last.pop(group, None)
            return None

        body = normalize(text)
        if not body or len(body) > self.max_len or body.startswith("/"):
            self._last.pop(group, None)
            return None

        prev = self._last.get(group)
        self._last[group] = (body, sender)
        if prev is None:
            return None

        prev_text, prev_sender = prev
        if body != prev_text or sender == prev_sender:
            # 换了一句 —— 之前那句的「跟过」标记可以清了，以后再复读还能跟。
            if body != self._repeated.get(group):
                self._repeated.pop(group, None)
            return None

        # 到这里 = **另一个人**发了和上一条一样的话
        if self._repeated.get(group) == body:
            return None                      # 第 4 条：这句跟过了，别再跟
        # ⚠️ `_at` 里没有记录 = 这个群**还没复读过**，第一次必须放行。
        # 写成 `.get(group, 0.0)` 是错的 —— 那等于拿 `now` 去和 0 比，
        # 生产里 `time.monotonic()` 起点很大所以侥幸能过，但换个时钟（测试里
        # 从 0 开始）就变成「距上次复读不到 N 秒」，第一次复读永远不触发。
        last_at = self._at.get(group)
        if last_at is not None and now - last_at < self.gap_sec:
            _log.info(
                "复读：%s 里 %r 命中，但距上次复读不到 %.0f 秒，这次不跟",
                group[:8], body[:24], self.gap_sec,
            )
            return None

        self._repeated[group] = body
        self._at[group] = now
        self._last.pop(group, None)          # 清掉 —— 第三个人不会再触发
        return body
