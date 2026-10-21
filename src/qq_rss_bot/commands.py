"""QQ 群里的指令解析与执行。

指令设计原则：全部走 @机器人 + 文本，因为群聊场景没有斜杠命令注册机制。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import ai, features, idolmaster, tickets as tickets_module
from .config import Config
from .db import Database, Subscription
from .menu import (
    MAX_INLINE_UNSUB,
    build_rss_keyboard,
    build_rss_markdown,
    build_rss_text,
    build_set_keyboard,
    build_set_markdown,
    build_set_text,
    build_unsub_keyboard,
    build_unsub_markdown,
    format_subscription_list,
)
from .rss import Entry, FeedResult, fetch_feed, resolve_url
from .routes import format_hits, load_index
from .notify import Notifier, NullNotifier
from .quota import QuotaExceeded
from .scheduler import (
    IMAGE_FAILURE_KEY_PREFIX,
    format_entry,
    image_failure_message,
    prepare_digests,
    send_entry_images,
)

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
       ``<@机器人openid>`` 而不是显示名，字符串匹配对不上。
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
/rss                               按钮菜单：订阅 / 退订 / 列表 / 搜索
/sub <RSSHub路由或URL> [间隔分钟]  订阅，默认 15 分钟
/unsub <编号>                      取消订阅
/list                              查看本群订阅
/find <关键词>                     搜 RSSHub 路由，不用记路径
/new [编号|all] [条数]             读最新几条，默认每源 3 条
/check <编号>                      抓一次看源还活着没（只看，不影响推送）
/pause <编号>                      暂停某个订阅
/resume <编号>                     恢复暂停的订阅
/interval <编号> <分钟>            改抓取间隔，不用退订重订
/ai <编号> on|off                  给某个源开/关 AI 摘要（抓正文 + 翻译/精简）
/set                               本群的功能开关（按钮面板）
/set <编号> on|off                 开/关某个推送功能，例：/set 1 off
/ping                              存活检测
/help                              本说明
/抽选                              偶像大师各场 live 现在正在受付中的抽选
/当落                              待当落 + 入金中的抽选（中了不付款会作废）

订阅是按群共享的：群里任何人订阅，全群都会收到推送。
/sub /unsub /pause /resume /interval /ai /set 仅群主和管理员可用。

例：/sub /36kr/newsflashes 30
    /sub /hackernews/best 60 --ai    订阅并打开 AI 摘要

新群会被自动订上默认源（AUTO_SUBSCRIBE_FEED），不用手动配。"""

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
    "find": "find",
    "search": "find",
    "搜索": "find",
    "路由": "find",
    "check": "check",
    "new": "new",
    "最新": "new",
    "pause": "pause",
    "暂停": "pause",
    "resume": "resume",
    "恢复": "resume",
    "interval": "interval",
    "间隔": "interval",
    "ai": "ai",
    "摘要": "ai",
    "ping": "ping",
    "ticket": "ticket",
    "tickets": "ticket",
    "抽选": "ticket",
    "抽票": "ticket",
    "受付": "ticket",
    "result": "result",
    "当落": "result",
    "进度": "result",
    "入金": "result",
    "rss": "rss",
    "菜单": "rss",
    "menu": "rss",
    "set": "set",
    "开关": "set",
    "功能": "set",
}

COMMAND_TOKENS = frozenset(COMMAND_ALIASES)

# 规范名 → 处理方法名
COMMAND_METHODS = {
    "help": "_help",
    "sub": "_sub",
    "unsub": "_unsub",
    "list": "_list",
    "find": "_find",
    "check": "_check",
    "new": "_new",
    "pause": "_pause",
    "resume": "_resume",
    "interval": "_interval",
    "ai": "_ai",
    "ping": "_ping",
    "ticket": "_ticket",
    "result": "_result",
    "rss": "_rss",
    "set": "_set",
}

