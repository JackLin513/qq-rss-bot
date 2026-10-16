"""进群自动订阅。

用户基本上只会订阅偶像大师这一个源，所以让每个群一进来就有内容，
而不是每次都去群里敲一遍 `/sub`。

三条设计约束：

* **只插一行 `initialized=0` 的记录，不在这里抓源。** `fetch_feed` 最长要
  `fetch_timeout_sec`（默认 20 秒），在 botpy 的事件回调里干等会把分发循环
  卡住。轮询器下一拍（≤ `tick_sec` = 30 秒）会走 scheduler 的
  `if not sub.initialized:` 分支：建立 baseline（**不推送**，否则一上来就
  倒灌几十条旧文）并把真实标题写回去。所以 `/list` 里先看到的是路由串，
  半分钟后变成真名字 —— 这是预期行为，不是 bug。
* **启动补订阅只补「一个订阅都没有」的群。** 有订阅的群要么已经订了这个源
  （幂等，白补），要么是**故意没订** —— 一律补会把故意退掉的群重新订回来。
  退过群的不会受影响：退群时 `db.forget_group` 连 `groups` 那行一起删了。
* **欢迎语只能走主动消息。** 进群事件是 `GroupManageEvent`，
  `__slots__` 里**没有 `msg_id`**，发不了被动回复。而且新群多半还没开
  「机器人主动在群聊内发言」—— 那条消息会静默失败，所以只记日志，
  绝不让它影响订阅本身。
"""

from __future__ import annotations

import logging

from .config import Config
from .db import Database
from .quota import Pusher
from .rss import resolve_url

_log = logging.getLogger("qq_rss_bot.autosub")


class AutoSubscriber:
    def __init__(self, cfg: Config, db: Database, sender: Pusher) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        route = cfg.auto_subscribe_feed.strip()
        # 解析成完整 URL。留空 = 这个功能整个关掉。
        self.route = route
        self.url = resolve_url(route, cfg.rsshub_base) if route else None

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def subscribe(self, group_openid: str, *, only_if_empty: bool) -> bool:
        """给一个群加默认订阅，返回是否**真的新建**了。只碰数据库，不抓源。"""
        if self.url is None:
            return False
        if only_if_empty and self.db.count_subscriptions(group_openid) > 0:
            return False
        # title 先写路由串：轮询器首拍会用真实标题覆盖掉。
        # 写 None 的话 /list 和 /rss 里那一行就没名字可显示。
        _sub_id, created = self.db.add_subscription(
            group_openid, self.url, self.route, self.cfg.default_interval_sec
        )
        return created

    async def on_join(self, group_openid: str) -> bool:
        """机器人被拉进新群。`created` 为假（重放事件、或本来就订过）就不重复欢迎。"""
        created = self.subscribe(group_openid, only_if_empty=False)
        if created:
            await self._welcome(group_openid)
        return created

    async def _welcome(self, group_openid: str) -> None:
        text = (
            "已就绪 ✨\n"
            f"本群已自动订阅默认源：{self.route}\n"
            "有新内容会自动发到这里。发 /rss 打开按钮菜单，/help 看全部指令。"
        )
        try:
            # 走 sender 而不是直接打 API：和别的主动消息一样过令牌桶和每日配额
            await self.sender.send_text(group_openid, text)
        except Exception:  # noqa: BLE001 —— 网络层什么都可能抛
            # 最常见的原因是新群还没打开「机器人主动在群聊内发言」。
            # 订阅本身已经写库了，这条发不出去不影响功能。
            _log.warning(
                "群 %s 的欢迎语没发出去（多半是没开「机器人主动在群聊内发言」）",
                group_openid,
                exc_info=True,
            )

    def backfill(self) -> list[str]:
        """启动补订阅，返回被补上的群。**只补零订阅的群，且不发消息。**

        退群时 `db.forget_group` 会把 `groups` 那行删掉，所以这里遍历到的
        都是机器人**当前还在**的群。
        """
        if self.url is None:
            return []
        return [g for g in self.db.known_groups() if self.subscribe(g, only_if_empty=True)]
