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

**补丁三：上传图片字节（``file_data``）。**
botpy 的 ``post_group_file`` / ``post_c2c_file`` 只收 ``url``，由 **QQ 的服务器
自己去下载**。但实测：QQ 的服务器**拉不到 ``i.ytimg.com``**（``850027 富媒体
文件上传超时``，err_code 40034003），而同一时刻 ``i1.hdslb.com`` 是 ✅ ——
也就是说「能不能用 URL 形态」完全取决于 QQ 机房到那个图床的连通性，
**我们控制不了**。

而 QQ 的接口**本来就收 base64 的 ``file_data``**（实测：32771 字节的图 →
43696 字节 base64，上传成功并拿到 ``file_info``）—— 只是 botpy 的签名里没有
这个形参，``payload = locals()`` 又只打包声明过的形参，所以传不进去。

补上它之后，YouTube / X 的封面走「我们自己下载 → base64 上传」，
不再依赖 QQ 够不够得到那个域名。

三个补丁都利用同一个机制：``ConnectionState.__init__`` 用
``inspect.getmembers`` 收集所有 ``parse_*`` 方法建解析表，
所以在 Client 建立连接之前替换类方法就会被自动注册。
"""

from __future__ import annotations

import logging

from botpy.api import BotAPI
from botpy.connection import ConnectionState
from botpy.http import Route
from botpy.message import GroupMessage

_log = logging.getLogger("qq_rss_bot.compat")

# 用来判断某个 parser 是否已经被我们替换过（幂等安装）
_PATCH_FLAG = "_qq_rss_bot_patched"


class Mention:
    """保留平台给出的**全部** mention 字段。

    botpy 的 ``_User`` 只取 ``id / username / bot / avatar``，丢掉了
    **``is_you``** —— 而 ``is_you`` 是判断「这条 @ 是不是冲着机器人来的」
    最精确的依据，也是唯一能认出指令面板那种 @ 的东西：
    面板插入的是 openid 占位符 ``<@机器人openid>``，靠匹配显示名根本对不上。

    和 ``member_role`` 是同一类问题：报文里有，botpy 不要，只能自己捡回来。
    """

    __slots__ = (
        "id",
        "username",
        "bot",
        "avatar",
        "is_you",
        "member_openid",
        "member_role",
        "scope",
    )

    def __init__(self, data: dict) -> None:
        self.id = data.get("id")
        self.username = data.get("username")
        self.bot = data.get("bot")
        self.avatar = data.get("avatar")
        self.is_you = data.get("is_you")
        self.member_openid = data.get("member_openid")
        self.member_role = data.get("member_role")
        self.scope = data.get("scope")

    def __repr__(self) -> str:
        return f"Mention(username={self.username!r}, is_you={self.is_you!r})"


class GroupMessageWithRole(GroupMessage):
    """补上 botpy 丢掉的字段：``member_role``、``mentions``、卡片消息的两个键。

    实测报文里 ``author`` 带 ``member_role``（群主是 ``"owner"``），
    ``mentions[]`` 里带 ``is_you``，卡片消息（群里分享的链接）带
    ``message_type=3`` 和 ``ark_data`` —— 但 botpy 全都丢。
    ``GroupMessage`` 用了 ``__slots__``，不能事后挂新属性，
    所以用子类加槽位，并把 ``mentions`` 整个重建。
    """

    __slots__ = ("member_role", "message_type", "ark_data")

    def __init__(self, api, event_id, data) -> None:
        super().__init__(api, event_id, data)
        self.member_role = (data.get("author") or {}).get("member_role")
        # 卡片消息靠这两个认：message_type=3 是 ark，ark_data["fields"] 里才有
        # 标题和 jump_url。见 bilibili.py 里四种分享形态的说明。
        self.message_type = data.get("message_type")
        self.ark_data = data.get("ark_data")
        # 重建 mentions，把 is_you / member_openid 救回来
        raw = data.get("mentions") or []
        self.mentions = [Mention(m) for m in raw if isinstance(m, dict)]


def _make_parser(event: str):
    """生成一个把事件路由到 ``event`` 的解析器，构造带角色的群消息。"""

    def parse(self, payload: dict) -> None:
        # DEBUG 级转储原始报文。@ 的形态、有没有 mentions 字段这类事猜不出来 ——
        # 指令面板的 <@OPENID> 占位符就是靠这个抓到的（正则只认 \d+ 时全部漏掉）。
        _log.debug("原始报文 %s: %r", event, payload)
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


def _install_parsers() -> list[str]:
    """补丁一 / 二：新协议事件名 + 群内角色。"""
    changed: list[str] = []
    for event, method in _PARSERS:
        current = getattr(ConnectionState, method, None)
        if current is not None and getattr(current, _PATCH_FLAG, False):
            continue
        setattr(ConnectionState, method, _make_parser(event))
        changed.append(method)
    return changed


def _file_payload(
    file_type: int,
    url: str | None,
    srv_send_msg: bool,
    file_data: str | None,
) -> dict:
    """构造 ``/files`` 的请求体。

    **显式构造，不用原版那个 ``locals()``。** 原版把 ``url=None`` 也打包进去，
    而同时带 ``url: null`` 和 ``file_data`` 有被平台拒的风险 —— 二选一。
    """
    payload: dict = {"file_type": file_type, "srv_send_msg": srv_send_msg}
    if file_data is not None:
        payload["file_data"] = file_data
    else:
        payload["url"] = url
    return payload


async def _post_group_file(
    self,
    group_openid: str,
    file_type: int,
    url: str | None = None,
    srv_send_msg: bool = False,
    *,
    file_data: str | None = None,
) -> dict:
    """群聊图片上传，额外支持 base64 的 ``file_data``。见模块 docstring 补丁三。"""
    route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group_openid)
    return await self._http.request(
        route, json=_file_payload(file_type, url, srv_send_msg, file_data)
    )


async def _post_c2c_file(
    self,
    openid: str,
    file_type: int,
    url: str | None = None,
    srv_send_msg: bool = False,
    *,
    file_data: str | None = None,
) -> dict:
    """私聊图片上传，额外支持 base64 的 ``file_data``。"""
    route = Route("POST", "/v2/users/{openid}/files", openid=openid)
    return await self._http.request(
        route, json=_file_payload(file_type, url, srv_send_msg, file_data)
    )


def _install_file_upload() -> list[str]:
    """补丁三：把两个上传方法的签名换成支持 ``file_data`` 的版本。"""
    changed: list[str] = []
    for name, impl in (
        ("post_group_file", _post_group_file),
        ("post_c2c_file", _post_c2c_file),
    ):
        current = getattr(BotAPI, name, None)
        if current is None or getattr(current, _PATCH_FLAG, False):
            continue
        setattr(impl, _PATCH_FLAG, True)
        setattr(BotAPI, name, impl)
        changed.append(name)
    return changed


def install_botpy_patches() -> list[str]:
    """安装全部 botpy 补丁，返回本次实际改动的项（幂等，重复调用返回空列表）。

    必须在 ``RssBot(...)`` 构造**之前**调用：解析表是 ``ConnectionState``
    实例化时一次性建好的，事后再换就来不及了。
    """
    changed = _install_parsers() + _install_file_upload()
    if changed:
        _log.info("已替换 botpy 实现：%s", "、".join(changed))
    else:
        _log.debug("botpy 补丁已是最新，跳过")
    return changed


# 旧名保留：smoke.py 和早期文档里用的是这个。
install_new_protocol_support = install_botpy_patches