# 只有群主/管理员能用的指令。**从别名表派生**，加别名不会漏 ——
# 曾经是手工列 {"sub","subscribe","订阅",...}，漏一个中文别名就等于没限制。
RESTRICTED_CANONICAL = frozenset(
    # ⚠️ `set` 在里面：它改的是**全群**收不收某个推送，和 `/sub` 同一类，
    # 不是个人偏好。普通成员点按钮会被拒，拒绝日志里带角色。
    {"sub", "unsub", "pause", "resume", "interval", "ai", "set"}
)
RESTRICTED_TOKENS = frozenset(
    token
    for token, canonical in COMMAND_ALIASES.items()
    if canonical in RESTRICTED_CANONICAL
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

# /find 每次最多显示几条 —— 群消息有长度限制，而且人也不会往下翻
_FIND_LIMIT = 5

# /new 是**逐条带图**发的，一次刷太多会撞上群维度 20 qpm 的频控，
# 所以三个数都得有：默认值、单源上限、单次总量上限。
_NEW_DEFAULT = 3
_NEW_MAX_PER_SUB = 10
_NEW_MAX_TOTAL = 15
# 逐条之间的间隔，和 scheduler 的推送节奏一致（稳定到达顺序 + 避频控）
_NEW_GAP_SEC = 0.4
# /new 的每群冷却。它一次最多发 15 条，占的是**全群共享**的每日配额，
# 不限的话一个人连点几下就能把一天的配额烧掉。
_NEW_COOLDOWN_SEC = 60.0

# 实测群主是 "owner"；"admin" 是按常见约定加的，若真实值不同，
# 拒绝时会把实际角色打进日志，照着改这里即可。
ADMIN_ROLES = frozenset({"owner", "admin"})

# /rss 的子参数：`/rss unsub` 是「退订…」按钮点出来的二级卡片（手打也认）。
_RSS_UNSUB_ARGS = frozenset({"unsub", "退订", "cancel"})


class Replier(Protocol):
    async def __call__(self, text: str) -> None: ...


class CardReplier(Protocol):
    """markdown 卡片回复。

    ``keyboard`` 为 None 时**不发按钮**（bilibili 预览卡片就是这样 —— 空
    ``{"content": {"rows": []}}`` 有被平台静默丢弃的风险，干脆整个参数不传）。
    """

    async def __call__(self, markdown: str, keyboard: dict | None = None) -> None: ...


@dataclass
class CommandContext:
    group_openid: str
    reply: Replier
    # 发起人在群里的角色（owner / admin / …）。由 compat.py 的补丁补上，
    # 老协议路径或 botpy 行为变化时可能为 None —— 见 CommandHandler._is_admin。
    member_role: str | None = None
    # 带按钮的回复。**必须有默认值**：smoke.py 里十几处是位置传参
    # （CommandContext("G", reply, "owner")），加个没默认值的字段会全炸。
    # 拿不到时 /rss 退化成纯文本，不影响功能。
    reply_card: CardReplier | None = None


class CommandHandler:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        http: httpx.AsyncClient,
        pusher: Pusher | None = None,
        notifier: Notifier | None = None,
        ai_http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.http = http
        # /new 要主动发消息（带图那条路只有 Pusher 有），
        # 所以把推送出口也接进来，而不是在指令层另写一套发消息的代码。
        # 测试里传 None 也能跑，_new 会回一句「不支持」。
        self.pusher = pusher
        # 配图没发出去时私聊告警。`/new` 是**有人正盯着群**的场景，看起来不像
        # 需要私聊 —— 但配图失败在群里只表现为「少了一张」，很容易被当成
        # 「这条本来就没图」，和定时推送是同一个盲区。
        self.notifier: Notifier = notifier or NullNotifier()
        # 抓正文时直连失败才会用到的备用客户端（走代理）。
        self.ai_http = ai_http
        self._routes = None  # 路由表懒加载，进程内常驻
        self._last_new: dict[str, float] = {}  # /new 的每群冷却
        # 抽票指令的数据源。带 60 秒内存缓存，见 TicketFeed。
        self.ticket_feed = tickets_module.TicketFeed(cfg, http)
        # 封面图的宽高，进程内缓存 —— 群里连点两次不用再探一遍图。
        self._ticket_cover_sizes: dict = {}

    async def handle(self, ctx: CommandContext, raw: str) -> None:
        text = _PLACEHOLDER_RE.sub("", raw or "").strip()
        if not text:
            await ctx.reply(HELP_TEXT)
            return

        parts = text.split()
        command, args = parts[0].lstrip("/").lower(), parts[1:]

        # 用 .get + getattr(..., None) 而不是 COMMAND_METHODS[canonical]：
        # 这一行在 try **外面**，只加别名忘了往 COMMAND_METHODS 挂方法就是
        # 未捕获的 KeyError（整个事件处理器炸掉，而不是回一句「未知指令」）。
        # smoke 里另有一条断言：COMMAND_METHODS 的每个值都真的存在于 CommandHandler。
        canonical = COMMAND_ALIASES.get(command)
        method = COMMAND_METHODS.get(canonical) if canonical else None
        handler = getattr(self, method, None) if method else None

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

    async def _rss(self, ctx: CommandContext, args: list[str]) -> None:
        """按钮菜单。

        指令面板是**平的、不能嵌套**（实测，`scripts/probe_panel.py` 可复跑），
        所以面板只留入口，真正的分层靠这条消息里的按钮。

        退订按钮按本群的实际订阅**现算**，点下去插的是完整指令 `/unsub 1` ——
        省得用户回去数编号。超过 MAX_INLINE_UNSUB 个就折叠成「退订…」，
        点开去二级卡片展开（单条消息的按钮行数上限官方没写，主动压住）。
        """
        subs = self.db.list_subscriptions(ctx.group_openid)
        # /rss unsub 走二级卡片；订阅不多时它和主卡片本来就一样，直接用主卡片
        sub = args[0].lower() if args else ""
        use_second = sub in _RSS_UNSUB_ARGS and len(subs) > MAX_INLINE_UNSUB
        if use_second:
            text, kb = build_unsub_markdown(subs), build_unsub_keyboard(subs)
        else:
            text, kb = build_rss_markdown(subs), build_rss_keyboard(subs)

        if ctx.reply_card is not None:
            try:
                await ctx.reply_card(text, kb)
                return
            except Exception:  # noqa: BLE001 —— 网络层什么都可能抛
                # 卡片发不出去（比如账号没有 markdown 权限）不该让 /rss 整个失效
                _log.warning("发送 /rss 按钮卡片失败，退回纯文本", exc_info=True)
        # 拿不到卡片通道（老协议路径 / 测试里的假实现）也走这里
        await ctx.reply(build_rss_text(subs))

    async def _set(self, ctx: CommandContext, args: list[str]) -> None:
        """本群的功能开关。**不带参数 = 出按钮面板。**

        改的是**全群**收不收某个推送（和 `/sub` 同一类），所以是受限指令 ——
        面板上的按钮谁都能点，但真执行时还是走 `_is_admin`。

        ⚠️ 群里的按钮**只能把指令填进输入框**（`action.type=2`；`enter: true`
        自动发送仅单聊可用），所以每个按钮插的都是**完整可发**的
        `/set <编号> on|off`，正文里也明写了要按发送。
        """
        states = features.states(self.db, ctx.group_openid)

        if not args:
            await self._send_set_panel(ctx, states)
            return

        feature = features.resolve_feature(args[0])
        if feature is None:
            n = len(features.FEATURES)
            await ctx.reply(
                f"认不出「{args[0]}」。用法：/set <编号 1-{n}> on|off"
                f"（也可以写英文名，如 /set morning off）。\n发 /set 看面板。"
            )
            return
        if len(args) < 2:
            # 只给编号时**不猜**「是想开还是想关」—— 猜错就是一次静默的群级变更
            await ctx.reply(
                f"用法：/set {args[0]} on|off\n"
                f"（{features.NAMES[feature]} 现在"
                f"{'开着' if states[feature] else '关着'}）"
            )
            return
        want = features.parse_bool(args[1])
        if want is None:
            await ctx.reply(f"on/off 认不出「{args[1]}」。用法：/set {args[0]} on|off")
            return

        self.db.set_group_feature(ctx.group_openid, feature, want)
        states[feature] = want
        _log.info(
            "功能开关：群 %s 的 %s → %s（角色=%r）",
            ctx.group_openid,
            feature,
            "开" if want else "关",
            ctx.member_role,
        )
        await self._send_set_panel(
            ctx,
            states,
            notice=f"✅ {features.NAMES[feature]}：本群已{'打开' if want else '关闭'}",
        )

    def _set_warnings(self, states: dict[str, bool]) -> list[str]:
        """「开关开了、但功能还是不会响」那几种情况，**必须写在面板上**。

        这是这个项目被咬过好几次的形态：用户按面板打开了，群里没反应，
        而日志上一切正常 —— 因为卡在**另一个**开关上。
        """
        out: list[str] = []
        if states.get("repeat") and not self.cfg.repeat_enabled:
            out.append(
                "复读的总开关 REPEAT_ENABLED=false（在 .env 里），这里开着也不会跟。"
                "改 .env 要 `docker compose up -d` 重建容器，`restart` 不重读。"
            )
        return out

    async def _send_set_panel(
        self,
        ctx: CommandContext,
        states: dict[str, bool],
        *,
        notice: str = "",
    ) -> None:
        """面板 + 按钮。卡片发不出去就退回纯文本（和 /rss 同一条路）。"""
        warnings = self._set_warnings(states)
        if ctx.reply_card is not None:
            try:
                await ctx.reply_card(
                    build_set_markdown(states, notice=notice, warnings=warnings),
                    build_set_keyboard(states),
                )
                return
            except Exception:  # noqa: BLE001 —— 网络层什么都可能抛
                _log.warning("发送 /set 按钮卡片失败，退回纯文本", exc_info=True)
        await ctx.reply(
            build_set_text(states, notice=notice, warnings=warnings)
        )

    async def _sub(self, ctx: CommandContext, args: list[str]) -> None:
        # `--ai` 可以在任意位置，先摘出去 —— 剩下的才是位置参数
        want_ai = any(a.lower() == "--ai" for a in args)
        args = [a for a in args if a.lower() != "--ai"]
        if not args:
            await ctx.reply("用法：/sub <RSSHub路由或URL> [间隔分钟] [--ai]")
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

        # 已经订过就别再抓一遍了 —— 顺便告诉用户怎么改间隔（以前只会回一句
        # 「已经订阅过了」，用户想改间隔只能退订重订，而重订会重建 baseline 丢增量）
        existing = self._find_sub(ctx.group_openid, url)
        if existing is not None:
            pos, sub = existing
            await ctx.reply(
                f"本群已经订阅过了（编号 {pos}）：{sub.title or url}\n"
                f"当前每 {sub.interval_sec // 60} 分钟检查一次，"
                f"想改间隔用 /interval {pos} <分钟>。"
            )
            return

        # 上限要在**抓取之前**判：不然白抓一次才说不让订
        cap = self.cfg.max_subs_per_group
        if cap and self.db.count_subscriptions(ctx.group_openid) >= cap:
            await ctx.reply(
                f"本群订阅数已达上限 {cap} 个。\n"
                f"用 /list 看看现有的、/unsub 退掉不用的再订阅。\n"
                f"（设上限是因为推送要占主动消息配额，源太多会互相挤。）"
            )
            return

        # 先抓一次：既能验证路由，又能建立 baseline，避免订阅瞬间倒灌旧文章
        result = await fetch_feed(self.http, url, self.cfg.fetch_timeout_sec)
        if not result.ok:
            await ctx.reply(f"订阅失败：{result.error}\n{url}")
            return

        sub_id, created = self.db.add_subscription(
            ctx.group_openid, url, result.title, interval
        )
        if not created:  # 竞态：上面查过之后、这里插入之前又被订了一次
            await ctx.reply(f"本群已经订阅过了：{result.title or url}")
            return

        if want_ai:
            self.db.set_subscription_ai(sub_id, True)

        # 把当前已有条目全部登记为已见，之后只推增量
        self.db.filter_new_entries(sub_id, [e.key for e in result.entries])
        self.db.mark_polled(sub_id, title=result.title, mark_initialized=True)

        # 编号是**本群内的位置**，不是数据库主键 —— 新增的排在最后，
        # 所以位置就是当前条数。直接用 sub_id 会泄漏全局自增主键：
        # 一个从没订阅过的新群，第一条会显示成「5.」。
        pos = self.db.count_subscriptions(ctx.group_openid)
        tail = "，只推新内容。"
        if want_ai:
            if self.cfg.ai_api_key:
                tail = "，只推新内容。\n已打开 AI 摘要：推送会带上译文标题和精简摘要。"
            else:
                # 开了但没配 key 是最容易困惑的状态 —— 明明打开了却什么都没变。
                tail = (
                    "，只推新内容。\n⚠️ AI 摘要已记为开启，但 **DEEPSEEK_API_KEY "
                    "没配**，实际不会生效。"
                )
        await ctx.reply(
            f"订阅成功，编号 {pos}\n"
            f"{result.title or url}\n"
            f"当前 {len(result.entries)} 条，每 {interval // 60} 分钟检查一次{tail}"
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

    def _position_of(self, group_openid: str, sub_id: int) -> int | None:
        """数据库主键 → 用户看到的**群内位置**。"""
        for pos, sub in enumerate(self.db.list_subscriptions(group_openid), 1):
            if sub.id == sub_id:
                return pos
        return None

    def _find_sub(self, group_openid: str, url: str) -> tuple[int, Subscription] | None:
        """按 URL 找本群的订阅，返回 (位置, 订阅)。"""
        for pos, sub in enumerate(self.db.list_subscriptions(group_openid), 1):
            if sub.url == url:
                return pos, sub
        return None

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
        # 编号用**本群内的位置**，不用数据库主键：主键全局自增、跨群共享，
        # 会让新群的第一条订阅显示成 "5."，删一条还留空号。
        # 格式化的那份在 menu.py —— /rss 的按钮发不出去时也用同一份兜底，
        # 两边必须长得一样。
        await ctx.reply(format_subscription_list(self.db.list_subscriptions(ctx.group_openid)))

    async def _route_index(self):
        """懒加载 RSSHub 路由表。

        4MB / 4004 条，进程内只留一份；磁盘缓存按天刷新，
        所以群里第一条 /find 之后，后面的都是纯内存检索。
        """
        if self._routes is None:
            self._routes = await load_index(
                cache_path=self.cfg.db_path.parent / "rsshub-routes.json",
                http=self.http,
                base_url=self.cfg.rsshub_base,
            )
        return self._routes

    async def _find(self, ctx: CommandContext, args: list[str]) -> None:
        """搜 RSSHub 路由。

        存在的理由：RSSHub 有 4000 多条路由，指望用户记住 `/36kr/newsflashes`
        这种路径不现实。搜出来直接给可订阅的路径，顺带标出哪些在本实例上
        跑不起来 —— 免得照着一份跑不通的路径去 /sub，再对着 503 发懵。
        """
        if not args:
            await ctx.reply(
                "用法：/find <关键词>\n"
                "按站点名、路由名或分类搜 RSSHub 路由。\n"
                "例：/find 偶像   /find github   /find 动漫"
            )
            return
        query = " ".join(args)
        try:
            index = await self._route_index()
        except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
            _log.warning("路由表取不到：%r", exc)
            await ctx.reply("路由表暂时取不到（RSSHub 可能没起来），稍后再试。")
            return
        total, hits = index.search(query, limit=_FIND_LIMIT)
        await ctx.reply(format_hits(query, total, hits, _FIND_LIMIT))

    async def _check(self, ctx: CommandContext, args: list[str]) -> None:
        """抓一次看看源还活着没。**只看，不动去重状态。**

        踩过的坑：这里原本调的是 `filter_new_entries`，它是「查的同时就写入已见」
        的版本。于是用户只是想预览一下，那几条新内容就被**静默消费**掉了，
        定时推送永远不会再发 —— 而回复里还写着「本次新 3 条」，
        读起来像是在报告状态，实际是把它吃掉了。
        推送路径本来就用只读的 `seen_keys`，这里必须一致。
        """
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
            # 手动检查也算一次失败：让它参与连续失败计数，
            # 该自动暂停的照样暂停。但**不** update last_polled_at，
            # 免得手动看一眼就把定时抓取往后推一轮。
            fails = self.db.mark_polled(sub_id, error=result.error)
            await ctx.reply(f"抓取失败（连续 {fails} 次）：{result.error}")
            return

        # 只读：不写 seen_entries
        already = self.db.seen_keys(sub_id, [e.key for e in result.entries])
        pending = len(result.entries) - len(already)
        self.db.mark_polled(sub_id, title=result.title, mark_initialized=True)
        preview = result.entries[0].title if result.entries else ""
        await ctx.reply(
            f"{result.title or sub.url}\n"
            f"共 {len(result.entries)} 条，其中 {pending} 条还没推送过\n"
            f"最新：{preview}\n"
            f"（只是看看，不影响推送状态；想现在就读用 /new {args[0]}）"
        )

    async def _pause(self, ctx: CommandContext, args: list[str]) -> None:
        """暂停订阅。表里本来就有 enabled 列，只是从前没有任何代码能改它。"""
        await self._set_enabled(ctx, args, False)

    async def _resume(self, ctx: CommandContext, args: list[str]) -> None:
        await self._set_enabled(ctx, args, True)

    async def _set_enabled(self, ctx: CommandContext, args: list[str], enabled: bool) -> None:
        word = "恢复" if enabled else "暂停"
        cmd = "resume" if enabled else "pause"
        if not args:
            await ctx.reply(f"用法：/{cmd} <编号>（编号用 /list 查）")
            return
        picked = self._pick(ctx.group_openid, args[0])
        if picked is None:
            await ctx.reply("编号不对，先用 /list 看本群订阅的编号。")
            return
        pos, sub = picked
        if not self.db.set_enabled(ctx.group_openid, sub.id, enabled):
            await ctx.reply(f"{word}失败，先用 /list 确认编号。")
            return
        name = sub.title or sub.url
        if enabled:
            await ctx.reply(f"已{word} {pos}. {name}\n下次抓取会重新对齐时间，失败计数已清零。")
        else:
            await ctx.reply(
                f"已{word} {pos}. {name}\n"
                f"暂停期间不再抓取和推送，已读过的不受影响；/resume {pos} 可以恢复。"
            )

    async def _interval(self, ctx: CommandContext, args: list[str]) -> None:
        """改抓取间隔。

        必须是独立指令：/sub 同一个 URL 只会说「已经订阅过了」，
        用户想从 15 分钟改成 60 分钟就只能退订重订 —— 而重订会**重建 baseline**，
        把两次之间的增量全部丢掉。改间隔不该付出这个代价。
        """
        if len(args) < 2:
            await ctx.reply(
                "用法：/interval <编号> <分钟>\n"
                "例：/interval 1 60    （编号用 /list 查）"
            )
            return
        picked = self._pick(ctx.group_openid, args[0])
        if picked is None:
            await ctx.reply("编号不对，先用 /list 看本群订阅的编号。")
            return
        try:
            minutes = int(args[1])
        except ValueError:
            await ctx.reply("间隔得是整数分钟，例如 /interval 1 60")
            return
        if not 1 <= minutes <= 1440:
            await ctx.reply("间隔请填 1~1440 分钟。")
            return
        pos, sub = picked
        if not self.db.set_interval(ctx.group_openid, sub.id, minutes * 60):
            await ctx.reply("修改失败，先用 /list 确认编号。")
            return
        await ctx.reply(
            f"{pos}. {sub.title or sub.url} 的间隔已改为 {minutes} 分钟，立刻生效。"
        )

    async def _ai(self, ctx: CommandContext, args: list[str]) -> None:
        """给单个源开关 AI 摘要。

        **不带参数就是列状态** —— 「哪些源开了」这个问题不该逼用户去翻 .env。
        """
        if not args:
            subs = self.db.list_subscriptions(ctx.group_openid)
            if not subs:
                await ctx.reply("本群还没有订阅，先用 /sub 订一个。")
                return
            lines = ["AI 摘要状态（用 /ai <编号> on|off 改）："]
            for i, sub in enumerate(subs, start=1):
                mark = "✅ 开" if sub.ai_enabled else "⬜ 关"
                lines.append(f"{mark}  {i}. {(sub.title or sub.url)[:30]}")
            if not self.cfg.ai_api_key:
                lines.append("\n⚠️ DEEPSEEK_API_KEY 没配，开了也不会生效。")
            lines.append("\n开了之后推送会抓正文并翻译/精简，按次计费（有日上限）。")
            await ctx.reply("\n".join(lines))
            return

        if len(args) < 2:
            await ctx.reply("用法：/ai <编号> on|off\n例：/ai 2 on   （编号用 /list 查）")
            return
        picked = self._pick(ctx.group_openid, args[0])
        if picked is None:
            await ctx.reply("编号不对，先用 /list 看本群订阅的编号。")
            return
        raw = args[1].strip().lower()
        if raw in ("on", "开", "1", "true", "yes"):
            enabled = True
        elif raw in ("off", "关", "0", "false", "no"):
            enabled = False
        else:
            await ctx.reply("第二个参数只能是 on 或 off。")
            return

        pos, sub = picked
        self.db.set_subscription_ai(sub.id, enabled)
        if not enabled:
            await ctx.reply(f"{pos}. {sub.title or sub.url} 的 AI 摘要已关闭。")
            return
        if not self.cfg.ai_api_key:
            await ctx.reply(
                f"{pos}. {sub.title or sub.url} 已记为开启，"
                f"但 **DEEPSEEK_API_KEY 没配**，实际不会生效。"
            )
            return
        await ctx.reply(
            f"{pos}. {sub.title or sub.url} 的 AI 摘要已开启。\n"
            f"下次推送会先抓正文，再交给 {self.cfg.ai_model} 翻译/精简。\n"
            f"抓不到正文（付费墙 / JS 渲染）就退回 RSS 原本的文案。"
        )

    def _parse_new_args(
        self, subs: list[Subscription], args: list[str]
    ) -> tuple[list[Subscription], int] | str:
        """解析 /new 的参数，返回 (要读的订阅, 每个源几条)，参数不对就返回一句提示。

        形态：
            /new             全部订阅，每个 3 条
            /new 2           第 2 个订阅，3 条
            /new 2 5         第 2 个订阅，5 条
            /new all 5       全部订阅，每个 5 条

        编号沿用 /list 的**本群位置**语义（和 _pick 一致），不是数据库主键。
        """
        if len(args) > 2:
            return "用法：/new [编号|all] [条数]"

        per = _NEW_DEFAULT
        if len(args) == 2:
            if not args[1].isdigit():
                return "条数得是整数，例如 /new 2 5"
            # 超出单源上限就静默截到上限：这里没必要报错，
            # 用户要的是「多给几条」，不是「必须恰好 N 条」。
            per = max(1, min(int(args[1]), _NEW_MAX_PER_SUB))

        target = (args[0] if args else "all").lower()
        if target in ("all", "全部", "所有"):
            return list(subs), per
        if not target.isdigit():
            return "用法：/new [编号|all] [条数]"
        pos = int(target)
        if pos < 1 or pos > len(subs):
            return "编号不对，先用 /list 看本群订阅的编号。"
        return [subs[pos - 1]], per

    async def _new(self, ctx: CommandContext, args: list[str]) -> None:
        """按需读取最新内容。

        和定时推送只差触发方式：推送是「有新条目就发」，/new 是「我现在就要看」。
        两者共用同一个推送出口和同一套文案，所以群里的观感是一致的。

        **读过的条目会被标记为已见**，定时推送不会再原样发一遍 ——
        否则用户主动看一眼，过几分钟机器人又把同样的东西刷一次。

        默认每源 3 条。整批一次性发完会撞频控，所以有单源和单次总量两个上限。
        """
        if self.pusher is None:
            await ctx.reply("当前通道不支持读取新闻。")
            return

        # 冷却：一次最多发 15 条，占的是**全群共享**的每日配额。
        # 不限的话一个人连点几下就能把一天的配额烧掉，别人就没得看了。
        now = time.monotonic()
        elapsed = now - self._last_new.get(ctx.group_openid, 0.0)
        if elapsed < _NEW_COOLDOWN_SEC:
            await ctx.reply(
                f"/new 每分钟只能用一次，还要等 "
                f"{int(_NEW_COOLDOWN_SEC - elapsed) + 1} 秒。"
            )
            return
        self._last_new[ctx.group_openid] = now

        subs = self.db.list_subscriptions(ctx.group_openid)
        if not subs:
            await ctx.reply("本群还没有订阅，用 /sub 添加。")
            return

        parsed = self._parse_new_args(subs, args)
        if isinstance(parsed, str):
            await ctx.reply(parsed)
            return
        targets, per = parsed

        # 并发抓，源之间互不拖累
        results = await asyncio.gather(
            *(fetch_feed(self.http, sub.url, self.cfg.fetch_timeout_sec) for sub in targets),
            return_exceptions=True,
        )

        plan: list[tuple[Subscription, FeedResult, list[Entry]]] = []
        failed = 0
        for sub, result in zip(targets, results):
            if isinstance(result, BaseException) or not result.ok:
                failed += 1
                _log.warning(
                    "读取最新失败 %s: %s",
                    sub.url,
                    result if isinstance(result, BaseException) else result.error,
                )
                continue
            plan.append((sub, result, list(result.entries[:per])))

        # 总量上限：按订阅顺序取，取满为止。被截掉的**不标记已见**，
        # 留给定时推送补发。
        budget = _NEW_MAX_TOTAL
        shown: list[tuple[Subscription, FeedResult, list[Entry]]] = []
        for sub, result, entries in plan:
            take = entries[:budget]
            if not take:
                break
            budget -= len(take)
            shown.append((sub, result, take))

        total = sum(len(entries) for _, _, entries in shown)
        if not total:
            note = f"（{failed} 个源抓取失败）" if failed else ""
            await ctx.reply(f"没取到内容。{note}")
            return

        # 回执先发：逐条发中间会静默好几秒，没有回执用户不知道点没点上。
        # 走被动回复，不占主动消息额度。
        dropped = sum(len(entries) for _, _, entries in plan) - total
        lines = [
            "最新 %d 条：%s"
            % (
                total,
                " · ".join(
                    f"{(sub.title or sub.url)[:20]} {len(entries)}" for sub, _, entries in shown
                ),
            )
        ]
        if dropped:
            lines.append(f"（单次上限 {_NEW_MAX_TOTAL} 条，省略 {dropped} 条）")
        if failed:
            lines.append(f"（{failed} 个源抓取失败）")
        await ctx.reply("\n".join(lines))

        # AI 摘要**先整批备好**再开始发 —— 逐条现算的话，第 2 条要等前一条
        # 抓完正文调完模型（3~8 秒），用户会觉得机器人卡死了。
        digests: dict[int, dict[str, ai.Digest]] = {}
        # 偶像大师的企划/分类标签，同样整批备好（一次 CMS 请求覆盖 30 篇，
        # 逐条现取就是 12 次请求）。非那个源返回 {}。
        tag_map: dict[int, dict[str, str]] = {}
        for sub, _result, entries in shown:
            if entries:
                digests[sub.id] = await prepare_digests(
                    self.db, self.cfg, self.http, self.ai_http, sub, entries
                )
                if self.cfg.imas_tags:
                    tag_map[sub.id] = await idolmaster.tags_for(
                        self.http, sub.url, entries
                    )

        seen: dict[int, list[str]] = {}
        sent = 0
        quota_hit = False
        # 整轮攒着、收尾报一次 —— 每条报一次虽然会被冷却压掉，但日志里
        # 会多出一串「没发出去」的噪音。
        img_failed: list[str] = []
        img_total = 0
        for sub, result, entries in shown:
            for entry in entries:
                if sent:
                    await asyncio.sleep(_NEW_GAP_SEC)
                sent += 1
                # `entry.images` 可能是 None（源里没图），先归一成 list 再判断
                images = list(entry.images or ())
                if not self.cfg.rss_multi_image:
                    images = images[:1]
                # 和定时推送同一套：多图合并成一条 markdown 时，链接排到图片后面
                use_md = (
                    self.cfg.multi_image_markdown
                    and self.cfg.attach_images
                    and len(images) > 1
                )
                text = format_entry(
                    result.title,
                    sub.url,
                    entry,
                    with_summary=self.cfg.push_summary,
                    summary_chars=self.cfg.summary_chars,
                    digest=digests.get(sub.id, {}).get(entry.key),
                    tags=tag_map.get(sub.id, {}).get(entry.key, ""),
                    with_link=not use_md,
                )
                ok = False
                if self.cfg.attach_images and images:
                    try:
                        failed_urls = await send_entry_images(
                            self.pusher,
                            ctx.group_openid,
                            text,
                            images,
                            http=self.http,
                            link=entry.link,
                            markdown=use_md,
                        )
                        img_total += len(images)
                        img_failed.extend(failed_urls)
                        ok = True
                    except QuotaExceeded as exc:
                        # 配额是整批级别的状态，不能当成「这张图坏了」退回纯文字 ——
                        # 那样会白试一遍，每条还多刷一行日志
                        _log.warning("群 %s 配额用尽，/new 提前结束: %s", ctx.group_openid, exc)
                        try:
                            await ctx.reply(
                                f"今日主动消息配额已用尽（{exc.used}/{exc.limit} 条），"
                                f"剩下的 {total - sent} 条不发了，明天零点恢复。"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        quota_hit = True
                        break
                    except Exception as exc:  # noqa: BLE001 —— QQ 拉不到图/图太大都可能
                        _log.warning("配图失败，退回纯文字 (%s): %s", images[0], exc)
                        # 首图就挂 → 这类图一张都发不出去，退回纯文字后
                        # 群里看起来和「这条本来没图」一模一样，必须报。
                        img_total += len(images)
                        img_failed.append(images[0])
                if quota_hit:
                    break
                if not ok:
                    try:
                        await self.pusher.send_text(ctx.group_openid, text)
                        ok = True
                    except QuotaExceeded as exc:
                        # 配额没了：剩下的发也白发，直接收尾。
                        # 已经发出去的那些照常标记已见（它们是成功的），
                        # 没发的**不标记**，留给定时推送。
                        _log.warning("群 %s 配额用尽，/new 提前结束: %s", ctx.group_openid, exc)
                        try:
                            await ctx.reply(
                                f"今日主动消息配额已用尽（{exc.used}/{exc.limit} 条），"
                                f"剩下的 {total - sent} 条不发了，明天零点恢复。"
                            )
                        except Exception:  # noqa: BLE001 —— 回执发不出去也不能影响收尾
                            pass
                        quota_hit = True
                        break
                    except Exception as exc:
                        _log.error("发送失败，留给定时推送: %s | %s", entry.title[:40], exc)

                # 只把**真发出去**的记成已见，和 scheduler 的 at-least-once 一致。
                # 配额用尽时 ok 必为 False，自然不会标记。
                if ok:
                    seen.setdefault(sub.id, []).append(entry.key)
                if quota_hit:
                    break
            if quota_hit:
                break

        if img_failed:
            await self.notifier.notify(
                ctx.group_openid,
                image_failure_message(
                    f"{len(img_failed)}/{img_total} 张没发出去（每张已重试一次）",
                    "来源：/new 手动拉取",
                    img_failed,
                ),
                key=f"{IMAGE_FAILURE_KEY_PREFIX}{ctx.group_openid}",
            )

        for sub_id, keys in seen.items():
            self.db.mark_seen(sub_id, keys)
            self.db.prune_seen(sub_id)

    # ---------- 抽票（ASOBI TICKET）----------

    async def _imas_receptions(self, ctx: CommandContext) -> list | None:
        """拉一次偶像大师的受付列表。拉不到就回一句人话并返回 None。

        `TicketFeed` 带 60 秒缓存 —— 群里有人连点这两条指令时不会把接口刷爆
        （整个响应 814 KB）。
        """
        try:
            return await self.ticket_feed.imas_receptions()
        except Exception as exc:  # noqa: BLE001 —— 拉不到不该让人看见堆栈
            _log.warning("抽票：指令拉取失败 %s: %s", type(exc).__name__, exc)
            await ctx.reply("ASOBI TICKET 接口暂时拉不到，过一会儿再试。")
            return None

    async def _reply_markdown(self, ctx: CommandContext, text: str) -> None:
        """优先用 markdown 回复（列表里带封面图，纯文本显示不了）。

        `reply_card` 拿不到、或者平台回业务错误码（比如没有 markdown 模板
        权限）时**退回纯文本**，并且把图片行去掉 —— 纯文本里
        `![图1 #208px #117px](…)` 是一坨没人看得懂的东西。
        """
        if ctx.reply_card is not None:
            try:
                await ctx.reply_card(text)
                return
            except Exception as exc:  # noqa: BLE001 —— 退纯文本，别把指令整挂
                _log.warning(
                    "抽票：markdown 回复失败，退回纯文本 %s: %s", type(exc).__name__, exc
                )
        await ctx.reply(tickets_module.strip_images(text))

    async def _digest(self, ctx: CommandContext, wanted: tuple[str, ...]) -> None:
        recs = await self._imas_receptions(ctx)
        if recs is None:
            return
        # 封面表（每场 live 的主视觉）。**拿不到不影响列表** ——
        # `TicketFeed.covers()` 自己吞异常，退到受付级封面再退纯文字。
        covers = (
            await self.ticket_feed.covers()
            if self.cfg.ticket_images
            else {}
        )
        text = await tickets_module.render_digest(
            self.http,
            recs,
            tickets_module.now(),
            wanted=wanted,
            size=self.cfg.ticket_image_size,
            images=self.cfg.ticket_images,
            cache=self._ticket_cover_sizes,
            width=self.cfg.ticket_image_w,
            booths=covers,
        )
        await self._reply_markdown(ctx, text)

    async def _ticket(self, ctx: CommandContext, args: list[str]) -> None:
        """`/抽选` —— 现在正在受付中的。按截止临近排序。"""
        await self._digest(ctx, ("open",))

    async def _result(self, ctx: CommandContext, args: list[str]) -> None:
        """`/当落` —— 待当落 + 入金中。

        入金那条比当落更该盯：中了不付款直接作废，而且没有任何二次提醒。
        """
        await self._digest(ctx, ("pending", "deposit"))
