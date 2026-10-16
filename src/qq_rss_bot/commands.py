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
from .db import Database
from .rss import fetch_feed, resolve_url

_log = logging.getLogger("qq_rss_bot.commands")

# QQ 消息里 @ 有两种形态，都得剥掉：
#   1. 老协议占位符 <@!123456>
#   2. 2026 新协议直接把显示名写进正文，如 "@猪排爱好者 /ping"
_PLACEHOLDER_RE = re.compile(r"<@!?\d+>")


def strip_mention(text: str | None, bot_name: str | None = None) -> str:
    """去掉正文里的 @ 痕迹，留下纯指令。"""
    text = _PLACEHOLDER_RE.sub("", text or "")
    if bot_name:
        text = re.sub(rf"@\s*{re.escape(bot_name)}", "", text)
    return text.strip()


def is_bot_mentioned(
    content: str | None, mentions: object, bot_name: str | None
) -> bool:
    """判断这条群消息是否 @ 了机器人。

    优先用结构化的 mentions 数组（报文里有就说明平台提供了），
    没有就退化成匹配正文里的 "@显示名" —— 实测 2026 新协议的报文里
    **不带 mentions 字段**，所以这个退化分支才是实际生效的那条。
    """
    for item in mentions or []:  # type: ignore[union-attr]
        for attr in ("member_openid", "id", "username", "name"):
            value = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
            if value and bot_name and value == bot_name:
                return True
    return bool(bot_name and f"@{bot_name}" in (content or ""))

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

# 只有群主/管理员能用的指令（按 token 判，别名也要列进来）
RESTRICTED_TOKENS = frozenset({"sub", "subscribe", "订阅", "unsub", "取消"})

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

        handler = {
            "help": self._help,
            "帮助": self._help,
            "?": self._help,
            "sub": self._sub,
            "subscribe": self._sub,
            "订阅": self._sub,
            "unsub": self._unsub,
            "取消": self._unsub,
            "list": self._list,
            "列表": self._list,
            "check": self._check,
            "ping": self._ping,
        }.get(command)

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
        if not args or not args[0].isdigit():
            await ctx.reply("用法：/unsub <编号>（编号用 /list 查）")
            return
        sub_id = int(args[0])
        if self.db.remove_subscription(ctx.group_openid, sub_id):
            await ctx.reply(f"已取消订阅 {sub_id}。")
        else:
            await ctx.reply(f"没找到本群的订阅 {sub_id}。")

    async def _list(self, ctx: CommandContext, args: list[str]) -> None:
        subs = self.db.list_subscriptions(ctx.group_openid)
        if not subs:
            await ctx.reply("本群还没有订阅，用 /sub 添加。")
            return
        lines = ["本群订阅："]
        for sub in subs:
            name = sub.title or sub.url
            if len(name) > 40:
                name = name[:39] + "…"
            flag = "" if sub.initialized else "（待首抓）"
            err = f" ⚠{sub.last_error}" if sub.last_error else ""
            lines.append(f"{sub.id}. {name} / {sub.interval_sec // 60}min{flag}{err}")
        await ctx.reply("\n".join(lines))

    async def _check(self, ctx: CommandContext, args: list[str]) -> None:
        """手动触发一次抓取，用来验证路由是否还活着。"""
        if not args or not args[0].isdigit():
            await ctx.reply("用法：/check <编号>")
            return
        sub_id = int(args[0])
        sub = next(
            (s for s in self.db.list_subscriptions(ctx.group_openid) if s.id == sub_id), None
        )
        if sub is None:
            await ctx.reply(f"没找到本群的订阅 {sub_id}。")
            return

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
