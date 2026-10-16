"""botpy 1.2.1 的两处适配。

**补丁一：新协议事件名。**
QQ 开放平台 2026 年起对群聊改用 ``GROUP_MESSAGE_CREATE`` 事件（全量群消息，
不再只推 @ 消息）。botpy 1.2.1 的 ConnectionState 里没有对应的 parse_ 方法，
网关收到该事件时只会打一行

    ERROR botpy | _parser unknown event group_message_create.

然后**把事件丢掉**，机器人表现为「连上了但永远收不到消息」。

**补丁二：群内角色。**
botpy 的 ``GroupMessage._User`` 只取了 ``member_openid``，把报文里的
``author.member_role``（owner / admin / …）丢弃了。``/sub``、``/unsub``
要做管理员限制就必须知道发起人角色，所以这里换成带角色的子类。

两个补丁都利用同一个机制：``ConnectionState.__init__`` 用
``inspect.getmembers`` 收集所有 ``parse_*`` 方法建解析表，
所以在 Client 建立连接之前替换类方法就会被自动注册。
"""

from __future__ import annotations

import logging

from botpy.connection import ConnectionState
from botpy.message import GroupMessage

_log = logging.getLogger("qq_rss_bot.compat")

# 用来判断某个 parser 是否已经被我们替换过（幂等安装）
_PATCH_FLAG = "_qq_rss_bot_patched"


class GroupMessageWithRole(GroupMessage):
    """补上 ``member_role`` 的群消息。

    实测报文里 ``author`` 带 ``member_role``（群主是 ``"owner"``），
    但 botpy 只保留了 ``member_openid``。``GroupMessage`` 用了 ``__slots__``，
    所以不能事后挂属性，只能用子类加一个槽位。
    """

    __slots__ = ("member_role",)

    def __init__(self, api, event_id, data) -> None:
        super().__init__(api, event_id, data)
        self.member_role = (data.get("author") or {}).get("member_role")


def _make_parser(event: str):
    """生成一个把事件路由到 ``event`` 的解析器，构造带角色的群消息。"""

    def parse(self, payload: dict) -> None:
        # 报文结构与 @ 消息一致（d.group_openid / d.id / d.content 都在），
        # 所以直接复用 GroupMessage，包括 message.reply() 的被动回复能力。
        message = GroupMessageWithRole(self.api, payload.get("id"), payload.get("d", {}))
        self._dispatch(event, message)

    setattr(parse, _PATCH_FLAG, True)
    return parse


# (派发事件名, ConnectionState 上的方法名)
_PARSERS = (
    ("group_message_create", "parse_group_message_create"),
    ("group_at_message_create", "parse_group_at_message_create"),
)


def install_new_protocol_support() -> list[str]:
    """安装两个补丁，返回本次实际替换了哪些解析器（幂等，重复调用返回空列表）。"""
    changed: list[str] = []
    for event, method in _PARSERS:
        current = getattr(ConnectionState, method, None)
        if current is not None and getattr(current, _PATCH_FLAG, False):
            continue
        setattr(ConnectionState, method, _make_parser(event))
        changed.append(method)

    if changed:
        _log.info("已替换 botpy 解析器：%s", "、".join(changed))
    else:
        _log.debug("botpy 解析器已是补丁版，跳过")
    return changed
