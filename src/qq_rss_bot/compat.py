"""botpy 1.2.1 的新协议适配。

背景：QQ 开放平台 2026 年起对群聊改用 ``GROUP_MESSAGE_CREATE`` 事件
（全量群消息，不再只推 @ 消息）。botpy 1.2.1 的 ConnectionState 里没有
对应的 parse_ 方法，网关收到该事件时只会打一行

    ERROR botpy | _parser unknown event group_message_create.

然后**把事件丢掉**，机器人表现为「连上了但永远收不到消息」。

ConnectionState.__init__ 用 inspect.getmembers 收集所有 parse_* 方法来建
解析表，所以在 Client 建立连接之前给类补一个方法就能被自动注册。
"""

from __future__ import annotations

import logging

from botpy.connection import ConnectionState
from botpy.message import GroupMessage

_log = logging.getLogger("qq_rss_bot.compat")


def install_new_protocol_support() -> bool:
    """补上 GROUP_MESSAGE_CREATE 解析器。返回是否真的做了改动。"""
    if hasattr(ConnectionState, "parse_group_message_create"):
        _log.info("botpy 已自带 group_message_create 支持，跳过适配")
        return False

    def parse_group_message_create(self, payload: dict) -> None:
        # 报文结构与老的 @ 消息一致（d.group_openid / d.id / d.content 都在），
        # 直接复用 GroupMessage 即可，包括 message.reply() 的被动回复能力。
        message = GroupMessage(self.api, payload.get("id"), payload.get("d", {}))
        self._dispatch("group_message_create", message)

    ConnectionState.parse_group_message_create = parse_group_message_create
    _log.info("已为 botpy 补上 group_message_create 解析器（QQ 2026 新协议）")
    return True
