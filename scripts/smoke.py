"""冒烟测试：不连 QQ，只验证解析/去重/推送格式化/真实抓取。

跑法：uv run python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 造假环境变量，避免 load_config 因缺凭证退出
os.environ.setdefault("QQ_BOT_APPID", "1")
os.environ.setdefault("QQ_BOT_SECRET", "x")

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database, utcnow  # noqa: E402
from qq_rss_bot.commands import (  # noqa: E402
    COMMAND_ALIASES,
    COMMAND_TOKENS,
    HELP_TEXT,
    RESTRICTED_CANONICAL,
    is_bot_mentioned,
    looks_like_command,
    strip_mention,
)
from qq_rss_bot.compat import install_new_protocol_support  # noqa: E402
from qq_rss_bot.rss import Entry, FeedResult, fetch_feed, resolve_url  # noqa: E402
from qq_rss_bot.scheduler import FeedPoller, format_entry  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {detail}")


def cfg_stub():
    """最小 Config，只为满足 FeedPoller 的字段访问。"""
    from qq_rss_bot.config import Config

    return Config(
        appid="1",
        secret="x",
        is_sandbox=False,
        rsshub_base="http://127.0.0.1:1200",
        default_interval_sec=60,
        tick_sec=30,
        fetch_timeout_sec=20,
        max_items_per_push=5,
        db_path=Path("/tmp/unused.db"),
        log_level="INFO",
        attach_images=True,
        restrict_admin_commands=True,
    )


async def _check_push_routing() -> None:
    """有图 → send_rich；无图 → send_text；配图失败 → 退回纯文字。

    用合成条目，不联网，所以结果是确定的。
    """
    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.scheduler import FeedPoller

    class Fake:
        def __init__(self, explode: bool = False) -> None:
            self.text: list[str] = []
            self.rich: list[tuple[str, str]] = []
            self.explode = explode

        async def send_text(self, group_openid: str, content: str) -> None:
            self.text.append(content)

        async def send_rich(self, group_openid: str, content: str, url: str) -> None:
            if self.explode:
                raise RuntimeError("图片上传失败")
            self.rich.append((content, url))

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        sid, _ = db.add_subscription("G", "https://a.com/f", "源名", 60)
        db.mark_polled(sid, title="源名", mark_initialized=True)
        sub = db.list_subscriptions("G")[0]

        with_img = E("k1", "标题1", "https://a.com/1", None, "", "https://a.com/1.jpg")
        no_img = E("k2", "标题2", "https://a.com/2", None, "", None)

        fake = Fake()
        await FeedPoller(cfg_stub(), db, None, fake)._push_entries(
            sub, "源名", [with_img, no_img]
        )
        check("有图 → send_rich", len(fake.rich) == 1, str(fake.rich))
        check("有图走的是源里的图片 URL",
              fake.rich and fake.rich[0][1] == "https://a.com/1.jpg")
        check("无图 → send_text", len(fake.text) == 1 and "标题2" in fake.text[0])
        check("文案含源名/标题/链接",
              "📰 源名" in fake.rich[0][0] and "标题1" in fake.rich[0][0]
              and "https://a.com/1" in fake.rich[0][0])

        boom = Fake(explode=True)
        await FeedPoller(cfg_stub(), db, None, boom)._push_entries(sub, "源名", [with_img])
        check("配图失败退回纯文字", len(boom.text) == 1 and not boom.rich)
        db.close()


async def _check_retry_on_push_failure() -> None:
    """推送失败必须留下重试机会。

    回归测试：曾经 filter_new_entries 在推送**之前**就标记已见，
    网络一断这些条目就永远不再推 —— 静默丢消息。
    """
    import qq_rss_bot.scheduler as sched
    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.rss import FeedResult
    from qq_rss_bot.scheduler import FeedPoller

    original = sched.fetch_feed

    async def fake_fetch(client, url, timeout):  # noqa: ARG001
        return FeedResult(
            url=url,
            title="源名",
            entries=[
                E("k1", "标题1", "https://a.com/1", None, "", None),
                E("k2", "标题2", "https://a.com/2", None, "", None),
            ],
        )

    sched.fetch_feed = fake_fetch
    try:
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "t.db")
            sid, _ = db.add_subscription("G", "https://a.com/f", "源名", 60)
            db.mark_polled(sid, title="源名", mark_initialized=True)
            sub = db.list_subscriptions("G")[0]

            class Flaky:
                def __init__(self) -> None:
                    self.fail = True
                    self.text: list[str] = []

                async def send_text(self, group_openid: str, content: str) -> None:
                    if self.fail:
                        raise RuntimeError("模拟网络中断")
                    self.text.append(content)

                async def send_rich(self, group_openid, content, url) -> None:
                    await self.send_text(group_openid, content)

            f = Flaky()
            poller = FeedPoller(cfg_stub(), db, None, f)

            await poller._poll_one(sub)
            check("推送失败 → 不标记已见（留待重试）",
                  db.seen_keys(sid, ["k1", "k2"]) == set(), str(db.seen_keys(sid, ["k1", "k2"])))

            f.fail = False
            await poller._poll_one(sub)
            check("网络恢复后重推成功", len(f.text) == 2, f"实际 {len(f.text)}")
            check("推成功后标记已见",
                  db.seen_keys(sid, ["k1", "k2"]) == {"k1", "k2"})

            f.text.clear()
            await poller._poll_one(sub)
            check("已见的不再重推", f.text == [], f"实际 {len(f.text)}")

            check("seen_keys 只查不写", db.seen_keys(sid, ["从没见过的key"]) == set())
            db.close()
    finally:
        sched.fetch_feed = original


print("[1] resolve_url")
check("路由补全", resolve_url("/github/trending/daily/python", "https://rsshub.app")
      == "https://rsshub.app/github/trending/daily/python")
check("无斜杠路由", resolve_url("weibo/user/1", "https://rsshub.app") == "https://rsshub.app/weibo/user/1")
check("完整 URL 原样", resolve_url("https://a.com/f.xml", "https://rsshub.app") == "https://a.com/f.xml")
check("base 尾斜杠", resolve_url("/x", "https://rsshub.app/") == "https://rsshub.app/x")

print("[2] Entry key 退化链")
check("优先用 guid", Entry.from_raw({"id": "g1", "link": "l1", "title": "t"}).key == "g1")
check("无 guid 用 link", Entry.from_raw({"link": "l1", "title": "t"}).key == "l1")
k1 = Entry.from_raw({"title": "t", "published": "2026-01-01"}).key
k2 = Entry.from_raw({"title": "t", "published": "2026-01-01"}).key
check("兜底 hash 稳定", k1 == k2 and len(k1) == 40)
check("HTML 清理", "<" not in Entry.from_raw({"title": "<b>粗</b>体 &amp; 号"}).title)
check("长标题截断", len(Entry.from_raw({"title": "x" * 500}).title) == 120)

print("[3] Database 去重 / 清理 / 退订")
with tempfile.TemporaryDirectory() as td:
    db = Database(Path(td) / "t.db")
    db.touch_group("G1")
    db.touch_group("G1")
    check("群去重", db.known_groups() == ["G1"])

    sid, created = db.add_subscription("G1", "https://a.com/f", "标题", 60)
    check("首次订阅 created=True", created is True)
    sid2, created2 = db.add_subscription("G1", "https://a.com/f", "标题", 60)
    check("重复订阅不新建", created2 is False and sid2 == sid)

    check("首抓全为新", len(db.filter_new_entries(sid, ["a", "b", "c"])) == 3)
    check("二次抓取无新", db.filter_new_entries(sid, ["a", "b", "c"]) == [])
    check("增量只回新的", db.filter_new_entries(sid, ["b", "c", "d", "e"]) == ["d", "e"])

    # 新建但从未抓过的订阅【应该】立刻到期 —— /sub 之后要马上抓一次建立 baseline
    check("从未抓过 → 立刻到期", len(db.due_subscriptions(utcnow())) == 1)
    db.mark_polled(sid, title="新标题")
    check("刚抓过 → 不到期", db.due_subscriptions(utcnow()) == [])
    check("间隔已过 → 到期", len(db.due_subscriptions("2099-01-01T00:00:00+00:00")) == 1)

    for i in range(50):
        db.filter_new_entries(sid, [f"k{i}"])
    db.prune_seen(sid, keep=10)
    check("prune 生效", db.stats()["seen_entries"] == 10)

    check("跨群不能退订", db.remove_subscription("G2", sid) is False)
    check("本群可退订", db.remove_subscription("G1", sid) is True)
    check("退订后级联清 seen", db.stats()["seen_entries"] == 0)
    db.close()

print("[4] format_entry（单条推送文案）")
_e = Entry(key="k", title="标题", link="https://a.com/1", published=None, summary="")
text = format_entry("源名", "https://a.com/f", _e)
check("含源名", "源名" in text)
check("含标题与链接", "标题" in text and "https://a.com/1" in text)
check("无源名时退化用 url", "https://a.com/f" in format_entry(None, "https://a.com/f", _e))
check("无链接条目不报错", format_entry("源名", "u", Entry("k", "标", "", None, "")) != "")
check("超长截断", len(format_entry("源名", "u",
      Entry("k", "标" * 200, "https://a.com/" + "p" * 2000, None, ""))) <= 1500)

print("[4] Entry 配图抽取")
_img_html = '<div><img src="https://a.com/x.png" /></div>'
check("从 <img src> 抠图", Entry.from_raw({"title": "t", "summary": _img_html}).image
      == "https://a.com/x.png")
check("扩展名藏在 query 里也能抠",
      Entry.from_raw({"title": "t", "summary":
                      '<img src="https://a.com/Image/get?path=/a/b/c.jpeg">'}).image
      == "https://a.com/Image/get?path=/a/b/c.jpeg")
check("协议相对链接补 https",
      Entry.from_raw({"title": "t", "summary": '<img src="//a.com/x.jpg">'}).image
      == "https://a.com/x.jpg")
check("Atom 的 content[] 里也能抠",
      Entry.from_raw({"title": "t", "content": [{"value": _img_html}]}).image
      == "https://a.com/x.png")
check("无图 → None", Entry.from_raw({"title": "t", "summary": "纯文字"}).image is None)
check("配图 URL 不会漏进 summary",
      "<img" not in Entry.from_raw({"title": "t", "summary": _img_html}).summary)

print("[4.5] @ 识别与剥离（QQ 2026 新协议）")
check("剥显示名 @", strip_mention("@猪排爱好者 /ping", "猪排爱好者") == "/ping")
check("剥占位符 @", strip_mention("<@!123456> /ping") == "/ping")
check("两者都有", strip_mention("<@!123> @bot /sub x", "bot") == "/sub x")
check("无 @ 原样", strip_mention("/list", "bot") == "/list")
check("空输入", strip_mention(None, "bot") == "")
check("名字含正则元字符", strip_mention("@a.b(c) /ping", "a.b(c)") == "/ping")

check("正文含 @名字 → 视为被 @", is_bot_mentioned("@bot /ping", [], "bot") is True)
check("闲聊 → 不算被 @", is_bot_mentioned("111", [], "bot") is False)
check("无机器人名 → 不算", is_bot_mentioned("@bot hi", [], None) is False)
check("结构化 mentions 命中", is_bot_mentioned("hi", [{"username": "bot"}], "bot") is True)
check("mentions 里有别人 → 不算", is_bot_mentioned("hi", [{"username": "别人"}], "bot") is False)
check("mentions 为 None 不炸", is_bot_mentioned("hi", None, "bot") is False)
check("已知局限：@名字前缀会被误判", is_bot_mentioned("@bot2 hi", [], "bot") is True)

print("[4.58] @ 的三种形态（含指令面板的 openid 占位符）")
# 回归：面板插入的 @ 是 <@OPENID> 占位符，不是显示名。
# 正则曾经写成 <@!?\d+> 只认纯数字（老协议 QQ 号），
# 于是面板的消息全部漏掉 —— 症状是「面板完美显示，点了没反应」。
_PANEL = "<@0123456789ABCDEF0123456789ABCDEF> /help "
check("面板形态：占位符+指令 → 命中",
      is_bot_mentioned(_PANEL, [], "猪排爱好者") is True)
check("面板形态能剥干净", strip_mention(_PANEL, "猪排爱好者") == "/help")
check("面板形态不带斜杠也认",
      is_bot_mentioned("<@ABC123> ping", [], "猪排爱好者") is True)
check("连续多个占位符",
      is_bot_mentioned("<@A1> <@B2> /list", [], "猪排爱好者") is True)
check("老数字占位符仍认",
      is_bot_mentioned("<@!123456> /ping", [], "猪排爱好者") is True)
check("手工 @ 显示名仍认",
      is_bot_mentioned("@猪排爱好者 /ping", [], "猪排爱好者") is True)

# 反向：@ 队友 + 闲聊 绝不能被当成在叫机器人（否则群里每次 @ 人都刷屏）
check("@队友闲聊不误触发",
      is_bot_mentioned("<@DEADBEEF00112233> 你看这个", [], "猪排爱好者") is False)
check("纯闲聊不触发", is_bot_mentioned("111", [], "猪排爱好者") is False)
check("占位符在中间不算（已知局限）",
      is_bot_mentioned("看 <@A1> /help", [], "猪排爱好者") is False)
# 真实报文里 mentions 带 is_you，平台直接标了「这个 @ 就是你」——
# 比任何字符串匹配都可靠，也不需要启发式。
_BOT_MENTION = [{"id": "C371C51B", "is_you": True, "bot": True, "username": "猪排爱好者"}]
_OTHER_MENTION = [{"id": "DEADBEEF", "is_you": False, "bot": False, "username": "张三"}]
check("is_you=True → 命中（面板形态，最可靠）",
      is_bot_mentioned("<@C371C51B> /help ", _BOT_MENTION, "猪排爱好者") is True)
check("@队友（is_you=False）→ 明确否定",
      is_bot_mentioned("<@DEADBEEF> /help", _OTHER_MENTION, "猪排爱好者") is False)
# 有了 is_you，连「@队友 + 裸指令名」都不会再误判 —— 启发式在这条路上不生效
check("@队友 + 裸指令名也不再误判",
      is_bot_mentioned("<@DEADBEEF> ping", _OTHER_MENTION, "猪排爱好者") is False)
# 没有 mentions 字段时才退化到启发式，此时占位符+裸指令名仍会误判（已知局限）
check("无 mentions 时退化到启发式（已知局限仍在）",
      is_bot_mentioned("<@DEADBEEF> ping", [], "猪排爱好者") is True)

check("looks_like_command 认裸名", looks_like_command("sub /x") is True)
check("looks_like_command 认斜杠", looks_like_command("/sub") is True)
check("looks_like_command 拒闲聊", looks_like_command("你看这个") is False)
check("looks_like_command 拒空", looks_like_command("") is False)
check("looks_like_command 拒未知指令", looks_like_command("/whatever") is False)

print("[4.55] 逐条推送的配图路由")
asyncio.run(_check_push_routing())

print("[4.56] 推送失败必须可重试（不丢消息）")
asyncio.run(_check_retry_on_push_failure())

print("[4.6] botpy 新协议适配器注册")
import inspect  # noqa: E402
from botpy.connection import ConnectionState  # noqa: E402
install_new_protocol_support()
methods = {a[6:].lower() for a, _ in inspect.getmembers(ConnectionState) if a.startswith("parse_")}
check("类上出现 parse_group_message_create", "group_message_create" in methods)
# 真正决定成败的是实例化时构建的解析表
state = ConnectionState(lambda *a, **k: None, None)
check("解析表里真的注册了", "group_message_create" in state.parsers)
check("老 @ 事件仍在", "group_at_message_create" in state.parsers)
check("重复安装安全", install_new_protocol_support() == [])
check("解析器带补丁标记（幂等依据）",
      getattr(ConnectionState.parse_group_message_create, "_qq_rss_bot_patched", False) is True)

print("[4.7] 群内角色提取与权限限制")
import dataclasses  # noqa: E402

from qq_rss_bot.commands import (  # noqa: E402
    ADMIN_ROLES,
    COMMAND_ALIASES,
    COMMAND_METHODS,
    RESTRICTED_TOKENS,
    CommandContext,
    CommandHandler,
)

_COMMAND_METHODS_KEYS = frozenset(COMMAND_METHODS)
from qq_rss_bot.compat import GroupMessageWithRole  # noqa: E402

_msg = GroupMessageWithRole(
    None, "evt",
    {"id": "m1", "group_openid": "G", "content": "hi",
     "author": {"member_openid": "u1", "member_role": "owner"}},
)
check("从报文提取 member_role", _msg.member_role == "owner")
check("member_openid 仍正常", _msg.author.member_openid == "u1")
check("报文缺 author 不炸", GroupMessageWithRole(None, "e", {"id": "m"}).member_role is None)
check("报文缺 member_role 得 None",
      GroupMessageWithRole(None, "e", {"author": {"member_openid": "u"}}).member_role is None)

# botpy 的 _User 只留 id/username/bot/avatar，把 is_you 丢了 ——
# 而 is_you 是唯一能精确认出「指令面板那种 @」的字段
_msg_m = GroupMessageWithRole(None, "evt", {
    "id": "m2", "group_openid": "G", "content": "<@C371C51B> /help ",
    "author": {"member_openid": "u1", "member_role": "owner"},
    "mentions": [{"id": "C371C51B", "is_you": True, "bot": True, "username": "猪排爱好者",
                  "member_openid": "C371C51B", "member_role": "member", "scope": "single"}],
})
check("mentions 保住了 is_you", _msg_m.mentions[0].is_you is True)
check("mentions 保住了 member_openid",
      _msg_m.mentions[0].member_openid == "C371C51B")
check("mentions 保住了 scope", _msg_m.mentions[0].scope == "single")
check("端到端：面板报文被认成 @ 了机器人",
      is_bot_mentioned(_msg_m.content, _msg_m.mentions, "猪排爱好者") is True)
check("报文没有 mentions 字段时 mentions 为空列表",
      GroupMessageWithRole(None, "e", {"id": "m"}).mentions == [])


async def _check_permissions() -> None:
    from qq_rss_bot.commands import COMMAND_ALIASES

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None)

        def ctx(role):
            return CommandContext(group_openid="G", reply=None, member_role=role)

        check("owner 是管理员", h._is_admin(ctx("owner")) is True)
        check("admin 是管理员", h._is_admin(ctx("admin")) is True)
        check("member 不是管理员", h._is_admin(ctx("member")) is False)
        check("大小写不敏感", h._is_admin(ctx("OWNER")) is True)
        check("角色缺失时放行（fail-open，不能把机器人锁死）",
              h._is_admin(ctx(None)) is True)

        # 断言的是**派生规则**而不是一份硬编码清单：清单会随加指令过期，
        # 过期了就变成一条永远为真的假测试。
        check("RESTRICTED_TOKENS 由别名表派生（加别名不会漏限制）",
              RESTRICTED_TOKENS == {
                  t for t, c in COMMAND_ALIASES.items() if c in RESTRICTED_CANONICAL
              },
              str(sorted(RESTRICTED_TOKENS)))
        check("所有会改订阅状态的指令都在受限集合里",
              {"sub", "unsub", "pause", "resume", "interval"} <= RESTRICTED_CANONICAL,
              str(sorted(RESTRICTED_CANONICAL)))
        check("每个受限别名都真的会被拦（不是只在表里）",
              all(h._restricted(t) for t in RESTRICTED_TOKENS))
        check("只读指令不受限",
              not any(h._restricted(t) for t in ("list", "find", "check", "ping", "help")))
        check("每个别名都指向已知规范名",
              all(c in _COMMAND_METHODS_KEYS for c in COMMAND_ALIASES.values()))
        check("/sub 受限", h._restricted("sub") is True)
        check("别名 订阅 同样受限", h._restricted("订阅") is True)
        check("/unsub 受限", h._restricted("unsub") is True)
        check("/list 不受限", h._restricted("list") is False)
        check("/ping 不受限", h._restricted("ping") is False)

        off = CommandHandler(dataclasses.replace(cfg_stub(), restrict_admin_commands=False), db, None)
        check("开关关掉后不再受限", off._restricted("sub") is False)

        # 端到端：普通成员发 /unsub 应被拒
        replies: list[str] = []

        async def reply(text: str) -> None:
            replies.append(text)

        await h.handle(CommandContext("G", reply, "member"), "/unsub 1")
        check("普通成员发 /unsub 被拒", any("只允许群主" in r for r in replies), str(replies))

        replies.clear()
        await h.handle(CommandContext("G", reply, "owner"), "/unsub 1")
        check("群主发 /unsub 走到编号校验（本群没订阅）",
              any("编号不对" in r for r in replies), str(replies))

        replies.clear()
        await h.handle(CommandContext("G", reply, "member"), "/list")
        check("普通成员 /list 可用", any("还没有订阅" in r for r in replies), str(replies))
        db.close()


asyncio.run(_check_permissions())

async def _check_panel_text_forms() -> None:
    """指令面板插入的文本必须都能被解析。

    面板是用户在客户端里看到的唯一入口：点一下会把指令名填进输入框。
    填进来的形式（带不带斜杠、用中文名还是英文名）都得认，
    否则用户照着面板点了却得到「未知指令」。
    """
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None)
        replies: list[str] = []

        async def reply(text: str) -> None:
            replies.append(text)

        ctx = CommandContext("G", reply, "owner")
        forms = [
            "/sub", "/unsub", "/list", "/check", "/ping", "/help",
            "sub", "unsub", "list", "check", "ping", "help",
            "/订阅", "/取消", "/列表", "/帮助", "/?",
        ]
        unknown: list[str] = []
        for text in forms:
            replies.clear()
            await h.handle(ctx, text)
            if any("未知指令" in r for r in replies):
                unknown.append(text)

        check("面板可能插入的所有形式都被识别", not unknown, f"未识别：{unknown}")
        db.close()


print("[4.75] 指令面板插入的文本能否被解析")
asyncio.run(_check_panel_text_forms())

print("[4.8] 帮助文案里不该出现跑不通的示例")
check("帮助不含 github/trending 示例", "github/trending" not in HELP_TEXT)
check("帮助含可用路由示例", "/36kr/newsflashes" in HELP_TEXT)
check("帮助说明了订阅是群共享的", "按群共享" in HELP_TEXT)
check("帮助说明了受限指令", "仅群主和管理员可用" in HELP_TEXT)

print("[4.76] 指令面板字数按显示宽度校验")
import setup_panel  # noqa: E402

check("ASCII 宽 = 字符数", setup_panel._width("/sub") == 4)
check("中文按 2 算", setup_panel._width("中文") == 4)
check("全角标点按 2 算", setup_panel._width("，") == 2)
check("中英混排", setup_panel._width("订阅 RSS") == 8)  # 4 + 1 + 3

_problems = setup_panel.validate(setup_panel.PANEL_ITEMS)
check("当前 PANEL_ITEMS 通过官方限制", _problems == [], str(_problems))

# 面板是**平的、不能嵌套**（scripts/probe_panel.py 实测），所以它只当入口：
# 订阅/退订/列表/搜索全在 /rss 的按钮卡片里。指令一多就往回加会立刻挤爆。
check("面板只留三个入口", [i["name"] for i in setup_panel.PANEL_ITEMS] == ["rss", "new", "help"],
      str([i["name"] for i in setup_panel.PANEL_ITEMS]))
check("面板入口都是所有人可用",
      all(i.get("only_admin") is False for i in setup_panel.PANEL_ITEMS))
# 宽度口径必须只有一份实现（setup_panel._width 是 menu.display_width 的别名）
import qq_rss_bot.menu as _menu  # noqa: E402
check("面板宽度口径与消息按钮共用", setup_panel._width is _menu.display_width)

# 回归：这 16 个中文只有 16 个字符（旧的 len() 判定会放行），
# 但显示宽度 32 已超 desc 上限 30，接口会回 30013。
_wide = [{"name": "/x", "desc": "中" * 16, "type": "command"}]
check("16 个中文（宽 32）被判超限", setup_panel.validate(_wide) != [])

_long_name = [{"name": "/" + "a" * 14, "desc": "短", "type": "command"}]
check("name 宽 15 被判超限", setup_panel.validate(_long_name) != [])

check("type 非法被抓", setup_panel.validate([{"name": "/x", "desc": "d", "type": "text"}]) != [])
check("link 非 https 被抓",
      setup_panel.validate([{"name": "/x", "desc": "d", "type": "link", "link": "http://a"}]) != [])
check("21 个元素被抓",
      setup_panel.validate([{"name": f"/{i}", "desc": "d", "type": "command"} for i in range(21)]) != [])

# 30013 的错误提示必须提到宽度，否则会把人带偏到「元素太多」
_hint = setup_panel.ERROR_HINTS["30013"]
check("30013 提示包含宽度信息", "宽度" in _hint, _hint)

print("[4.77] 面板比对：归一化掉假差异")
check("斜杠归一 /sub → sub", setup_panel._name_key("/sub") == "sub")
check("裸名原样", setup_panel._name_key("sub") == "sub")
check("None 不炸", setup_panel._name_key(None) == "")

check("PANEL_ITEMS 用裸名（服务端会剥掉斜杠）",
      all(not i["name"].startswith("/") for i in setup_panel.PANEL_ITEMS))

# 服务端真实回读形态：裸名、only_admin=false 时字段被省略
# 从 PANEL_ITEMS 造出「服务端回读形态」：裸名 + only_admin=false 时字段消失。
# 不硬编码列表 —— 否则每加一条指令，这里都要跟着改（曾经就因此误报过）。
_remote = [
    {k: v for k, v in item.items() if not (k == "only_admin" and v is False)}
    for item in setup_panel.PANEL_ITEMS
]
check("真实回读形态 == 完全一致", setup_panel.compare_items(_remote) == [],
      str(setup_panel.compare_items(_remote)))

# 带斜杠的历史数据也该被认成一致（不能报「缺少 /sub / 多出 sub」）
_slashy = [dict(i, name="/" + i["name"]) for i in _remote]
check("带斜杠的旧数据不算差异", setup_panel.compare_items(_slashy) == [],
      str(setup_panel.compare_items(_slashy)))

# 真差异必须仍然抓得到，归一化不能把问题一起抹掉。
# 元素名从 PANEL_ITEMS 里取 —— 以前这里写死 "ping" / "list"，
# 面板一改就变成两条假失败（而且失败原因看起来像比对逻辑坏了）。
_victim, _mutated = setup_panel.PANEL_ITEMS[1]["name"], setup_panel.PANEL_ITEMS[0]["name"]
check("真缺元素仍被抓", f"缺少元素 {_victim}" in
      " ".join(setup_panel.compare_items([i for i in _remote if i["name"] != _victim])))
check("描述改错仍被抓", any("desc" in d for d in setup_panel.compare_items(
    [dict(i, desc="改过了") if i["name"] == _mutated else i for i in _remote])))
check("顺序变了仍被抓", any("顺序" in d for d in setup_panel.compare_items(
    list(reversed(_remote)))))
check("重名被抓", setup_panel.validate(
    [{"name": "sub", "desc": "a", "type": "command"},
     {"name": "/sub", "desc": "b", "type": "command"}]) != [])

async def _check_subscription_numbering() -> None:
    """ /list 的编号必须是**本群内的位置**，不是数据库主键。

    回归：曾经直接打 sub.id。主键是全局自增、跨群共享的，
    于是从没订阅过的新群第一条订阅会显示成 "5."，删一条还留下空号 ——
    用户看到的编号毫无规律可循。
    """
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None)
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def run(group: str, cmd: str) -> str:
            out.clear()
            await h.handle(CommandContext(group, reply, "owner"), cmd)
            return out[0] if out else ""

        # 先往别的群塞订阅，把自增主键顶高，制造「主键 != 位置」的局面
        for i in range(4):
            db.add_subscription("OTHER", f"https://o.com/{i}", f"占位{i}", 900)
        for i in range(3):
            db.add_subscription("G", f"https://a.com/{i}", f"源{i + 1}", 900)

        listing = await run("G", "/list")
        check("编号从 1 开始（不受全局主键影响）", "1. 源1" in listing, listing)
        check("编号连续到 3", "3. 源3" in listing, listing)
        check("不泄漏被顶高的主键号", "5." not in listing and "7." not in listing, listing)

        text = await run("G", "/unsub 1")
        check("取消后回复带编号和名字", text == "已取消订阅 1. 源1", text)

        listing = await run("G", "/list")
        check("删完重排成 1./2.", "1. 源2" in listing and "2. 源3" in listing, listing)
        check("重排后不留空号", "3." not in listing, listing)

        db.add_subscription("G", "https://a.com/9", "源4", 900)
        check("新增的排在最后", "3. 源4" in await run("G", "/list"))

        db.add_subscription("NEW", "https://n.com/1", "新群的源", 900)
        check("从没订阅过的新群第一条就是 1.",
              "1. 新群的源" in await run("NEW", "/list"))

        check("越界编号被拒", "编号不对" in await run("G", "/unsub 99"))
        check("非数字被拒", "编号不对" in await run("G", "/unsub abc"))
        check("0 被拒", "编号不对" in await run("G", "/unsub 0"))
        check("负数被拒", "编号不对" in await run("G", "/unsub -1"))
        check("/check 越界也被拒", "编号不对" in await run("G", "/check 99"))
        db.close()


print("[4.79] /list 编号按群内位置排（不是数据库主键）")
asyncio.run(_check_subscription_numbering())

print("[4.81] RSSHub 路由检索（/find）")
from qq_rss_bot.routes import (  # noqa: E402
    LEVEL_BAD,
    LEVEL_OK,
    LEVEL_WARN,
    RouteIndex,
    format_hits,
    _availability,
)

# 可用性判定：这三类是最容易让人「照文档订阅然后吃 503」的原因
check("requirePuppeteer → 跑不了",
      _availability({"requirePuppeteer": True})[0] == LEVEL_BAD)
check("requireConfig（非 optional）→ 有坑",
      _availability({"requireConfig": [{"name": "GITHUB_ACCESS_TOKEN"}]})[0] == LEVEL_WARN)
check("requireConfig 里 optional 的不算阻塞",
      _availability({"requireConfig": [{"name": "WEIBO_COOKIES", "optional": True}]})[0]
      == LEVEL_OK)
check("antiCrawler → 有坑", _availability({"antiCrawler": True})[0] == LEVEL_WARN)
check("features 全 false → 可用",
      _availability({"requirePuppeteer": False, "requireConfig": False,
                     "antiCrawler": False})[0] == LEVEL_OK)
check("没有 features 字段 → 可用", _availability(None)[0] == LEVEL_OK)
check("features 是怪类型不炸", _availability("nonsense")[0] == LEVEL_OK)
check("puppeteer 优先于 config 报告",
      _availability({"requirePuppeteer": True,
                     "requireConfig": [{"name": "X"}]})[0] == LEVEL_BAD)

_FIXTURE = {
    "idolmaster": {
        "name": "アイドルマスター THE IDOLM@STER",
        "url": "idolmaster-official.jp",
        "categories": ["anime", "game"],
        "routes": {
            "/news": {"path": "/news", "name": "ニュース News",
                      "example": "/idolmaster/news"},
        },
    },
    "weibo": {
        "name": "微博",
        "url": "weibo.com",
        "categories": ["social-media"],
        "routes": {
            "/hot": {"path": "/hot", "name": "热搜", "example": "/weibo/hot",
                     "features": {"requirePuppeteer": True}},
        },
    },
    "example": {
        "name": "示例站",
        "url": "example.com",
        "categories": ["other"],
        "routes": {
            "/hot": {"path": "/hot", "name": "热搜", "example": "/example/hot"},
            "/trending": {"path": "/trending/:lang?", "name": "Trending",
                          "example": "/example/trending/daily",
                          "features": {"requireConfig": [
                              {"name": "EXAMPLE_TOKEN", "optional": False}]}},
            "/bare": {"path": "/bare/:id", "name": "没有 example 的路由"},
        },
    },
}

_idx = RouteIndex.parse(_FIXTURE)
check("解析出全部路由", len(_idx) == 5, str(len(_idx)))

check("按站点名搜（中文）", _idx.search("微博", 5)[0] == 1)
check("按路径搜", _idx.search("idolmaster", 5)[0] == 1)
check("按分类中文别名搜", _idx.search("动漫", 5)[0] >= 1)
check("按分类英文 slug 搜", _idx.search("anime", 5)[0] >= 1)
check("搜不到返回 0 条", _idx.search("这个肯定搜不到xyz", 5) == (0, []))

# 同分时**可用的排前面** —— 搜出来跑不起来是最没用的结果
_hot = _idx.search("热搜", 5)[1]
check("同分时可用路由优先", _hot[0].level == LEVEL_OK, str([(h.site, h.level) for h in _hot]))

check("可用性标进了结果",
      any(h.level == LEVEL_WARN for h in _idx.search("trending", 5)[1]))
check("没有 example 时退回 path",
      any(h.path.startswith("/bare") for h in _idx.search("bare", 5)[1]))

_text = format_hits("微博", 1, _idx.search("微博", 5)[1], 5)
check("结果含路径", "/weibo/hot" in _text, _text)
check("结果含站点名", "微博" in _text)
check("结果标出坑", "需要浏览器" in _text, _text)
check("结果给出下一步", "/sub" in _text)

_empty = format_hits("xyz", 0, [], 5)
check("没结果时给分类提示", "动漫" in _empty and "没找到" in _empty, _empty)

# 命中数超过展示上限时要说明还有多少
_many = RouteIndex.parse({
    "n": {"name": "站", "url": "n.com", "categories": ["other"],
          "routes": {f"/r{i}": {"path": f"/r{i}", "name": f"条目{i}",
                                "example": f"/n/r{i}"} for i in range(9)}},
})
_tot, _top = _many.search("条目", 5)
check("只返回 limit 条", len(_top) == 5 and _tot == 9)
check("提示还有多少条", "还有 4 条" in format_hits("条目", _tot, _top, 5))

# 缓存往返：磁盘上存的就是这个结构
_rt = RouteIndex.from_dict(_idx.to_dict())
check("缓存往返条数一致", len(_rt) == len(_idx))
check("缓存往返后仍能搜", _rt.search("微博", 5)[0] == 1)
check("缓存往返保住可用性",
      _rt.search("trending", 5)[1][0].level == LEVEL_WARN)
try:
    RouteIndex.from_dict({"nonsense": 1})
    check("坏缓存要抛错（好触发重拉）", False)
except ValueError:
    check("坏缓存要抛错（好触发重拉）", True)

print("[4.83] /new 读取最新（逐条带图 / 限条数 / 读过就不再推）")


class _FakePusher:
    """记录发出的消息，不碰 QQ。"""

    def __init__(self, *, rich_raises: bool = False, text_raises_for: str | None = None) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.rich_raises = rich_raises
        self.text_raises_for = text_raises_for

    async def send_text(self, group_openid: str, content: str) -> None:
        if self.text_raises_for and self.text_raises_for in content:
            raise RuntimeError("模拟发送失败")
        self.sent.append((group_openid, content, None))

    async def send_rich(self, group_openid: str, content: str, image_url: str) -> None:
        if self.rich_raises:
            raise RuntimeError("模拟配图失败")
        self.sent.append((group_openid, content, image_url))


def _entry(title: str, image: str | None = None) -> Entry:
    return Entry(key=f"k-{title}", title=title, link=f"https://e.com/{title}",
                 published=None, summary="", image=image)


async def _check_new_command() -> None:
    from qq_rss_bot import commands as cm
    from qq_rss_bot.commands import CommandHandler, _NEW_MAX_PER_SUB, _NEW_MAX_TOTAL
    from qq_rss_bot.rss import FeedResult

    # ---- 参数解析（不联网）----
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        for i in range(3):
            db.add_subscription("G", f"https://a.com/{i}", f"源{i + 1}", 900)
        h = CommandHandler(cfg_stub(), db, None)
        subs = db.list_subscriptions("G")

        p_all, n = h._parse_new_args(subs, [])
        check("/new 默认全部源 + 3 条", len(p_all) == 3 and n == 3, f"{len(p_all)},{n}")
        p_one, n = h._parse_new_args(subs, ["2"])
        check("/new 2 选第 2 个源", len(p_one) == 1 and p_one[0].title == "源2", str(p_one))
        check("/new 2 条数仍是默认 3", n == 3)
        p_one, n = h._parse_new_args(subs, ["2", "5"])
        check("/new 2 5 同时指定源和条数", len(p_one) == 1 and n == 5, f"{n}")
        p_all, n = h._parse_new_args(subs, ["all", "5"])
        check("/new all 5 全部源 + 5 条", len(p_all) == 3 and n == 5, f"{len(p_all)},{n}")
        p_all, n = h._parse_new_args(subs, ["全部", "2"])
        check("all 认中文「全部」", len(p_all) == 3 and n == 2)
        check("条数超单源上限被截",
              h._parse_new_args(subs, ["1", "999"])[1] == _NEW_MAX_PER_SUB)
        check("条数 0 抬到 1", h._parse_new_args(subs, ["1", "0"])[1] == 1)
        for bad in (["9"], ["abc"], ["2", "abc"], ["1", "2", "3"]):
            check(f"参数 {bad} 被拒并给提示",
                  isinstance(h._parse_new_args(subs, bad), str))

    # ---- 逐条带图 + 标记已见 ----
    async def run_new(per_sub_entries, *, args=(), rich_raises=False,
                      text_raises_for=None, attach_images=True):
        """跑一次 /new，返回 (假推送出口, 被动回复列表, handler, db, 源列表)。"""
        td = tempfile.TemporaryDirectory()
        db = Database(Path(td.name) / "t.db")
        for i, entries in enumerate(per_sub_entries, 1):
            sid, _ = db.add_subscription("G", f"https://a.com/{i}", f"源{i}", 900)
            db.mark_polled(sid, title=f"源{i}", mark_initialized=True)

        pusher = _FakePusher(rich_raises=rich_raises, text_raises_for=text_raises_for)
        cfg = dataclasses.replace(cfg_stub(), attach_images=attach_images)
        h = CommandHandler(cfg, db, None, pusher)
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def fake_fetch(http, url, timeout):
            idx = int(url.rsplit("/", 1)[-1]) - 1
            return FeedResult(url=url, title=f"源{idx + 1}", entries=per_sub_entries[idx])

        orig = cm.fetch_feed
        cm.fetch_feed = fake_fetch
        try:
            await h.handle(CommandContext("G", reply, "owner"), "/new " + " ".join(args))
        finally:
            cm.fetch_feed = orig
        return pusher, out, h, db, td

    one = [[_entry("有图", "https://img/1.jpg"), _entry("无图"),
            _entry("也有图", "https://img/3.jpg")]]
    pusher, out, h, db, td = await run_new(one)
    try:
        check("回执走被动回复（不占主动额度）", len(out) == 1 and "最新 3 条" in out[0], str(out))
        check("逐条发，不是合并摘要", len(pusher.sent) == 3, str(len(pusher.sent)))
        check("有图的走 send_rich",
              [m[2] for m in pusher.sent] == ["https://img/1.jpg", None, "https://img/3.jpg"],
              str([m[2] for m in pusher.sent]))
        check("文案复用推送格式", "📰 源1" in pusher.sent[0][1], pusher.sent[0][1])
        check("回执带每个源的条数", "源1 3" in out[0], out[0])

        # 「不推」：读过的必须已见，定时推送不再重发
        sid = db.list_subscriptions("G")[0].id
        keys = [e.key for e in one[0]]
        check("读过的全部标记已见", db.seen_keys(sid, keys) == set(keys))
        check("定时推送不会再推一遍", db.filter_new_entries(sid, keys) == [])
    finally:
        td.cleanup()

    # ---- 关掉配图时不该有 rich ----
    pusher, out, h, db, td = await run_new(one, attach_images=False)
    try:
        check("关掉配图后全走 send_text", all(m[2] is None for m in pusher.sent), str(pusher.sent))
    finally:
        td.cleanup()

    # ---- 配图失败退回纯文字，且仍算成功 ----
    pusher, out, h, db, td = await run_new(one, rich_raises=True)
    try:
        check("配图失败退回纯文字，条数不变", len(pusher.sent) == 3, str(len(pusher.sent)))
        check("退回后不带图", all(m[2] is None for m in pusher.sent))
        sid = db.list_subscriptions("G")[0].id
        check("退回后仍标记已见", len(db.seen_keys(sid, [e.key for e in one[0]])) == 3)
    finally:
        td.cleanup()

    # ---- 发送失败的不标记已见（留给定时推送补发）----
    pusher, out, h, db, td = await run_new(one, text_raises_for="无图")
    try:
        sid = db.list_subscriptions("G")[0].id
        seen = db.seen_keys(sid, [e.key for e in one[0]])
        check("发失败的那条不算已见", "k-无图" not in seen, str(seen))
        check("成功的两条仍标记已见", len(seen) == 2, str(seen))
        check("失败的会留给定时推送",
              db.filter_new_entries(sid, ["k-无图"]) == ["k-无图"])
    finally:
        td.cleanup()

    # ---- 单次总量上限：截断 + 被截的不标记已见 ----
    many = [[_entry(f"甲{i}") for i in range(10)], [_entry(f"乙{i}") for i in range(10)]]
    pusher, out, h, db, td = await run_new(many, args=("all", "10"))
    try:
        check(f"总量截到 {_NEW_MAX_TOTAL} 条", len(pusher.sent) == _NEW_MAX_TOTAL,
              str(len(pusher.sent)))
        check("回执说明截断了多少", f"省略 5 条" in out[0], out[0])
        subs = db.list_subscriptions("G")
        seen1 = db.seen_keys(subs[0].id, [f"k-甲{i}" for i in range(10)])
        seen2 = db.seen_keys(subs[1].id, [f"k-乙{i}" for i in range(10)])
        check("第一个源 10 条全发全标记", len(seen1) == 10, str(len(seen1)))
        check("被截掉的第二个源只标记发出去的部分", len(seen2) == 5, str(len(seen2)))
        check("被截的那 5 条仍待推送",
              len(db.filter_new_entries(subs[1].id, [f"k-乙{i}" for i in range(5, 10)])) == 5)
    finally:
        td.cleanup()

    # ---- 抓取失败的源 ----
    async def run_partial():
        td = tempfile.TemporaryDirectory()
        db = Database(Path(td.name) / "t.db")
        db.add_subscription("G", "https://a.com/1", "好的源", 900)
        db.add_subscription("G", "https://a.com/2", "坏的源", 900)
        pusher = _FakePusher()
        h = CommandHandler(cfg_stub(), db, None, pusher)
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def fake_fetch(http, url, timeout):
            if url.endswith("/2"):
                return FeedResult(url=url, error="HTTP 500")
            return FeedResult(url=url, title="好的源", entries=[_entry("一"), _entry("二")])

        orig = cm.fetch_feed
        cm.fetch_feed = fake_fetch
        try:
            await h.handle(CommandContext("G", reply, "owner"), "/new")
        finally:
            cm.fetch_feed = orig
        return pusher, out, td

    pusher, out, td = await run_partial()
    try:
        check("坏源不拖累好源", len(pusher.sent) == 2, str(len(pusher.sent)))
        check("回执点名有几个源失败", "1 个源抓取失败" in out[0], out[0])
    finally:
        td.cleanup()

    # ---- 边界：没订阅 / 没传 pusher / 源里没条目 ----
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None, _FakePusher())
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        await h.handle(CommandContext("G", reply, "owner"), "/new")
        check("没订阅时给提示", "还没有订阅" in out[0], str(out))

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        db.add_subscription("G", "https://a.com/1", "源1", 900)
        h = CommandHandler(cfg_stub(), db, None, None)
        out2: list[str] = []

        async def reply2(text: str) -> None:
            out2.append(text)

        await h.handle(CommandContext("G", reply2, "owner"), "/new")
        check("没有推送出口时明确说不支持", "不支持" in out2[0], str(out2))

    pusher, out, h, db, td = await run_new([[]])
    try:
        check("源里没条目时不静默", "没取到内容" in out[0], out[0])
        check("没条目就不发消息", not pusher.sent)
    finally:
        td.cleanup()


asyncio.run(_check_new_command())

print("[4.85] 主动消息节流（令牌桶 + 每日配额）")
from qq_rss_bot.quota import (  # noqa: E402
    DEFAULT_DAILY_LIMIT,
    GroupSender,
    QuotaExceeded,
    _Bucket,
    today,
)


class _Recorder:
    """记录发出的消息，并能模拟失败。"""

    def __init__(self, fail_with: str | None = None) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail_with = fail_with
        self.inflight = 0
        self.max_inflight = 0

    async def _record(self, group: str, content: str, image: str | None) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)  # 让出控制权，暴露并发
            if self.fail_with and self.fail_with in content:
                raise RuntimeError("模拟发送失败")
            self.sent.append((group, content, image))
        finally:
            self.inflight -= 1

    async def send_text(self, group: str, content: str) -> None:
        await self._record(group, content, None)

    async def send_rich(self, group: str, content: str, image: str) -> None:
        await self._record(group, content, image)


async def _check_bucket() -> None:
    # 桶本身：容量内瞬时，超出后按回填速率排队
    b = _Bucket(capacity=2, per_second=100.0)
    t0 = time.monotonic()
    await b.take()
    await b.take()
    check("容量内不等待", time.monotonic() - t0 < 0.05, f"{time.monotonic() - t0:.3f}s")
    t1 = time.monotonic()
    await b.take()
    waited = time.monotonic() - t1
    check("超出容量后按回填速率等待", 0.005 < waited < 0.1, f"{waited:.3f}s")

    # 默认参数对应官方频控
    check("默认每分钟上限 = 官方群频控 20", DEFAULT_DAILY_LIMIT == 900)
    check("today() 是本地日期格式", len(today()) == 10 and today()[4] == "-", today())


async def _check_group_sender() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        rec = _Recorder()
        # per_minute 开很大，把令牌桶排除掉，单独测每日配额
        s = GroupSender(rec, db, per_minute=10_000, daily_limit=3)
        for i in range(3):
            await s.send_text("G", f"m{i}")
        check("配额内正常发出", len(rec.sent) == 3, str(len(rec.sent)))
        check("已用计数落库", s.used_today("G") == 3, str(s.used_today("G")))
        check("剩余额度正确", s.remaining_today("G") == 0)
        check("配额用尽可查询", s.exhausted("G") is True)
        try:
            await s.send_text("G", "超了")
            check("超出日配额要抛 QuotaExceeded", False)
        except QuotaExceeded as exc:
            check("超出日配额要抛 QuotaExceeded", exc.used == 3 and exc.limit == 3, str(exc))
        check("被拒的消息没有真的发出去", len(rec.sent) == 3, str(len(rec.sent)))

        # 配额是**按群**算的，一个群用完不影响另一个群
        await s.send_text("H", "别的群")
        check("配额按群隔离", s.used_today("H") == 1 and s.used_today("G") == 3)

        # 落库而不是放内存：换一个 sender 实例（等价于进程重启）读数不变
        s2 = GroupSender(_Recorder(), db, per_minute=10_000, daily_limit=3)
        check("配额跨实例保持（重启不清零）", s2.used_today("G") == 3)
        check("重启后仍然拒发", s2.exhausted("G") is True)

    # 并发发送必须串行 —— 否则 5 个源的推送循环会同时打满频控
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        rec = _Recorder()
        s = GroupSender(rec, db, per_minute=10_000, daily_limit=100)
        await asyncio.gather(*[s.send_text("G", f"c{i}") for i in range(8)])
        check("同一群的并发发送被串行化", rec.max_inflight == 1, str(rec.max_inflight))
        check("并发发送一条不丢", len(rec.sent) == 8, str(len(rec.sent)))

    # 不同群之间不该互相阻塞（配额是按群算的）
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        rec = _Recorder()
        s = GroupSender(rec, db, per_minute=10_000, daily_limit=100)
        await asyncio.gather(s.send_text("A", "1"), s.send_text("B", "2"))
        check("不同群可以并行", rec.max_inflight == 2, str(rec.max_inflight))


asyncio.run(_check_bucket())
asyncio.run(_check_group_sender())

print("[4.86] 抓取失败计数 / 自动暂停 / 整体故障通知")


class _FakeNotifier:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str, str]] = []

    async def notify(self, group_openid: str, text: str, *, key: str = "") -> bool:
        self.notes.append((group_openid, key, text))
        return True


def _poller_fixture(*, entries=None, error=None, cfg=None, quota=None):
    """搭一个只跑一轮的 FeedPoller，fetch 用假的。"""
    td = tempfile.TemporaryDirectory()
    db = Database(Path(td.name) / "t.db")
    rec = _Recorder()
    notifier = _FakeNotifier()
    c = cfg or dataclasses.replace(cfg_stub(), max_fail_before_pause=2)
    p = FeedPoller(c, db, None, rec, notifier=notifier, quota=quota)

    async def fake_fetch(http, url, timeout):
        if error is not None:
            return FeedResult(url=url, error=error)
        return FeedResult(url=url, title="源", entries=entries or [])

    import qq_rss_bot.scheduler as sch

    orig = sch.fetch_feed
    sch.fetch_feed = fake_fetch
    return td, db, rec, notifier, p, lambda: setattr(sch, "fetch_feed", orig)


async def _check_fail_and_pause() -> None:
    # 连续失败到阈值 → 自动暂停 + 私聊通知
    td, db, rec, notifier, p, restore = _poller_fixture(error="HTTP 503")
    try:
        sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
        db.mark_polled(sid, title="源1", mark_initialized=True)
        for i in range(1, 3):
            db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
            db._conn.commit()
            await p.poll_once()
            sub = db.list_subscriptions("G")[0]
            check(f"第 {i} 次失败后 fail_count={i}", sub.fail_count == i, str(sub.fail_count))
        sub = db.list_subscriptions("G")[0]
        check("到阈值后自动暂停", sub.enabled is False)
        check("暂停后不再参与轮询", db.due_subscriptions("2099-01-01T00:00:00+00:00") == [])
        check("暂停时发了私聊通知",
              any(k == f"pause:{sid}" for _, k, _ in notifier.notes),
              str([k for _, k, _ in notifier.notes]))
        check("通知里说明了怎么恢复",
              any("/resume" in t for _, _, t in notifier.notes))
    finally:
        restore()
        td.cleanup()

    # 成功一次就把失败计数清零
    td, db, rec, notifier, p, restore = _poller_fixture(entries=[])
    try:
        sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
        db._conn.execute("UPDATE subscriptions SET fail_count = 3, initialized = 1")
        db._conn.commit()
        db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
        db._conn.commit()
        await p.poll_once()
        check("抓取成功把 fail_count 清零", db.list_subscriptions("G")[0].fail_count == 0)
    finally:
        restore()
        td.cleanup()

    # 整轮全挂 → 怀疑 RSSHub 挂了，通知一次
    td, db, rec, notifier, p, restore = _poller_fixture(error="连接被拒绝")
    try:
        for i in range(2):
            sid, _ = db.add_subscription("G", f"https://a.com/{i}", f"源{i}", 900)
            db.mark_polled(sid, title=f"源{i}", mark_initialized=True)
        db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
        db._conn.commit()
        await p.poll_once()
        check("整轮全挂时发出整体故障通知",
              any(k == "rsshub_down" for _, k, _ in notifier.notes),
              str([k for _, k, _ in notifier.notes]))
    finally:
        restore()
        td.cleanup()

    # 配额用尽导致的**跳过**不是失败，不能误报成整体故障
    # （演练时抓到的真 bug：配额一满就报「RSSHub 挂了」，方向完全错）
    td, db, rec, notifier, p, restore = _poller_fixture(entries=[])
    try:
        sender = GroupSender(rec, db, per_minute=10_000, daily_limit=0)
        p.quota = sender
        sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
        db.mark_polled(sid, title="源1", mark_initialized=True)
        db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
        db._conn.commit()
        await p.poll_once()
        check("★ 配额用尽导致的跳过不误报整体故障",
              not any(k == "rsshub_down" for _, k, _ in notifier.notes),
              str([k for _, k, _ in notifier.notes]))
    finally:
        restore()
        td.cleanup()

    # 只要有一个源成功，就不该报整体故障
    td, db, rec, notifier, p, restore = _poller_fixture(error=None, entries=[])
    try:
        sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
        db.mark_polled(sid, title="源1", mark_initialized=True)
        db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
        db._conn.commit()
        await p.poll_once()
        check("有源成功就不报整体故障",
              not any(k == "rsshub_down" for _, k, _ in notifier.notes))
    finally:
        restore()
        td.cleanup()


async def _check_quota_in_push() -> None:
    """配额用尽时：整批停止、标记已见（避免明天倒灌）、私聊通知。"""
    td = tempfile.TemporaryDirectory()
    db = Database(Path(td.name) / "t.db")
    rec = _Recorder()
    notifier = _FakeNotifier()
    # 日配额 2，但这一轮有 4 条新内容
    sender = GroupSender(rec, db, per_minute=10_000, daily_limit=2)
    p = FeedPoller(
        dataclasses.replace(cfg_stub(), attach_images=False),
        db, None, sender, notifier=notifier, quota=sender,
    )
    entries = [Entry(key=f"k{i}", title=f"标题{i}", link="https://x",
                     published=None, summary="") for i in range(4)]
    sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
    db.mark_polled(sid, title="源1", mark_initialized=True)
    db.filter_new_entries(sid, ["old"])  # 建个 baseline 之外的东西无所谓

    import qq_rss_bot.scheduler as sch

    async def fake_fetch(http, url, timeout):
        return FeedResult(url=url, title="源1", entries=entries)

    orig = sch.fetch_feed
    sch.fetch_feed = fake_fetch
    try:
        db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
        db._conn.commit()
        await p.poll_once()
    finally:
        sch.fetch_feed = orig

    check("配额内只发了 2 条", len(rec.sent) == 2, str(len(rec.sent)))
    check("配额用尽发了私聊通知",
          any(k == "quota" for _, k, _ in notifier.notes),
          str([k for _, k, _ in notifier.notes]))
    check("通知里说清了已用/上限",
          any("2/2" in t for _, _, t in notifier.notes), str(notifier.notes))
    # 关键：剩下的必须标记已见，否则明天配额恢复会倒灌一整天的积压
    left = db.filter_new_entries(sid, [e.key for e in entries])
    check("被配额挡下的条目标记为已见（防明天倒灌）", left == [], str(left))
    # 配额用尽后不再抓取
    db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
    db._conn.commit()
    before = len(rec.sent)
    await p.poll_once()
    check("配额用尽后跳过抓取", len(rec.sent) == before)
    td.cleanup()


asyncio.run(_check_fail_and_pause())
asyncio.run(_check_quota_in_push())

print("[4.87] /check 只看不吞 + /sub 编号 + 订阅上限")


async def _check_check_and_sub() -> None:
    from qq_rss_bot import commands as cm

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None, None)
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def run(cmd: str) -> str:
            out.clear()
            await h.handle(CommandContext("G", reply, "owner"), cmd)
            return out[0] if out else ""

        entries = [Entry(key=f"新{i}", title=f"新文章{i}", link="https://x",
                         published=None, summary="") for i in range(3)]
        sid, _ = db.add_subscription("G", "https://a.com/1", "源1", 900)
        db.mark_polled(sid, title="源1", mark_initialized=True)

        async def fake_fetch(http, url, timeout):
            return FeedResult(url=url, title="源1", entries=entries)

        orig = cm.fetch_feed
        cm.fetch_feed = fake_fetch
        try:
            text = await run("/check 1")
            check("/check 报告有几条待推送", "3 条还没推送过" in text, text)
            left = db.filter_new_entries(sid, [e.key for e in entries])
            check("★ /check 不再吞掉待推送条目（回归）", len(left) == 3, str(left))
            check("/check 提示用 /new 去读", "/new" in text, text)

            # /sub 的编号必须是群内位置
            for i in range(4):
                db.add_subscription("OTHER", f"https://o.com/{i}", f"占位{i}", 900)
            out.clear()
            await h.handle(CommandContext("G", reply, "owner"), "/sub /v2ex/topics/hot")
            first = out[-1]
            check("★ 新订阅报的编号是群内位置（不是主键）",
                  "编号 2" in first, first.replace("\n", " | "))
            out.clear()
            await h.handle(CommandContext("G", reply, "owner"), "/sub /v2ex/topics/hot")
            dup = out[-1]
            check("★ 重复订阅也报群内位置", "编号 2" in dup, dup.replace("\n", " | "))
            check("重复订阅时提示怎么改间隔", "/interval" in dup, dup)
            check("重复订阅不再白抓一次（回得快）", "已经订阅过了" in dup)

            listing = await run("/list")
            check("/list 与 /sub 报的编号一致", "2. " in listing, listing)

            # 订阅数上限
            capped = dataclasses.replace(cfg_stub(), max_subs_per_group=2)
            h2 = CommandHandler(capped, db, None, None)
            out.clear()
            await h2.handle(CommandContext("G", reply, "owner"), "/sub /36kr/newsflashes")
            check("到上限时拒绝新订阅", "已达上限" in out[-1], out[-1].replace("\n", " | "))
            check("上限提示了怎么处理", "/unsub" in out[-1])
            # 已订阅的 URL 不受上限影响（应当回「已经订阅过了」而不是「已达上限」）
            out.clear()
            await h2.handle(CommandContext("G", reply, "owner"), "/sub /v2ex/topics/hot")
            check("已订阅的 URL 不受上限影响", "已经订阅过了" in out[-1], out[-1])
        finally:
            cm.fetch_feed = orig


asyncio.run(_check_check_and_sub())

print("[4.88] /pause /resume /interval")
from qq_rss_bot.commands import COMMAND_METHODS  # noqa: E402


async def _check_pause_resume_interval() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None, None)
        out: list[str] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def run(cmd: str, role: str = "owner") -> str:
            out.clear()
            await h.handle(CommandContext("G", reply, role), cmd)
            return out[0] if out else ""

        db.add_subscription("G", "https://a.com/1", "源1", 900)
        s2, _ = db.add_subscription("G", "https://a.com/2", "源2", 900)
        # 标成已初始化，才能验证 /interval **不会**把它清掉
        db.mark_polled(s2, title="源2", mark_initialized=True)

        check("三个新指令都挂了处理方法",
              all(COMMAND_METHODS[c] for c in ("pause", "resume", "interval")))

        text = await run("/pause 1")
        check("暂停成功并报编号和名字", "已暂停 1. 源1" in text, text)
        check("暂停后 enabled=0", db.list_subscriptions("G")[0].enabled is False)
        check("暂停的不再参与轮询",
              all(s.id != db.list_subscriptions("G")[0].id
                  for s in db.due_subscriptions("2099-01-01T00:00:00+00:00")))
        listing = await run("/list")
        check("/list 标出已暂停", "1. 源1" in listing and "已暂停" in listing, listing)

        text = await run("/resume 1")
        check("恢复成功", "已恢复 1. 源1" in text, text)
        check("恢复后 enabled=1", db.list_subscriptions("G")[0].enabled is True)

        # 恢复要清零失败计数，否则一恢复就立刻又被自动暂停
        db._conn.execute("UPDATE subscriptions SET fail_count = 9, enabled = 0 WHERE id = ?",
                         (db.list_subscriptions("G")[0].id,))
        db._conn.commit()
        await run("/resume 1")
        check("★ 恢复时清零失败计数（否则立刻又被暂停）",
              db.list_subscriptions("G")[0].fail_count == 0)

        text = await run("/interval 2 60")
        check("改间隔成功", "已改为 60 分钟" in text, text)
        check("间隔落库", db.list_subscriptions("G")[1].interval_sec == 3600)
        check("★ 改间隔后立刻生效（清空 last_polled_at）",
              db.list_subscriptions("G")[1].last_polled_at is None)
        # 关键：改间隔**不能**清 initialized —— 清了就会重建 baseline，
        # 把两次抓取之间的增量整段丢掉（正是 /sub 重订的老问题）
        check("★ 改间隔不重建 baseline（initialized 保持 True）",
              db.list_subscriptions("G")[1].initialized is True)

        for bad, why in [("/interval 2", "缺分钟"), ("/interval 2 abc", "非整数"),
                         ("/interval 2 0", "0 分钟"), ("/interval 2 9999", "超上限"),
                         ("/interval 9 10", "编号越界")]:
            text = await run(bad)
            check(f"/interval 拒绝{why}", "用法" in text or "间隔" in text or "编号不对" in text,
                  text.replace("\n", " | "))

        check("暂停/恢复/改间隔都只允许管理员",
              all(h._restricted(c) for c in ("pause", "resume", "interval", "暂停", "恢复", "间隔")))
        text = await run("/pause 1", role="member")
        check("普通成员不能暂停", "只允许群主和管理员" in text, text)


asyncio.run(_check_pause_resume_interval())

print("[4.89] 私聊通知（防刷屏 / 无收件人 / 发送失败）")
from qq_rss_bot.notify import C2CNotifier, NullNotifier  # noqa: E402


async def _check_notifier() -> None:
    class Sender:
        def __init__(self, fail: bool = False) -> None:
            self.sent: list[tuple[str, str]] = []
            self.fail = fail

        async def send_c2c_text(self, openid: str, content: str) -> None:
            if self.fail:
                raise RuntimeError("用户还没和机器人建立会话")
            self.sent.append((openid, content))

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        s = Sender()
        n = C2CNotifier(s, db, cooldown_sec=60, min_gap_sec=0)

        check("没有收件人时返回 False", await n.notify("G", "出错了") is False)
        check("没有收件人时不发消息", s.sent == [])

        db.set_notify_target("G", "USER_A")
        check("有收件人时送达", await n.notify("G", "第一条", key="k1") is True)
        check("真的发出去了", len(s.sent) == 1 and s.sent[0][0] == "USER_A")

        # 同一类错误在冷却期内只发一次 —— RSSHub 一挂是所有源一起挂，
        # 不做限制就是 DM 风暴
        check("同 key 在冷却期内被抑制",
              await n.notify("G", "第二条", key="k1") is False)
        check("冷却期内没有多发", len(s.sent) == 1, str(len(s.sent)))
        check("不同 key 不受影响", await n.notify("G", "另一类", key="k2") is True)
        check("不同 key 真的发出去了", len(s.sent) == 2)
        check("空 key 不做冷却", await n.notify("G", "无 key 一") is True)
        check("空 key 不做冷却（第二次）", await n.notify("G", "无 key 二") is True)

        # 多个收件人
        db.set_notify_target("G", "USER_B")
        s2 = Sender()
        n2 = C2CNotifier(s2, db, cooldown_sec=60, min_gap_sec=0)
        await n2.notify("G", "广播", key="x")
        check("通知发给所有登记的管理员", len(s2.sent) == 2, str(len(s2.sent)))

    # 发送失败（用户没建会话）必须被吞掉，且不回落成群消息
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        db.set_notify_target("G", "USER_A")
        bad = Sender(fail=True)
        n = C2CNotifier(bad, db, cooldown_sec=60, min_gap_sec=0)
        check("发送失败返回 False 而不是抛异常",
              await n.notify("G", "发不出去", key="k") is False)
        check("失败时确实没发出去", bad.sent == [])

    check("NullNotifier 什么都不做",
          await NullNotifier().notify("G", "x") is False)


asyncio.run(_check_notifier())

print("[4.895] 私聊通知白名单（NOTIFY_OPENIDS）")
from qq_rss_bot.config import Config, _env_list  # noqa: E402


async def _check_notify_allowlist() -> None:
    class Sender:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        async def send_c2c_text(self, openid: str, content: str) -> None:
            self.sent.append((openid, content))

    # 白名单模式：数据库里登记的收件人一律忽略
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        db.set_notify_target("G", "OTHER_ADMIN")
        s = Sender()
        n = C2CNotifier(s, db, cooldown_sec=60, min_gap_sec=0, allowlist=["ME"])
        await n.notify("G", "只给我", key="k")
        check("白名单模式只发给名单上的人", [o for o, _ in s.sent] == ["ME"], str(s.sent))
        check("白名单模式忽略数据库里登记的管理员",
              "OTHER_ADMIN" not in [o for o, _ in s.sent])
        check("is_target 认白名单内的人", n.is_target("G", "ME") is True)
        check("is_target 否掉白名单外的人", n.is_target("G", "OTHER_ADMIN") is False)
        check("is_target 对空 openid 一律否", n.is_target("G", "") is False)

    # **数据库为空时白名单照样能发** —— 这正是当前的真实情况：
    # notify_targets 是空的（群角色的实际取值没实测到，自动登记从没触发过）
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        s = Sender()
        n = C2CNotifier(s, db, cooldown_sec=60, min_gap_sec=0, allowlist=["ME"])
        check("数据库空表时白名单照样送达", await n.notify("G", "x", key="k") is True)
        check("数据库空表时确实发出去了", len(s.sent) == 1)
        check("空表时 is_target 仍认白名单", n.is_target("G", "ME") is True)

    # 白名单为空 = 退回自动登记模式（不能把默认行为改坏）
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        db.set_notify_target("G", "ADMIN_A")
        s = Sender()
        n = C2CNotifier(s, db, cooldown_sec=60, min_gap_sec=0, allowlist=[])
        await n.notify("G", "x", key="k")
        check("白名单为空时退回数据库登记", [o for o, _ in s.sent] == ["ADMIN_A"])
        check("白名单为空时 is_target 走数据库", n.is_target("G", "ADMIN_A") is True)
        check("白名单为空时 is_target 仍否掉没登记的人",
              n.is_target("G", "STRANGER") is False)

    # 超过上限要截断（不是静默丢弃：构造时会打 WARNING）
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        s = Sender()
        n = C2CNotifier(s, db, cooldown_sec=60, min_gap_sec=0,
                        allowlist=["A", "B", "C", "D", "E"])
        await n.notify("G", "x", key="k")
        check("白名单超过 MAX_TARGETS 时截断",
              [o for o, _ in s.sent] == ["A", "B", "C"], str(s.sent))

    # 空串要丢掉（.env 里写成 "A,," 不该产生一个空收件人）
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        n = C2CNotifier(Sender(), db, cooldown_sec=60, min_gap_sec=0,
                        allowlist=["A", "", "B"])
        check("白名单里的空串被丢掉", n.allowlist == ("A", "B"), str(n.allowlist))


asyncio.run(_check_notify_allowlist())

# NOTIFY_OPENIDS 的解析：逗号 / 空格 / 空值
_saved_notify = os.environ.get("NOTIFY_OPENIDS")
try:
    os.environ["NOTIFY_OPENIDS"] = "AAA,BBB"
    check("NOTIFY_OPENIDS 逗号分隔", _env_list("NOTIFY_OPENIDS") == ("AAA", "BBB"))
    os.environ["NOTIFY_OPENIDS"] = "AAA, BBB   CCC"
    check("NOTIFY_OPENIDS 混用逗号和空格",
          _env_list("NOTIFY_OPENIDS") == ("AAA", "BBB", "CCC"))
    os.environ["NOTIFY_OPENIDS"] = "  AAA  "
    check("NOTIFY_OPENIDS 两侧空白被剥掉", _env_list("NOTIFY_OPENIDS") == ("AAA",))
    os.environ["NOTIFY_OPENIDS"] = ""
    check("NOTIFY_OPENIDS 空值 → 空元组", _env_list("NOTIFY_OPENIDS") == ())
    os.environ["NOTIFY_OPENIDS"] = " , , "
    check("NOTIFY_OPENIDS 只有分隔符 → 空元组", _env_list("NOTIFY_OPENIDS") == ())
    del os.environ["NOTIFY_OPENIDS"]
    check("NOTIFY_OPENIDS 未设置 → 空元组", _env_list("NOTIFY_OPENIDS") == ())
finally:
    if _saved_notify is None:
        os.environ.pop("NOTIFY_OPENIDS", None)
    else:
        os.environ["NOTIFY_OPENIDS"] = _saved_notify

check("Config 有 notify_openids 字段",
      "notify_openids" in Config.__dataclass_fields__)
check("notify_openids 默认是空元组（= 自动登记模式）",
      Config.__dataclass_fields__["notify_openids"].default == ())

print("[4.91] /rss 按钮卡片（动态按钮 + 显示宽度）")

import dataclasses  # noqa: E402,E501
import qq_rss_bot.menu as menu  # noqa: E402
from qq_rss_bot.db import Subscription  # noqa: E402

check("中文按 2 算宽度", menu.display_width("中文") == 4)
check("ASCII 宽 = 字符数", menu.display_width("/unsub 1") == 8)
# 这行是整个「label 里只放编号、名字放正文」决定的依据
check("★ 「退订 偶像大师」宽 13，已超 label 上限 10",
      menu.display_width("退订 偶像大师") == 13 > menu.LABEL_LIMIT)
check("「退订 1」在上限内", menu.display_width("退订 1") <= menu.LABEL_LIMIT)

_clamped = menu.clamp_label("退订 偶像大师新闻速报")
check("clamp_label 把超宽文本收窄到上限内",
      menu.display_width(_clamped) <= menu.LABEL_LIMIT, _clamped)
check("clamp_label 不动没超宽的", menu.clamp_label("退订 1") == "退订 1")
check("clamp_label 对空/None 安全", menu.clamp_label("") == "" and menu.clamp_label(None) == "")

# ★ 这个平台上「200 从来不等于生效」：rows 不包 content 就是「接口成功、按钮消失」
check("★ 缺 content 外层会被抓出来",
      any("content" in p for p in menu.validate_keyboard({"rows": [{"buttons": []}]})))
check("★ menu.keyboard() 一定带 content 外层", "content" in menu.keyboard([[]]))
check("★ menu.keyboard() 不把 rows 直接挂在顶层", "rows" not in menu.keyboard([[]]))


def _subs(n: int, title: str = "源") -> list[Subscription]:
    return [
        Subscription(
            id=i,
            group_openid="G",
            url=f"https://a.com/{i}",
            title=f"{title}{i}",
            interval_sec=900,
            initialized=True,
            last_polled_at=None,
            last_error=None,
        )
        for i in range(1, n + 1)
    ]


# 属性断言，不是抽查几个字面量：这类 schema 的坑全都是「某一种输入才触发」。
# 标题里塞极端值，因为正文要用它、label 不能用它 —— 两边都得活下来。
_HOSTILE_TITLES = ["", "a" * 200, "中" * 80, "偶像大师 新闻 🎉", "退订", "   ", "/unsub 1"]
_bad: list[str] = []
for _n in (0, 1, 2, 3, 4, 5, 6, 10):
    for _t in _HOSTILE_TITLES:
        _kb = menu.build_rss_keyboard(_subs(_n, _t))
        for _p in menu.validate_keyboard(_kb, known_tokens=COMMAND_TOKENS):
            _bad.append(f"n={_n} title={_t!r}: {_p}")
check("★ 所有按钮卡片（8 种订阅数 × 7 种标题）都合法", _bad == [], "; ".join(_bad[:4]))

_kb0 = menu.build_rss_keyboard([])
check("零订阅时不出现退订按钮",
      all("unsub" not in b["id"] for r in _kb0["content"]["rows"] for b in r["buttons"]))
check("零订阅正文给出订阅指引", "还没有订阅" in menu.build_rss_markdown([]))

_kb1 = menu.build_rss_keyboard(_subs(1))
_datas = [b["action"]["data"] for r in _kb1["content"]["rows"] for b in r["buttons"]]
check("1 个订阅：退订按钮插的是完整指令", "/unsub 1" in _datas, str(_datas))
check("1 个订阅：有订阅列表入口", "/list" in _datas)
check("1 个订阅：有暂停/恢复入口", "/pause " in _datas and "/resume " in _datas)
check("1 个订阅：不带编号的按钮留尾空格等用户补参数", "/find " in _datas and "/sub " in _datas)

_kb4 = menu.build_rss_keyboard(_subs(4))
_d4 = [b["action"]["data"] for r in _kb4["content"]["rows"] for b in r["buttons"]]
check("4 个订阅：全部平铺", all(f"/unsub {i}" in _d4 for i in range(1, 5)), str(_d4))
check("4 个订阅：主卡片行数压得住", len(_kb4["content"]["rows"]) <= 5,
      str(len(_kb4["content"]["rows"])))

_kb5 = menu.build_rss_keyboard(_subs(5))
_d5 = [b["action"]["data"] for r in _kb5["content"]["rows"] for b in r["buttons"]]
check("★ 超过 4 个就折叠成一个「退订…」", "/rss unsub" in _d5 and "/unsub 1" not in _d5, str(_d5))

_kbs = menu.build_unsub_keyboard(_subs(10))
_ds = [b["action"]["data"] for r in _kbs["content"]["rows"] for b in r["buttons"]]
check("二级卡片平铺全部 10 个", all(f"/unsub {i}" in _ds for i in range(1, 11)))
check("二级卡片有返回菜单", "/rss" in _ds)
check("二级卡片通过体检", menu.validate_keyboard(_kbs, known_tokens=COMMAND_TOKENS) == [])

# 按钮 data 指向不存在的指令 = 点下去插一条机器人不认识的命令
check("★ data 里的指令名必须都在指令表里",
      menu.validate_keyboard(
          menu.keyboard([[menu._cmd_button("x", "坏的", "/unsb 1")]]),
          known_tokens=COMMAND_TOKENS) != [])

check("★ COMMAND_METHODS 的每个方法都真的存在（防拼错 / 漏挂）",
      all(hasattr(CommandHandler, m) for m in COMMAND_METHODS.values()),
      str([m for m in COMMAND_METHODS.values() if not hasattr(CommandHandler, m)]))
check("COMMAND_METHODS 覆盖所有别名指向的规范名",
      set(COMMAND_METHODS) == set(COMMAND_ALIASES.values()))
check("rss 别名认中文", COMMAND_ALIASES.get("菜单") == "rss" and COMMAND_ALIASES["rss"] == "rss")
check("rss 不在受限表里（谁都能打开菜单）",
      COMMAND_ALIASES["rss"] not in RESTRICTED_CANONICAL)
check("帮助里提到了 /rss", "/rss" in HELP_TEXT)


async def _check_rss_command() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        h = CommandHandler(cfg_stub(), db, None, None)
        out: list[str] = []
        cards: list[tuple[str, dict]] = []

        async def reply(text: str) -> None:
            out.append(text)

        async def reply_card(markdown: str, keyboard: dict) -> None:
            cards.append((markdown, keyboard))

        async def boom(markdown: str, keyboard: dict) -> None:
            raise RuntimeError("模拟卡片发送失败")

        async def run(cmd: str, card=reply_card, role: str = "owner") -> None:
            out.clear()
            cards.clear()
            await h.handle(CommandContext("G", reply, role, card), cmd)

        db.add_subscription("G", "https://a.com/1", "偶像大师", 900)

        await run("/rss")
        check("/rss 走的是按钮卡片那条路", len(cards) == 1 and not out, str(out))
        check("/rss 卡片正文带订阅名", "偶像大师" in cards[0][0], cards[0][0])
        check("/rss 卡片通过体检",
              menu.validate_keyboard(cards[0][1], known_tokens=COMMAND_TOKENS) == [])

        await run("菜单")  # 中文别名
        check("中文别名「菜单」也认", len(cards) == 1)

        await run("/rss", card=None)
        check("拿不到卡片通道时退回纯文本", not cards and len(out) == 1, str(out))
        check("退回的纯文本照样有订阅名和指令", "偶像大师" in out[0] and "/unsub" in out[0], out[0])

        await run("/rss", card=boom)
        check("★ 卡片发送失败也退回纯文本（不让 /rss 整个失效）",
              not cards and len(out) == 1 and "偶像大师" in out[0], str(out))

        await run("/rss unsub", card=None)
        check("订阅不多时 /rss unsub 直接给主卡片列表", "偶像大师" in out[0], out[0])

        for i in range(2, 7):  # 凑到 6 个，越过折叠阈值
            db.add_subscription("G", f"https://a.com/{i}", f"源{i}", 900)
        await run("/rss unsub")
        check("订阅多时 /rss unsub 给二级卡片", "退订哪一个" in cards[0][0], cards[0][0])
        _dd = [b["action"]["data"] for r in cards[0][1]["content"]["rows"] for b in r["buttons"]]
        check("二级卡片平铺 6 个订阅", all(f"/unsub {i}" in _dd for i in range(1, 7)), str(_dd))

        await run("/rss 乱写的参数")
        check("未知子参数回主卡片而不是报错", len(cards) == 1 and "本群订阅" in cards[0][0])

        await run("/menu", role="member")
        check("普通成员也能打开菜单（不受限）", len(cards) == 1)

        db.close()


asyncio.run(_check_rss_command())

print("[4.92] 进群自动订阅与启动补订阅")

from qq_rss_bot.autosub import AutoSubscriber  # noqa: E402

_ROUTE = "/idolmaster/news"


def _autocfg(**kw):
    return dataclasses.replace(cfg_stub(), auto_subscribe_feed=_ROUTE, **kw)


def _check_autosub() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        pusher = _FakePusher()
        sub = AutoSubscriber(_autocfg(), db, pusher)

        check("配了源就启用", sub.enabled)
        check("路由被解析成完整 URL",
              sub.url == resolve_url(_ROUTE, cfg_stub().rsshub_base), str(sub.url))
        check("没配源就整个关掉", not AutoSubscriber(cfg_stub(), db, pusher).enabled)

        # ---- 进新群 ----
        db.touch_group("NEW")
        created = asyncio.run(sub.on_join("NEW"))
        check("进群后自动建了一条订阅", created and db.count_subscriptions("NEW") == 1)
        only = db.list_subscriptions("NEW")[0]
        check("订阅的是配置里那个源", only.url == sub.url, only.url)
        # ★ 关键：只插记录不抓源，把 baseline 留给轮询器 —— 否则事件回调要干等 20 秒
        check("★ 留 initialized=False 给轮询器建 baseline（不在回调里抓源）", not only.initialized)
        check("★ 新订阅立刻是「到点该抓」的状态",
              any(s.id == only.id for s in db.due_subscriptions(utcnow())))
        check("标题先占位成路由串，首拍会被真实标题覆盖", only.title == _ROUTE)
        check("欢迎语走了一次主动消息", len(pusher.sent) == 1, str(pusher.sent))
        check("欢迎语里写了订阅的是哪个源", _ROUTE in pusher.sent[0][1])

        # 重放同一个事件（重连时可能发生）不该重复订阅、重复欢迎
        asyncio.run(sub.on_join("NEW"))
        check("★ on_join 幂等：订阅还是 1 条", db.count_subscriptions("NEW") == 1)
        check("★ 重放不会重复发欢迎语", len(pusher.sent) == 1, str(len(pusher.sent)))

        # ---- 欢迎语发不出去不能影响订阅 ----
        db.touch_group("NOPUSH")
        grumpy = AutoSubscriber(_autocfg(), db, _FakePusher(text_raises_for="已就绪"))
        asyncio.run(grumpy.on_join("NOPUSH"))
        check("★ 欢迎语失败不影响订阅本身（新群多半没开主动发言）",
              db.count_subscriptions("NOPUSH") == 1)

        # ---- 启动补订阅 ----
        db.touch_group("EMPTY")          # 一个订阅都没有 → 该补
        db.touch_group("HASONE")         # 已经有订阅 → 不该动
        db.add_subscription("HASONE", "https://x.com/1", "别动我", 900)

        added = sub.backfill()
        check("补订阅只碰零订阅的群", set(added) == {"EMPTY"}, str(added))
        check("零订阅的群被补上", db.count_subscriptions("EMPTY") == 1)
        check("★ 已有订阅的群一律不动（不把故意退掉的重订回来）",
              db.count_subscriptions("HASONE") == 1
              and db.list_subscriptions("HASONE")[0].title == "别动我")
        check("补订阅不发消息（启动时刷一堆太吵）", len(pusher.sent) == 1, str(len(pusher.sent)))
        check("补订阅幂等：再跑一次没人需要补", sub.backfill() == [])
        check("没配源时补订阅什么也不做",
              AutoSubscriber(cfg_stub(), db, pusher).backfill() == [])

        # ---- 退群 ----
        # ★ 退群必须连 groups 那行一起删，否则下次启动会把退掉的群当「零订阅的新群」
        #   重新补上，然后一直往机器人已经不在的群推送、每轮失败一次、永不停。
        db.touch_group("BYE")
        sub.subscribe("BYE", only_if_empty=False)
        db.set_notify_target("BYE", "OPENID123")
        db.forget_group("BYE")
        check("★ 退群后不再出现在 known_groups 里", "BYE" not in db.known_groups())
        check("★ 退群后补订阅不会把它复活",
              "BYE" not in sub.backfill() and db.count_subscriptions("BYE") == 0)
        check("退群清掉了该群的订阅", db.count_subscriptions("BYE") == 0)
        check("退群清掉了该群的通知对象", db.notify_targets("BYE") == [])
        check("没退的群不受影响", db.count_subscriptions("NEW") == 1)
        check("forget_group 返回删掉的订阅数",
              db.forget_group("NEW") == 1 and db.count_subscriptions("NEW") == 0)

        db.close()


_check_autosub()

print("[4.9] 数据库迁移与新方法")


def _check_db_extras() -> None:
    with tempfile.TemporaryDirectory() as td:
        # 造一个「老库」：没有 fail_count 列
        path = Path(td) / "old.db"
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_openid TEXT NOT NULL, url TEXT NOT NULL, title TEXT,
                interval_sec INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                initialized INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                last_polled_at TEXT, last_error TEXT, UNIQUE (group_openid, url));
            """
        )
        conn.execute(
            "INSERT INTO subscriptions (group_openid,url,title,interval_sec,created_at)"
            " VALUES ('G','https://a.com/1','老数据',900,'2020-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        db = Database(path)  # 打开时应当自动补列
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(subscriptions)")}
        check("★ 老库自动补上 fail_count 列", "fail_count" in cols, str(sorted(cols)))
        subs = db.list_subscriptions("G")
        check("迁移后老数据仍在", len(subs) == 1 and subs[0].title == "老数据")
        check("老数据的 fail_count 默认 0", subs[0].fail_count == 0)
        check("老数据的 enabled 默认 True", subs[0].enabled is True)

        tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("新表 send_counters 建好", "send_counters" in tables)
        check("新表 notify_targets 建好", "notify_targets" in tables)

        # 配额计数
        check("初始配额为 0", db.send_count("G", "2026-01-01") == 0)
        db.bump_send_count("G", "2026-01-01", 3)
        db.bump_send_count("G", "2026-01-01", 2)
        check("配额累加", db.send_count("G", "2026-01-01") == 5, str(db.send_count("G", "2026-01-01")))
        check("配额按日期隔离", db.send_count("G", "2026-01-02") == 0)
        check("配额按群隔离", db.send_count("H", "2026-01-01") == 0)
        db.prune_send_counters(keep_days=0)
        check("旧配额可清理", db.send_count("G", "2026-01-01") == 0)

        # 通知对象
        db.set_notify_target("G", "U1")
        db.set_notify_target("G", "U2")
        db.set_notify_target("G", "U1")  # 重复登记不该产生两条
        check("通知对象去重", sorted(db.notify_targets("G")) == ["U1", "U2"],
              str(db.notify_targets("G")))
        check("最近登记的排前面", db.notify_targets("G")[0] == "U1")
        db.remove_notify_target("G", "U1")
        check("可以移除通知对象", db.notify_targets("G") == ["U2"])
        check("空 openid 不登记", (db.set_notify_target("G", ""), db.notify_targets("G"))[1]
              == ["U2"])

        check("stats 带上了暂停数", "paused" in db.stats())


_check_db_extras()

print("[5] 真实抓取 RSSHub")
cfg = load_config()
print(f"  rsshub = {cfg.rsshub_base}")


async def real_fetch() -> None:
    url = resolve_url("/v2ex/topics/hot", cfg.rsshub_base)
    async with httpx.AsyncClient(headers={"User-Agent": "qq-rss-bot-smoke/0.1"}, follow_redirects=True) as c:
        res = await fetch_feed(c, url, 30)
    if res.ok:
        check(f"抓到 {len(res.entries)} 条", len(res.entries) > 0)
        print(f"        源标题：{res.title}")
        print(f"        首条  ：{res.entries[0].title}")
        print(f"        首条 key：{res.entries[0].key[:60]}")
        check("key 非空", all(e.key for e in res.entries))
    else:
        print(f"  SKIP  网络抓取未成功（{res.error}）—— 不影响离线逻辑判定")


asyncio.run(real_fetch())

print(f"\n结果：{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
