"""QQ 群里的指令解析与执行。

指令设计原则：全部走 @机器人 + 文本，因为群聊场景没有斜杠命令注册机制。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Config
from .db import Database, Subscription
from .rss import fetch_feed, resolve_url

_log = logging.getLogger("qq_rss_bot.commands")

# QQ 消息里 @ 的形态（既要能剥掉，也要能认出来）：
#   1. 显名明文     "@猪排爱好者 /ping"        —— 手工 @ 时平台展开成这样
#   2. openid 占位符 "<@C371C51BA906A22E4...>" —— **指令面板插入的是这种**
#   3. 老数字占位符  "<@!123456>"               —— 老协议
#
# 踩过的坑：正则曾经写成 <@!?\d+>，只认纯数字（老协议的 QQ 号）。
# 新协议的占位符里是 **32 位十六进制 openid**，于是面板的消息
# 一条都匹配不上 —— 表现是「面板完美显示，点了没反应」，
# 而丢在 DEBUG 级日志里，从外面完全看不出原因。
_OPENID = r"[0-9A-Za-z_-]+"
_PLACEHOLDER_RE = re.compile(rf"<@!?{_OPENID}>")
# 开头的连续占位符（可能 @ 了多个），用于判断「这条是不是冲着机器人来的」
_LEADING_MENTIONS_RE = re.compile(rf"^(?:\s*<@!?{_OPENID}>)+")


def strip_mention(text: str | None, bot_name: str | None = None) -> str:
    """去掉正文里的 @ 痕迹，留下纯指令。"""
    text = _PLACEHOLDER_RE.sub("", text or "")
    if bot_name:
        text = re.sub(rf"@\s*{re.escape(bot_name)}", "", text)
    return text.strip()


def _field(item: object, name: str):
    """从 mention 里取字段，dict 和对象两种形态都支持。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def is_bot_mentioned(
    content: str | None, mentions: object, bot_name: str | None
) -> bool:
    """判断这条群消息是否 @ 了机器人。

    按可靠性从高到低：

    1. **``is_you``** —— 平台直接标了「这个 @ 就是你」。这是唯一精确的依据，
       也是唯一能认出指令面板那种 @ 的东西：面板插入的是 openid 占位符
       ``<@C371C51B...>`` 而不是显示名，字符串匹配对不上。
       （botpy 的 ``_User`` 会丢掉 ``is_you``，由 ``compat.Mention`` 捡回来。）
    2. ``mentions`` **非空但没一个 ``is_you``** → 明确是在 @ 别人，直接否定。
       有了这条，「@队友 + 恰好一个指令名」不再误触发。
    3. 没有 ``mentions`` 字段 —— 手工 @ 的报文就是这样，平台把 @ 展开成
       显示名写进正文。退化成匹配 ``@显示名``；
       再退化成「开头是占位符 + 后面跟已知指令名」。
    """
    items = list(mentions or [])  # type: ignore[arg-type]

    for item in items:
        if _field(item, "is_you") is True:
            return True
        # 老报文可能没有 is_you，退化成比对用户名
        if bot_name:
            for attr in ("username", "name"):
                if _field(item, attr) == bot_name:
                    return True
    if items:
        return False

    text = content or ""

    # 手工 @：平台把 @ 展开成显示名写进正文
    if bot_name and f"@{bot_name}" in text:
        return True

    # 兜底：正文开头是 <@OPENID> 占位符。刻意要求后面跟的是**已知指令名**，
    # 否则「@张三 你看这个」会被误判 —— 占位符形态和 @ 人一模一样，
    # 拿不到机器人自己的 openid 就分不出来。
    stripped = _LEADING_MENTIONS_RE.sub("", text).strip()
    if stripped != text.strip():
        return looks_like_command(stripped)
    return False

HELP_TEXT = """RSS 订阅机器人
/sub <RSSHub路由或URL> [间隔分钟]  订阅，默认 15 分钟
/unsub <编号>                      取消订阅
/list                              查看本群订阅
/check <编号>                      立刻抓一次并预览
/ping                              存活检测
/help                              本说明

订阅是按群共享的：群里任何人订阅，全群都会收到推送。
/sub 与 /unsub 仅群主和管理员可用。

例：/sub /36kr/newsflashes 30"""

# 指令别名 → 规范名。键是群里实际能敲的 token（不带斜杠）。
# 这张表是「什么算指令」的唯一依据 —— 别名、权限、@ 识别都从它派生，
# 免得加个别名时忘了同步别处。
COMMAND_ALIASES = {
    "help": "help",
    "帮助": "help",
    "?": "help",
    "sub": "sub",
    "subscribe": "sub",
    "订阅": "sub",
    "unsub": "unsub",
    "取消": "unsub",
    "list": "list",
    "列表": "list",
    "check": "check",
    "ping": "ping",
}

