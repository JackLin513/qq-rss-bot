"""botpy 客户端：把 QQ 事件接到指令处理器和轮询器上。"""

from __future__ import annotations

import itertools
import logging
from typing import Any

import botpy
import httpx
from botpy.message import GroupMessage

from .commands import CommandContext, CommandHandler, is_bot_mentioned, strip_mention
from .config import Config
from .db import Database
from .scheduler import FeedPoller

_log = logging.getLogger("qq_rss_bot.bot")

# RSSHub 和很多源站都会拦默认 UA
_USER_AGENT = "qq-rss-bot/0.1 (+https://github.com/DIYgod/RSSHub)"


class RssBot(botpy.Client):
    def __init__(self, cfg: Config) -> None:
        super().__init__(
            intents=botpy.Intents(public_messages=True),
            is_sandbox=cfg.is_sandbox,
            # 给 HTTP 留够时间，botpy 默认 5s 对抓 RSS 太短
            timeout=cfg.fetch_timeout_sec + 5,
            log_level=logging.INFO,
        )
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.rss_http = httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        self.commands = CommandHandler(cfg, self.db, self.rss_http)
        # RssBot 自己就是 FeedPoller 的推送出口（实现了 send_text / send_rich）
        self.poller = FeedPoller(cfg, self.db, self.rss_http, self)
        self._poller_task: Any = None

    # ---------- 生命周期 ----------

    async def on_ready(self) -> None:
        _log.info("机器人「%s」已上线", self.robot.name)
        # 轮询器和 botpy 共用一个事件循环，在 on_ready 里挂上去最省事
        if self._poller_task is None:
            self._poller_task = self.loop.create_task(
                self.poller.run(), name="qq_rss_bot.poller"
            )

    async def close(self) -> None:
        self.poller.stop()
        if self._poller_task is not None:
            self._poller_task.cancel()
        await self.rss_http.aclose()
        self.db.close()
        await super().close()

    # ---------- 群事件 ----------

    async def on_group_add_robot(self, event) -> None:
        """机器人被拉进群。

        这是拿到 group_openid 的最佳时机 —— 主动推送必须要它，
        而且它不是群号，之前没记录过就只能等有人在群里 @ 一次。
        """
        group_openid = getattr(event, "group_openid", None)
        if not group_openid:
            _log.warning("group_add_robot 事件缺少 group_openid：%r", event)
            return
        self.db.touch_group(group_openid)
        _log.info("已记录新群 %s", group_openid)

    async def on_group_del_robot(self, event) -> None:
        group_openid = getattr(event, "group_openid", None)
        if not group_openid:
            return
        for sub in self.db.list_subscriptions(group_openid):
            self.db.remove_subscription(group_openid, sub.id)
        _log.info("已退出群 %s，相关订阅一并清理", group_openid)

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """老协议：只有被 @ 时平台才推，所以进来就一定是在 @ 机器人。"""
        await self._handle_group_message(message)

    async def on_group_message_create(self, message: GroupMessage) -> None:
        """新协议（2026）：平台推的是**全量群消息**，连闲聊都推。

        所以必须自己筛 @，否则群里任何人说话都会触发机器人。
        见 compat.py 了解为什么需要单独解析这个事件。
        """
        bot_name = getattr(self.robot, "name", None)
        if not is_bot_mentioned(message.content, message.mentions, bot_name):
            _log.debug("忽略非 @ 消息: %r", message.content)
            return
        await self._handle_group_message(message)

    async def _handle_group_message(self, message: GroupMessage) -> None:
        self.db.touch_group(message.group_openid)
        text = strip_mention(message.content, getattr(self.robot, "name", None))
        _log.info("群 %s 指令: %r", message.group_openid, text)
        ctx = CommandContext(
            group_openid=message.group_openid,
            reply=self._make_replier(message),
        )
        await self.commands.handle(ctx, text)

    # ---------- 内部工具 ----------

    def _make_replier(self, message: GroupMessage):
        """被动回复。

        同一条 msg_id 回复多次时 msg_seq 必须递增，否则后续消息会被丢弃，
        所以这里自己维护计数器，而不是用 message.reply()。
        """
        seq = itertools.count(1)

        async def reply(text: str) -> None:
            await self.api.post_group_message(
                group_openid=message.group_openid,
                msg_type=0,
                msg_id=message.id,
                msg_seq=next(seq),
                content=text,
            )

        return reply

    # ---------- 推送出口（FeedPoller 通过这两个方法发消息）----------

    async def send_text(self, group_openid: str, content: str) -> None:
        """主动发纯文字。

        不带 msg_id 就是主动消息，需要群里开启「机器人主动在群聊内发言」权限，
        否则接口会报错（错误码见日志）。
        """
        await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=0,
            content=content,
        )

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None:
        """主动发图文，两步走。

        一步只能带一张图：官方 media 字段是单个对象，传数组会 400。
        url 给的是图片地址，由 QQ 服务器自己去下载，不需要我们先下再传。
        """
        media = await self.api.post_group_file(
            group_openid=group_openid,
            file_type=1,  # 1=图片(png/jpg) 2=视频mp4 3=语音silk
            url=image_url,
            # False = 只上传拿 file_info，不单独发一条；要「文字+图」必须走两步
            srv_send_msg=False,
        )
        file_info = media.get("file_info") if isinstance(media, dict) else None
        if not file_info:
            raise RuntimeError(f"上传图片未拿到 file_info: {media!r}")

        await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=7,  # 7=富媒体（图文）
            content=content,
            media={"file_info": file_info},
        )