COMMAND_TOKENS = frozenset(COMMAND_ALIASES)

# 规范名 → 处理方法名
COMMAND_METHODS = {
    "help": "_help",
    "sub": "_sub",
    "unsub": "_unsub",
    "list": "_list",
    "check": "_check",
    "ping": "_ping",
}

# 只有群主/管理员能用的指令。**从别名表派生**，加别名不会漏 ——
# 曾经是手工列 {"sub","subscribe","订阅",...}，漏一个中文别名就等于没限制。
RESTRICTED_TOKENS = frozenset(
    token for token, canonical in COMMAND_ALIASES.items() if canonical in ("sub", "unsub")
)


def looks_like_command(text: str | None) -> bool:
    """首词是不是已知指令名（带不带斜杠都认）。

    用于判断一条**没有明确 @ 显示名**的消息是不是冲着机器人来的：
    指令面板插入的 @ 是 openid 占位符，剥掉之后剩下的就是指令名。

    刻意只认**已知**指令名，不认「以 / 开头的任意词」：
    否则群里正常的斜杠用法会触发机器人刷「未知指令」。
    """
    parts = (text or "").strip().split()
    if not parts:
        return False
    return parts[0].lstrip("/").lower() in COMMAND_TOKENS

# 实测群主是 "owner"；"admin" 是按常见约定加的，若真实值不同，
# 拒绝时会把实际角色打进日志，照着改这里即可。
ADMIN_ROLES = frozenset({"owner", "admin"})


class Replier(Protocol):
    async def __call__(self, text: str) -> None: ...


@dataclass
class CommandContext:
    group_openid: str
    reply: Replier
    # 发起人在群里的角色（owner / admin / …）。由 compat.py 的补丁补上，
    # 老协议路径或 botpy 行为变化时可能为 None —— 见 CommandHandler._is_admin。
    member_role: str | None = None


class CommandHandler:
    def __init__(self, cfg: Config, db: Database, http: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http

    async def handle(self, ctx: CommandContext, raw: str) -> None:
        text = _PLACEHOLDER_RE.sub("", raw or "").strip()
        if not text:
            await ctx.reply(HELP_TEXT)
            return

        parts = text.split()
        command, args = parts[0].lstrip("/").lower(), parts[1:]

        canonical = COMMAND_ALIASES.get(command)
        handler = getattr(self, COMMAND_METHODS[canonical]) if canonical else None

        if handler is None:
            # 群里随便聊天时不要刷屏，只有看起来像指令才回
            if text.startswith("/"):
                await ctx.reply(f"未知指令：/{command}\n\n{HELP_TEXT}")
            return

        if self._restricted(command) and not self._is_admin(ctx):
            _log.info(
                "拒绝受限指令 %s：发起人角色=%r（群 %s）",
                command,
                ctx.member_role,
                ctx.group_openid,
            )
            await ctx.reply("这个指令只允许群主和管理员使用。")
            return

        try:
            await handler(ctx, args)
        except Exception:
            _log.exception("处理指令 %s 失败", command)
            await ctx.reply("执行出错，已记录日志。")

    def _restricted(self, command: str) -> bool:
        return self.cfg.restrict_admin_commands and command in RESTRICTED_TOKENS

    def _is_admin(self, ctx: CommandContext) -> bool:
        role = (ctx.member_role or "").strip().lower()
        if role in ADMIN_ROLES:
            return True
        if not role:
            # 角色拿不到时放行：宁可漏管，也不能让机器人直接不可用。
            # 真出现这行日志说明 compat.py 的解析器补丁没生效。
            _log.warning(
                "拿不到发起人角色，放行受限指令 %s（检查 compat.py 补丁）", ctx.group_openid
            )
            return True
        return False

    # ---------- 各指令 ----------

    async def _help(self, ctx: CommandContext, args: list[str]) -> None:
        await ctx.reply(HELP_TEXT)

    async def _ping(self, ctx: CommandContext, args: list[str]) -> None:
        stats = self.db.stats()
        await ctx.reply(
            f"pong\n群 {stats['groups']} / 订阅 {stats['subscriptions']} / 已记录条目 {stats['seen_entries']}"
        )

    async def _sub(self, ctx: CommandContext, args: list[str]) -> None:
        if not args:
            await ctx.reply("用法：/sub <RSSHub路由或URL> [间隔分钟]")
            return

        url = resolve_url(args[0], self.cfg.rsshub_base)
        if len(args) > 1:
            try:
                minutes = int(args[1])
            except ValueError:
                await ctx.reply("间隔得是整数分钟，例如 /sub /github/trending/daily/python 30")
                return
            if not 1 <= minutes <= 1440:
                await ctx.reply("间隔请填 1~1440 分钟。")
                return
            interval = minutes * 60
        else:
            interval = self.cfg.default_interval_sec

        # 先抓一次：既能验证路由，又能建立 baseline，避免订阅瞬间倒灌旧文章
        result = await fetch_feed(self.http, url, self.cfg.fetch_timeout_sec)
        if not result.ok:
            await ctx.reply(f"订阅失败：{result.error}\n{url}")
            return

        sub_id, created = self.db.add_subscription(
            ctx.group_openid, url, result.title, interval
        )
        if not created:
            await ctx.reply(f"本群已经订阅过了（编号 {sub_id}）：{result.title or url}")
            return

        # 把当前已有条目全部登记为已见，之后只推增量
        self.db.filter_new_entries(sub_id, [e.key for e in result.entries])
        self.db.mark_polled(sub_id, title=result.title, mark_initialized=True)

        await ctx.reply(
            f"订阅成功，编号 {sub_id}\n"
            f"{result.title or url}\n"
            f"当前 {len(result.entries)} 条，每 {interval // 60} 分钟检查一次，只推新内容。"
        )

    async def _unsub(self, ctx: CommandContext, args: list[str]) -> None:
        if not args:
            await ctx.reply("用法：/unsub <编号>（编号用 /list 查）")
            return
        picked = self._pick(ctx.group_openid, args[0])
        if picked is None:
            await ctx.reply("编号不对，先用 /list 看本群订阅的编号。")
            return
        pos, sub = picked
        self.db.remove_subscription(ctx.group_openid, sub.id)
        # 回复里带上是哪一个：编号是**位置**，列表一变含义就跟着变，
        # 只有名字能让人一眼看出删错没有。
        await ctx.reply(f"已取消订阅 {pos}. {sub.title or sub.url}")

    def _pick(self, group_openid: str, raw: str) -> tuple[int, Subscription] | None:
        """把 /list 里的编号解析成 (位置, 订阅)。

        编号是**本群内的位置**（1、2、3…），不是数据库主键。
        主键是全局自增、跨群共享的 —— 一个从没订阅过的新群，第一条订阅会
        显示成 "5."；删掉一条还会留下空号。用户看到的编号必须和 /list 的顺序
        一致，否则他会照着一个记不住的数字去猜。
        """
        if not raw.isdigit():
            return None
        subs = self.db.list_subscriptions(group_openid)
        pos = int(raw)
        if pos < 1 or pos > len(subs):
            return None
        return pos, subs[pos - 1]

    async def _list(self, ctx: CommandContext, args: list[str]) -> None:
        subs = self.db.list_subscriptions(ctx.group_openid)
        if not subs:
            await ctx.reply("本群还没有订阅，用 /sub 添加。")
            return
        lines = ["本群订阅："]
        # 编号用**本群内的位置**，不用数据库主键：主键全局自增、跨群共享，
        # 会让新群的第一条订阅显示成 "5."，删一条还留空号。
        for pos, sub in enumerate(subs, 1):
            name = sub.title or sub.url
            if len(name) > 40:
                name = name[:39] + "…"
            flag = "" if sub.initialized else "（待首抓）"
            err = f" ⚠{sub.last_error}" if sub.last_error else ""
            lines.append(f"{pos}. {name} / {sub.interval_sec // 60}min{flag}{err}")
        await ctx.reply("\n".join(lines))

    async def _check(self, ctx: CommandContext, args: list[str]) -> None:
        """手动触发一次抓取，用来验证路由是否还活着。"""
        if not args:
            await ctx.reply("用法：/check <编号>")
            return
        picked = self._pick(ctx.group_openid, args[0])
        if picked is None:
            await ctx.reply("编号不对，先用 /list 看本群订阅的编号。")
            return
        _, sub = picked
        sub_id = sub.id

        result = await fetch_feed(self.http, sub.url, self.cfg.fetch_timeout_sec)
        if not result.ok:
            self.db.mark_polled(sub_id, error=result.error)
            await ctx.reply(f"抓取失败：{result.error}")
            return

        fresh = self.db.filter_new_entries(sub_id, [e.key for e in result.entries])
        self.db.mark_polled(sub_id, title=result.title, mark_initialized=True)
        preview = result.entries[0].title if result.entries else ""
        await ctx.reply(
            f"{result.title or sub.url}\n共 {len(result.entries)} 条，本次新 {len(fresh)} 条\n最新：{preview}"
        )
