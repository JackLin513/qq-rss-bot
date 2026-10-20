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
    import dataclasses  # noqa: PLC0415 —— 这个文件按节就近 import

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

        with_img = E("k1", "标题1", "https://a.com/1", None, "", ("https://a.com/1.jpg",))
        no_img = E("k2", "标题2", "https://a.com/2", None, "", ())

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

        # --- ★ 多图：一条资讯 3 张图 → 连发 3 条 ---
        multi = E("k3", "标题3", "https://a.com/3", None, "",
                  ("https://a.com/3a.jpg", "https://a.com/3b.jpg", "https://a.com/3c.jpg"))
        many = Fake()
        await FeedPoller(cfg_stub(), db, None, many)._push_entries(sub, "源名", [multi])
        check("★ RSS 多图连发 3 条", len(many.rich) == 3, str(many.rich))
        check("第一条带正文", "标题3" in many.rich[0][0], str(many.rich[0]))
        check("★ 后续条是占位文案", many.rich[1][0] == "（图 2/3）", str(many.rich[1]))
        check("★ 图片顺序不乱",
              [r[1] for r in many.rich] == ["https://a.com/3a.jpg",
                                            "https://a.com/3b.jpg",
                                            "https://a.com/3c.jpg"], str(many.rich))
        check("★ 多图成功时一条纯文字都不发", many.text == [], str(many.text))

        # 开关关掉 → 回到只发第一张
        single = Fake()
        await FeedPoller(dataclasses.replace(cfg_stub(), rss_multi_image=False),
                         db, None, single)._push_entries(sub, "源名", [multi])
        check("★ RSS_MULTI_IMAGE=false 时只发 1 条", len(single.rich) == 1, str(single.rich))
        check("关掉时发的是第一张",
              single.rich and single.rich[0][1] == "https://a.com/3a.jpg", str(single.rich))
        db.close()


async def _check_send_entry_images() -> None:
    """`send_entry_images` 的失败语义 —— 中间某张挂了不能拖垮整条。

    这条最容易被写反：把**后续**那张的异常也抛出去，调用方就会以为整条没发成，
    退回纯文字再发一遍 —— 正文已经发过了，用户看到的是**重复刷屏**。

    失败注入按 **URL** 而不是「第几次」：后者会被重试绕过，测不出真实语义。
    """
    import qq_rss_bot.scheduler as sched
    from qq_rss_bot.scheduler import send_entry_images

    saved_gap = sched.IMAGE_RETRY_GAP
    sched.IMAGE_RETRY_GAP = 0.0  # 测试不该为了等 sleep 花掉好几秒
    try:
        class P:
            def __init__(self, bad: "set[str] | None" = None) -> None:
                self.rich: list[tuple[str, str]] = []
                self._bad = bad or set()

            async def send_rich(self, group_openid: str, content: str, url: str) -> None:
                self.rich.append((content, url))
                if url in self._bad:
                    raise RuntimeError("这张图坏了")

        p = P()
        r = await send_entry_images(p, "G", "正文", ("u1", "u2", "u3"))
        check("★ 3 张图发 3 条", len(p.rich) == 3, str(p.rich))
        check("★ 全成功时返回空列表（调用方据此不告警）", r == [], repr(r))
        check("第一条带正文", p.rich[0][0] == "正文", str(p.rich[0]))
        check("★ 后续条是占位文案", p.rich[1][0] == "（图 2/3）", str(p.rich[1]))
        check("★ 顺序与传入一致", [r[1] for r in p.rich] == ["u1", "u2", "u3"], str(p.rich))

        # 第一条一直挂 → 重试一次后**必须抛出去**，调用方才能退回纯文字
        p = P(bad={"u1"})
        raised = False
        try:
            await send_entry_images(p, "G", "正文", ("u1", "u2"))
        except Exception:  # noqa: BLE001
            raised = True
        check("★ 第一条重试后仍失败要抛出去（调用方据此退回纯文字）", raised, str(p.rich))
        check("★ 第一条确实重试了（共 2 次调用）", len(p.rich) == 2, str(p.rich))

        # 中间那张一直挂 → 重试一次后吞掉，后面的照发
        p = P(bad={"u2"})
        r = await send_entry_images(p, "G", "正文", ("u1", "u2", "u3"))
        check("★ 中间某张失败不中断，后面的照发",
              bool(p.rich) and p.rich[-1][1] == "u3", str(p.rich))
        check("★ 返回的是**没发出去**的那张，不是全部", r == ["u2"], repr(r))
        check("★ 中间那张重试了一次（1 + 2 + 1 = 4 次调用）", len(p.rich) == 4, str(p.rich))

        # 偶发失败（QQ 那种「请稍后重试」）→ 重试就成，不算丢图
        class Flaky:
            def __init__(self) -> None:
                self.rich: list[tuple[str, str]] = []
                self.n = 0

            async def send_rich(self, group_openid: str, content: str, url: str) -> None:
                self.n += 1
                self.rich.append((content, url))
                if self.n == 2:
                    raise RuntimeError("偶发 500")

        f = Flaky()
        await send_entry_images(f, "G", "正文", ("u1", "u2"))
        check("★ 偶发失败重试后成功（函数正常返回，不抛）", len(f.rich) == 3, str(f.rich))
        check("★ 重试发的是同一张图", f.rich[-1][1] == "u2", str(f.rich))
    finally:
        sched.IMAGE_RETRY_GAP = saved_gap


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

print("[4.57] 多图连发的失败语义")
asyncio.run(_check_send_entry_images())

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
from qq_rss_bot.commands import COMMAND_ALIASES as _aliases  # noqa: E402

check("ASCII 宽 = 字符数", setup_panel._width("/sub") == 4)
check("中文按 2 算", setup_panel._width("中文") == 4)
check("全角标点按 2 算", setup_panel._width("，") == 2)
check("中英混排", setup_panel._width("订阅 RSS") == 8)  # 4 + 1 + 3

_problems = setup_panel.validate(setup_panel.PANEL_ITEMS)
check("当前 PANEL_ITEMS 通过官方限制", _problems == [], str(_problems))

# 面板是**平的、不能嵌套**（scripts/probe_panel.py 实测），所以它只当入口：
# 订阅/退订/列表/搜索全在 /rss 的按钮卡片里，面板只放常驻的那几个。
# 上限是 20 个元素，别把它当指令全集用。
check("面板入口就是这几个（顺序即显示顺序）",
      [i["name"] for i in setup_panel.PANEL_ITEMS]
      == ["rss", "new", "抽选", "当落", "help"],
      str([i["name"] for i in setup_panel.PANEL_ITEMS]))
# ★ 面板里的每个名字都必须是**真的能执行的指令**，否则用户点了没反应。
check("★ 面板里的名字在 COMMAND_ALIASES 里都认",
      all(_name in _aliases for _name in (i["name"] for i in setup_panel.PANEL_ITEMS)),
      [i["name"] for i in setup_panel.PANEL_ITEMS
       if i["name"] not in _aliases])
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


def _entry(
    title: str, image: str | None = None, images: "tuple[str, ...] | None" = None
) -> Entry:
    """造一条假 Entry。`images` 优先；只给 `image` 就是单图（老用法）。"""
    covers = images if images is not None else ((image,) if image else ())
    return Entry(key=f"k-{title}", title=title, link=f"https://e.com/{title}",
                 published=None, summary="", images=covers)


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

        # ---- 启动补订阅：给所有已知的群都补 ----
        db.touch_group("EMPTY")          # 一个订阅都没有 → 该补
        db.touch_group("HASONE")         # 订的是**别的**源 → 也该补
        db.add_subscription("HASONE", "https://x.com/1", "别动我", 900)

        added = sub.backfill()
        check("★ 所有已知群都补（含已有别的订阅的）",
              set(added) == {"EMPTY", "HASONE"}, str(added))
        check("零订阅的群被补上", db.count_subscriptions("EMPTY") == 1)
        check("★ 已有别的订阅的群也会补上这个源",
              db.count_subscriptions("HASONE") == 2,
              str([x.title for x in db.list_subscriptions("HASONE")]))
        check("★ 群里原有的订阅一条没动",
              "别动我" in [x.title for x in db.list_subscriptions("HASONE")])
        check("已经订过这个源的群不重复加（幂等）",
              db.count_subscriptions("NEW") == 1 and db.count_subscriptions("NOPUSH") == 1)
        check("补订阅不发消息（启动时刷一堆太吵）", len(pusher.sent) == 1, str(len(pusher.sent)))
        check("补订阅幂等：再跑一次没人需要补", sub.backfill() == [])
        # 保留的旧开关：True = 退回「只补一个订阅都没有的群」。
        # 造一个零订阅的群来验 —— 否则「所有群都已订阅」时两种模式都返回空，
        # 断言必然通过，等于没测。
        db.touch_group("ONLYEMPTY")
        check("★ only_if_empty=True 只补零订阅的群，有订阅的一律跳过",
              sub.backfill(only_if_empty=True) == ["ONLYEMPTY"])
        check("默认（False）则所有群都补，此时已无空缺", sub.backfill() == [])
        check("没配源时补订阅什么也不做",
              AutoSubscriber(cfg_stub(), db, pusher).backfill() == [])

        # ★ 全新部署的空库一个群都不认识 —— 补订阅只能空转。
        #   这正是迁移到新机器时日志写「补了 0 个群」的原因：
        #   群必须先被事件看见一次（有人 @ 机器人），此后重启才会自动补上。
        with tempfile.TemporaryDirectory() as td2:
            fresh = Database(Path(td2) / "fresh.db")
            check("★ 空库（一个已知群都没有）补订阅就是空转",
                  AutoSubscriber(_autocfg(), fresh, _FakePusher()).backfill() == []
                  and fresh.known_groups() == [])
            fresh.close()

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

print("[4.93] 文件日志（容器里唯一的可见通道）")


async def _check_file_logging() -> None:
    """`LOG_FILE` 那条路必须真的能用 —— 容器里它是**唯一**看得到的日志。

    实测踩到过：`docker logs` 要 root，群晖上非 root 连 `/volume1/@docker`
    都进不去，于是 bot 起没起来、为什么连不上网关，全都没法看。
    所以日志要同时落一份到挂载卷。
    """
    import logging
    from logging.handlers import RotatingFileHandler

    from qq_rss_bot.__main__ import setup_logging
    from qq_rss_bot.bot import RssBot

    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "sub" / "bot.log"   # 父目录不存在，要能自动建
        cfg = dataclasses.replace(load_config(), log_file=str(log_path),
                                  db_path=Path(td) / "b.db")

        handler = setup_logging(cfg)
        check("装了文件 handler", isinstance(handler, RotatingFileHandler))
        check("父目录被自动创建", log_path.parent.is_dir())

        # ★ 关键回归：botpy 的 configure_logging 会动 root 的 logger，
        # 文件 handler 必须能扛过 RssBot 的构造 —— 被冲掉的话真机上
        # 日志文件会一直是空的，而恰恰是出问题的时候才需要它。
        bot = RssBot(cfg)
        await bot.rss_http.aclose()
        bot.db.close()
        logging.getLogger("qq_rss_bot").info("MARKER-构造之后")
        for h in logging.getLogger().handlers:
            h.flush()

        text = log_path.read_text(encoding="utf-8")
        check("构造 RssBot 后仍写进文件", "MARKER-构造之后" in text, text[-120:])
        check("root 上留了两个 handler（stdout + 文件）",
              len(logging.getLogger().handlers) == 2,
              str([type(h).__name__ for h in logging.getLogger().handlers]))

        # 没配就不该建 handler
        plain = dataclasses.replace(load_config(), log_file="", db_path=Path(td) / "c.db")
        check("未配 LOG_FILE 时不装 handler", setup_logging(plain) is None)
    # 收尾：把这一轮加的 handler 摘掉，免得影响后面的用例
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(h)
        h.close()


asyncio.run(_check_file_logging())

print("[4.93.1] 轮询心跳（回答「进程还活着吗」）")


async def _check_poller_heartbeat() -> None:
    """INFO 级别下必须能看出「进程还活着、事件循环还在转」。

    实测踩到过：群里发消息没反应，日志静默 22 分钟 —— 而当时唯一的
    周期日志（`本轮待抓取`）是 DEBUG，`LOG_LEVEL=INFO` 下什么都看不到，
    分不清「没收到消息」和「整个进程卡死了」。这两个方向的排查完全不同。
    """
    import logging

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.INFO)
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    grab = _Grab()
    sched_log = logging.getLogger("qq_rss_bot.scheduler")
    sched_log.addHandler(grab)
    prev_level = sched_log.level
    sched_log.setLevel(logging.INFO)
    try:
        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "hb.db")
            # tick 压到 0.01s，让它快速跑够 20 拍以上
            cfg = dataclasses.replace(cfg_stub(), tick_sec=0, db_path=Path(td) / "hb.db")
            poller = FeedPoller(cfg, db, None, _NoopPusher())
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.05)
            poller.stop()
            await asyncio.wait_for(task, timeout=2)
            db.close()

        beats = [ln for ln in grab.lines if "轮询心跳" in ln]
        check("★ 心跳按 INFO 打出来了（INFO 级别下看得见）", len(beats) >= 2, str(grab.lines[:6]))
        check("心跳带上了拍数", beats and "第 1 拍" in beats[0], beats[:1])
        check("启动/停止也都有记录",
              any("轮询启动" in ln for ln in grab.lines) and any("轮询已停止" in ln for ln in grab.lines),
              str(grab.lines))
        # ★ 不能每拍都打 —— 30s 一拍的话那是一天 2880 行。
        # 精确判据：第 1、21、41… 拍各一条，所以条数必然等于 ceil(tick/20)。
        expect = (poller._ticks + 19) // 20
        check("★ 心跳是低频的（20 拍才一条）", len(beats) == expect,
              f"跑了 {poller._ticks} 拍，心跳 {len(beats)} 条，应为 {expect}")
    finally:
        sched_log.removeHandler(grab)
        sched_log.setLevel(prev_level)


class _NoopPusher:
    async def send_text(self, group_openid, content):
        pass

    async def send_rich(self, group_openid, content, image_url):
        pass


asyncio.run(_check_poller_heartbeat())

print("[4.93.2] 启动时报出「跑的是哪份代码」")


def _check_code_identity() -> None:
    """一条日志回答「我改的代码到底生效了没有」。

    实测踩到过：镜像从早上 09:56 就没重建过，源码 13:34 就换过了 —— 连着三次
    「重建」都只是用旧镜像重开容器。日志看起来完全正常（`已上线`、`轮询启动`
    都在），新代码一行都没生效，**而且没有任何地方提示**。这条日志就是那个提示。
    """
    import time as _time

    from qq_rss_bot.__main__ import code_identity

    pkg = Path(__file__).resolve().parents[1] / "src" / "qq_rss_bot"
    ident = code_identity()
    check("报出了包目录", str(pkg) in ident, ident)
    check("报出了改动时间", "最新改动" in ident, ident)
    # ★ 关键是报**最新**的那个 .py，不是固定某个文件 ——
    # 否则改了 bilibili.py 而 bot.py 没动，时间戳就不变，等于没提示
    newest = max(f.stat().st_mtime for f in pkg.glob("*.py"))
    stamp = _time.strftime("%m-%d %H:%M:%S", _time.localtime(newest))
    check("★ 时间取的是包内最新的 mtime", stamp in ident, f"期望 {stamp}，得到 {ident}")


_check_code_identity()

print("[4.94] bilibili 链接预览（全离线）")

import qq_rss_bot.bilibili as _bili  # noqa: E402


# 2026-10-17 抓到的**真实群消息 content**，原样贴进来当回归样本。
# 四种分享形态的差异全在这几段字符串里，改提取逻辑时这几条必须还是绿的。
_MINIAPP = (
    "[卡片消息] 小程序\n"
    "摘要: [QQ小程序]\u201c男人声称机器人杀了他的狗，要求保险公司给他赔偿，"
    "谁知保险员却拿起了刀《机器纪元》\u201d\n"
    "source_logo: https://open.gtimg.cn/open/app_icon/00/95/17/76/100951776_100_m.png?t=1792028716\n"
    "title: \u201c男人声称机器人杀了他的狗，要求保险公司给他赔偿，谁知保险员却拿起了刀《机器纪元》\u201d\n"
    "preview: https://pic.ugcimg.cn/5248e67e658cc9c3c54f616ce575790b/jpg1\n"
    "source: 哔哩哔哩"
)
_MINIAPP_ARK = {
    "ark_name": "小程序",
    "ark_type": "miniapp",
    "prompt": "[QQ小程序]…",
    "fields": {
        "preview": "https://pic.ugcimg.cn/5248e67e658cc9c3c54f616ce575790b/jpg1",
        "source": "哔哩哔哩",
        "title": "\u201c男人声称机器人杀了他的狗，要求保险公司给他赔偿，"
                 "谁知保险员却拿起了刀《机器纪元》\u201d",
    },
}
_SHORT_TEXT = "【\u201c男人声称机器人杀了他的狗…\u201d-哔哩哔哩】 https://b23.tv/uyKBSFP"
_H5_CARD = (
    "[卡片消息] 图文H5\n"
    "摘要: [分享]【中字熟肉】气象厅の狠活！10月16日误报9.9级末日大震惊…\n"
    "title: 【中字熟肉】气象厅の狠活！10月16日误报9.9级末日大震惊…\n"
    "desc: 来自哔哩哔哩\n"
    "jump_url: https://b23.tv/e1Xp08V\n"
    "tag: 哔哩哔哩HD"
)
_BV_TEXT = "https://www.bilibili.com/video/BV15eJt6pEam"
_CHITCHAT = "测试123"
# 反向样本：另一家的小程序卡片，长得和形态 1 一模一样，只有 source 不同
_MUSIC_CARD = (
    "[卡片消息] 小程序\n摘要: [QQ小程序]某首歌\ntitle: 某首歌\n"
    "preview: https://pic.ugcimg.cn/abcdef/jpg1\nsource: QQ音乐"
)
_MUSIC_ARK = {"ark_name": "小程序", "fields": {"source": "QQ音乐", "title": "某首歌"}}

# 分享版标题（无「！！！」）和线上真实标题（有，且搜索接口带 <em> 高亮标签）
_TITLE_SHARED = "男人声称机器人杀了他的狗，要求保险公司给他赔偿，谁知保险员却拿起了刀《机器纪元》"
_TITLE_ONLINE = (
    "男人声称机器人杀了他的狗，要求保险公司给他赔偿，谁知保险员却拿起了刀！！！"
    '<em class="keyword">《机器纪元》</em>'
)

print("[4.94.1] 四种分享形态的提取")


def _check_bili_extract() -> None:
    t = _bili.find_target(_SHORT_TEXT)
    check("纯文本里的 b23.tv 短链被抽出", t is not None and t.short == "https://b23.tv/uyKBSFP",
          repr(t))

    # ★ 形态 3 的关键：卡片消息的 content 是平台预渲染的全文，jump_url 就在里面，
    # 根本不用碰 ark_data。这条一红说明提取逻辑退回去挖 ark_data 了。
    t = _bili.find_target(_H5_CARD)
    check("★ 图文H5 卡片从 content 的 jump_url 抽出短链",
          t is not None and t.short == "https://b23.tv/e1Xp08V", repr(t))

    t = _bili.find_target(_BV_TEXT)
    check("纯文本里的 BV 号被抽出", t is not None and t.bvid == "BV15eJt6pEam", repr(t))

    t = _bili.find_target("看看这个 https://www.bilibili.com/video/BV1cku962EVm?p=2&t=30 挺好")
    check("带 ?p=2 等参数的链接也能抽出 BV",
          t is not None and t.bvid == "BV1cku962EVm", repr(t))

    t = _bili.find_target("https://www.bilibili.com/video/av170001")
    check("老式 av 号被抽出", t is not None and t.bvid == "av170001", repr(t))

    # 形态 1：报文里没有任何视频 ID，只能拿标题反查
    t = _bili.find_target(_MINIAPP, _MINIAPP_ARK)
    check("小程序卡片退化成标题反查", t is not None and t.title and not t.bvid and not t.short,
          repr(t))

    check("闲聊消息不触发", _bili.find_target(_CHITCHAT) is None)
    check("空 content 不触发", _bili.find_target("") is None)

    # ★ 反向用例：别家小程序卡片长得一模一样，只有 source 不同。
    # 不筛 source 的话，群里每分享一张卡片都会去搜一次 bilibili。
    check("★ 非 bilibili 的小程序卡片不触发（不误搜）",
          _bili.find_target(_MUSIC_CARD, _MUSIC_ARK) is None)
    # 有标题但完全没有「哔哩哔哩」字样时也不能触发
    check("★ 无 bilibili 来源标记的卡片不触发",
          _bili.find_target("title: 某首歌\nsource: 某音乐", {"fields": {"title": "某首歌"}}) is None)

    # BILI_HINT 只用来决定「认不出链接时要不要记一行日志」，判宽了会刷屏。
    for hit in ("https://b23.tv/x", "www.bilibili.com/video/BV1", "哔哩哔哩 分享", "B23.TV/x"):
        check(f"BILI_HINT 命中 {hit!r}", _bili.BILI_HINT.search(hit) is not None)
    for miss in ("", "今天天气不错", "https://www.youtube.com/watch?v=x", "https://b23.tv"):
        # 最后一个是**故意留的漏网**：没有 token 的裸域名不算分享，
        # 但它确实含 b23.tv，所以这里只断言前三个不命中
        if miss == "https://b23.tv":
            continue
        check(f"BILI_HINT 不命中 {miss!r}", _bili.BILI_HINT.search(miss) is None)


_check_bili_extract()

print("[4.94.2] 标题归一化与数字格式")


def _check_bili_format() -> None:
    a, b = _bili.norm_title(_TITLE_SHARED), _bili.norm_title(_TITLE_ONLINE)
    check("★ 分享版与线上版标题归一化后相等", a == b and len(a) > 20, f"{a!r} vs {b!r}")
    check("归一化剥掉了 HTML 高亮标签", "<em" not in b and "keyword" not in b, repr(b))
    check("归一化留下了标点以外的字", "机器纪元" in a, repr(a))
    check("归一化丢掉了标点", "，" not in a and "《" not in a, repr(a))

    check("播放数：9999 不加单位", _bili.fmt_count(9999) == "9999")
    check("播放数：10000 → 1.0万", _bili.fmt_count(10000) == "1.0万")
    check("播放数：30510 → 3.1万", _bili.fmt_count(30510) == "3.1万")
    # ★ 四舍五入溢出：直接按 1e8 分档的话这里会输出「10000.0万」
    check("★ 播放数：99999999 → 1.0亿（不出现 10000.0万）",
          _bili.fmt_count(99999999) == "1.0亿", _bili.fmt_count(99999999))
    check("播放数：123456789 → 1.2亿", _bili.fmt_count(123456789) == "1.2亿")

    check("时长：59 → 00:59", _bili.fmt_duration(59) == "00:59")
    check("时长：60 → 01:00", _bili.fmt_duration(60) == "01:00")
    check("时长：187 → 03:07", _bili.fmt_duration(187) == "03:07")
    check("时长：3723 → 1:02:03", _bili.fmt_duration(3723) == "1:02:03")


_check_bili_format()

print("[4.94.3] 卡片渲染")


def _check_bili_render() -> None:
    # pic 是 http://，要改成 https 并加上图床缩放后缀
    md = _bili.render({
        "bvid": "BV15eJt6pEam", "title": "标题", "pic": "http://i1.hdslb.com/bfs/archive/x.jpg",
        "owner": {"name": "UP主"}, "duration": 187,
        "stat": {"view": 30510, "danmaku": 62, "reply": 332, "like": 1041},
    })
    check("封面改成 https", "https://i1.hdslb.com" in md and "http://i1" not in md, md)
    check("封面带上缩放后缀", "@480w_270h_1c.jpg" in md, md)
    # ★ QQ markdown 的图片语法尺寸是必填的（官方示例带尺寸）
    check("★ 图片语法带 #宽px #高px", "![封面 #208px #117px](" in md, md)
    check("标题加粗", "**标题**" in md, md)
    check("UP 主和时长同一行", "UP主 · 03:07" in md, md)
    check("四项数据都在", all(k in md for k in ("播放 3.1万", "弹幕 62", "评论 332", "点赞 1041")), md)
    check("带可点的视频链接", "[在 bilibili 打开](https://www.bilibili.com/video/BV15eJt6pEam)" in md, md)

    # ★ 四项数据必须**固定分两行**，不能挤成一行 —— 挤一行的话手机窄屏上由
    # QQ 自己折行，折点不受控（实测「播放…弹幕…评论…点赞」会在中间断开）。
    _STAT_LABELS = ("播放", "弹幕", "评论", "点赞")
    stat_lines = [ln for ln in md.split("\n") if ln.startswith(_STAT_LABELS)]
    check("★ 四项数据固定分成两行（2 + 2）",
          stat_lines == ["播放 3.1万 · 弹幕 62", "评论 332 · 点赞 1041"], repr(stat_lines))
    check("★ 每行最多两项", all(ln.count("·") <= 1 for ln in stat_lines), repr(stat_lines))

    three = _bili.render({"title": "t", "stat": {"view": 1, "danmaku": 2, "reply": 3}})
    check("三项数据 → 两行（2 + 1）",
          [ln for ln in three.split("\n") if ln.startswith(_STAT_LABELS)] == ["播放 1 · 弹幕 2", "评论 3"],
          repr(three))
    one = _bili.render({"title": "t", "stat": {"view": 1}})
    check("一项数据 → 一行",
          [ln for ln in one.split("\n") if ln.startswith(_STAT_LABELS)] == ["播放 1"], repr(one))

    # ★ 实测 100 条热门标题里 7 条含 | [ ] ! —— 这些不能破坏结构
    weird = _bili.render({
        "bvid": "BV1xx411c7mD", "title": "《绝区零》菲欧妮 | 不要*死*挑战 [上]", "pic": "",
        "owner": {"name": "UP"}, "duration": 60, "stat": {"view": 5},
    })
    check("★ 标题里的 * 被剥掉（不然 ** 会被吃掉）", "**《绝区零》菲欧妮 | 不要死挑战 [上]**" in weird,
          weird)
    check("没有封面时不输出图片行", "![" not in weird, weird)

    empty = _bili.render({"bvid": "BV1xx411c7mD"})
    check("字段全缺也不炸，仍有标题占位", "**（无标题）**" in empty, empty)
    check("时长缺省为 00:00", "00:00" in empty, empty)


_check_bili_render()

print("[4.94.4] 解析与反查判据（假 http）")


class _FakeResp:
    def __init__(self, payload=None, headers=None, status=200, content=b""):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        # 和 httpx 一致：**3xx 也算失败**。_expand 曾经在上面踩过 ——
        # 302 是短链的正常形态，调了 raise_for_status 就变成永远解析失败。
        if not 200 <= self.status_code < 300:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._payload


class _FakeHttp:
    """按 URL 关键字路由的假 httpx.AsyncClient，顺便记录每次调用的参数。"""

    def __init__(self, search=None, view=None, location="", status=302):
        self.calls: list[tuple[str, dict]] = []
        self._search, self._view = search, view
        self._location, self._status = location, status

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        if "b23.tv" in url:
            return _FakeResp(headers={"location": self._location}, status=self._status)
        if "/wbi/search" in url:
            return _FakeResp(payload=self._search)
        if url.endswith("/view"):
            return _FakeResp(payload=self._view)
        raise AssertionError("没预期的 URL: %s" % url)


def _search_payload(*titles):
    return {"code": 0, "data": {"result": [
        {"bvid": "BV1" + str(i) * 9, "title": t} for i, t in enumerate(titles)
    ]}}


_VIEW_OK = {"code": 0, "data": {
    "bvid": "BV15eJt6pEam", "title": "T", "pic": "", "owner": {"name": "UP"},
    "duration": 60, "stat": {"view": 1},
}}


async def _check_bili_lookup() -> None:
    # 短链 → 302 → BV → view
    http = _FakeHttp(view=_VIEW_OK,
                     location="https://www.bilibili.com/video/BV15eJt6pEam?buvid=x&p=1")
    data = await _bili.lookup(http, _bili.find_target(_SHORT_TEXT))
    check("短链展开后取到视频信息", data is not None and data["bvid"] == "BV15eJt6pEam", repr(data))
    # ★ 共享客户端默认 follow_redirects=True，跟下去会把整个视频页 HTML 拉下来
    short_calls = [kw for url, kw in http.calls if "b23.tv" in url]
    check("★ 展开短链时按请求关掉了 follow_redirects",
          short_calls and short_calls[0].get("follow_redirects") is False, repr(short_calls))

    # ★ 反查判据：恰好一条才认
    http = _FakeHttp(search=_search_payload(_TITLE_ONLINE), view=_VIEW_OK)
    data = await _bili.lookup(http, _bili.Target(title=_TITLE_SHARED))
    check("★ 标题反查恰好一条命中 → 采纳", data is not None, repr(data))

    # 0 条（风控/空结果/真没有，一律放弃）
    http = _FakeHttp(search=_search_payload("完全不相干的标题"), view=_VIEW_OK)
    data = await _bili.lookup(http, _bili.Target(title=_TITLE_SHARED))
    check("★ 标题反查 0 条命中 → 放弃", data is None, repr(data))
    check("0 条命中时不该再去查 view",
          not any(u.endswith("/view") for u, _ in http.calls), repr(http.calls))

    # ≥2 条（同名视频）→ 宁可不回，也不能回错
    http = _FakeHttp(search=_search_payload(_TITLE_ONLINE, _TITLE_ONLINE), view=_VIEW_OK)
    data = await _bili.lookup(http, _bili.Target(title=_TITLE_SHARED))
    check("★ 标题反查 2 条命中 → 放弃（宁可不回也不回错）", data is None, repr(data))

    # 标题太短直接不查
    http = _FakeHttp(search=_search_payload("测试"), view=_VIEW_OK)
    data = await _bili.lookup(http, _bili.Target(title="测试"))
    check("标题太短时不发反查请求", data is None and not http.calls, repr(http.calls))

    # 反向：view 返回业务错误码（HTTP 200 + code != 0）
    http = _FakeHttp(view={"code": -404, "message": "啥都木有"})
    data = await _bili.lookup(http, _bili.Target(bvid="BV15eJt6pEam"))
    check("view 业务错误码 → 放弃（不是当成成功）", data is None, repr(data))


asyncio.run(_check_bili_lookup())

print("[4.94.5] 非 @ 消息的预览出口（不该发的绝不发）")

from qq_rss_bot.bot import RssBot as _RssBot  # noqa: E402


class _FakeApi:
    def __init__(self):
        self.sent: list[dict] = []

    async def post_group_message(self, **kw):
        self.sent.append(kw)
        return {"code": 0}


class _FakeMsg:
    def __init__(self, content, ark_data=None, group="G", mid="M1"):
        self.content, self.ark_data = content, ark_data
        self.group_openid, self.id = group, mid


class _BotStub:
    """只借用 RssBot 的两个方法，不真造一个 botpy Client（那要事件循环和网络）。"""

    _preview_bilibili = _RssBot._preview_bilibili
    _make_repliers = _RssBot._make_repliers

    def __init__(self, cfg, http, api):
        self.cfg, self.rss_http, self.api = cfg, http, api


async def _check_bili_preview() -> None:
    cfg = load_config()

    # 闲聊 → 什么都不发
    api = _FakeApi()
    bot = _BotStub(cfg, _FakeHttp(), api)
    check("闲聊消息返回「没回」", await bot._preview_bilibili(_FakeMsg(_CHITCHAT)) is False)
    check("★ 闲聊消息一条都没发", api.sent == [], repr(api.sent))

    # 别家卡片 → 什么都不发
    api = _FakeApi()
    bot = _BotStub(cfg, _FakeHttp(), api)
    await bot._preview_bilibili(_FakeMsg(_MUSIC_CARD, _MUSIC_ARK))
    check("★ 非 bilibili 卡片一条都没发", api.sent == [], repr(api.sent))

    # 开关关掉 → 什么都不发
    api = _FakeApi()
    bot = _BotStub(dataclasses.replace(cfg, bilibili_preview=False),
                   _FakeHttp(view=_VIEW_OK), api)
    check("开关关掉时返回「没回」",
          await bot._preview_bilibili(_FakeMsg(_BV_TEXT)) is False)
    check("★ BILIBILI_PREVIEW=false 时一条都没发", api.sent == [], repr(api.sent))

    # 正常路径 → 发一条 markdown 卡片
    api = _FakeApi()
    bot = _BotStub(cfg, _FakeHttp(view=_VIEW_OK), api)
    check("正常路径返回「回了」", await bot._preview_bilibili(_FakeMsg(_BV_TEXT)) is True)
    check("恰好发了一条", len(api.sent) == 1, repr(api.sent))
    sent = api.sent[0]
    check("msg_type=2（markdown）", sent.get("msg_type") == 2, repr(sent))
    check("带 msg_id（被动回复，不占主动配额）", sent.get("msg_id") == "M1", repr(sent))
    check("msg_seq=1", sent.get("msg_seq") == 1, repr(sent))
    check("markdown 是外层 content 包裹", "content" in (sent.get("markdown") or {}), repr(sent))
    # ★ 没有按钮的卡片必须整个不传 keyboard，不能塞空的 rows
    check("★ 不传 keyboard（不是传空 rows）", "keyboard" not in sent, repr(sent.keys()))

    # 解析不出来 → 一条都不发
    api = _FakeApi()
    bot = _BotStub(cfg, _FakeHttp(view={"code": -404, "message": "x"}), api)
    check("解析失败返回「没回」", await bot._preview_bilibili(_FakeMsg(_BV_TEXT)) is False)
    check("★ 解析失败时一条都没发", api.sent == [], repr(api.sent))

    # ★ 异常要被吃掉：发消息抛异常也不能冒到 botpy 的 _run_event
    class _BoomApi:
        async def post_group_message(self, **kw):
            raise RuntimeError("boom")

    bot = _BotStub(cfg, _FakeHttp(view=_VIEW_OK), _BoomApi())
    check("★ 发送异常被吞掉并返回「没回」",
          await bot._preview_bilibili(_FakeMsg(_BV_TEXT)) is False)


asyncio.run(_check_bili_preview())

print("[4.94.6] compat 补的两个槽位")


def _check_bili_slots() -> None:
    from qq_rss_bot.compat import GroupMessageWithRole

    msg = GroupMessageWithRole(
        None, "E1",
        {"id": "M1", "content": _H5_CARD, "group_openid": "G",
         "message_type": 3, "ark_data": {"ark_type": "tuwen", "fields": {"jump_url": "x"}},
         "author": {"member_role": "owner"}},
    )
    check("message_type 取得到", msg.message_type == 3, repr(msg.message_type))
    check("ark_data 取得到", isinstance(msg.ark_data, dict) and msg.ark_data.get("ark_type") == "tuwen",
          repr(msg.ark_data))
    check("member_role 没被挤掉", msg.member_role == "owner", repr(msg.member_role))

    plain = GroupMessageWithRole(None, "E2", {"id": "M2", "content": "hi", "group_openid": "G"})
    check("纯文本消息的 message_type 是 None", plain.message_type is None, repr(plain.message_type))
    check("纯文本消息的 ark_data 是 None", plain.ark_data is None, repr(plain.ark_data))


_check_bili_slots()

print("[4.94.7] 启动自检的探测（必须永远不抛）")


class _BoomHttp:
    """连不上时的假客户端：get 直接抛。"""

    def __init__(self, exc):
        self._exc = exc

    async def get(self, url, **kw):
        raise self._exc


async def _check_bili_probe() -> None:
    # 通 —— 拿得到 code=0
    msg = await _bili.probe(_FakeHttp(view=_VIEW_OK))
    check("探测：正常时给 ✅", msg.startswith("✅"), msg)
    check("探测：正常时带上样例 BV", "BV15eJt6pEam" in msg, msg)

    # ★ 视频被删：**网络是通的**，不能报成「连不上」—— 这两种情况的排查方向
    # 完全相反，混在一起会把人带沟里
    msg = await _bili.probe(_FakeHttp(view={"code": -404, "message": "啥都木有"}))
    check("探测：接口非 0 时报「网络通」而不是连不上", msg.startswith("⚠️") and "网络通" in msg, msg)
    check("探测：接口非 0 时带上业务码", "-404" in msg, msg)

    # ★★ 这个功能的意义就在这条：**探测本身绝不能抛**。
    # 它在 on_ready 里跑，抛出去会把启动流程带崩 —— 那还不如没有。
    for exc in (
        httpx.ConnectError("dns 挂了"),
        httpx.ConnectTimeout("超时"),
        httpx.ProxyError("代理不通"),
        httpx.TransportError("SSL: self-signed certificate in certificate chain"),
    ):
        msg = await _bili.probe(_BoomHttp(exc))
        check(f"探测：{type(exc).__name__} 不抛且报 ❌", msg.startswith("❌"), msg)
    check("探测：异常信息原样带出来（排查全靠它）",
          "self-signed" in await _bili.probe(_BoomHttp(httpx.TransportError("self-signed"))))

    # 响应体不是 JSON 也不能抛（比如被劫持成一个 HTML 登录页）
    class _NotJson(_FakeHttp):
        async def get(self, url, **kw):
            return _FakeResp(payload=None)

    msg = await _bili.probe(_NotJson(view=None))
    check("探测：响应体不是 JSON 时也不抛", msg.startswith("⚠️"), msg)


asyncio.run(_check_bili_probe())

print("[4.95] YouTube / X 链接预览（全离线）")

import base64 as _b64  # noqa: E402

from qq_rss_bot import preview as _pv  # noqa: E402

# 假的 youtubei 响应。**字段名照着真实响应抄** —— 抄错了测试就成了摆设。
_YTI_OK = {
    "playabilityStatus": {"status": "OK"},
    "videoDetails": {
        "title": "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)",
        "author": "Rick Astley",
        "lengthSeconds": "213",
        "viewCount": "1827523566",
    },
}
# 假的 fxtwitter 响应（这条是带图的）
_FX_OK = {
    "code": 200,
    "tweet": {
        "url": "https://x.com/BarackObama/status/896523232098078720",
        "text": "No one is born hating another person because of the color of his skin",
        "likes": 3400843,
        "retweets": 1216441,
        "replies": 58226,
        "author": {"name": "Barack Obama", "screen_name": "BarackObama"},
        "media": {
            "photos": [
                {
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/DHEXH7RV0AAUwKj.jpg?name=orig",
                }
            ]
        },
    },
}
# 同一条的「无图」版本
_FX_NO_PHOTO = {**_FX_OK, "tweet": {**_FX_OK["tweet"], "media": None}}

# ★ **纯图推**（没有正文）。抄的是 2026-10-20 群里那条 @latiechi（9913 赞）。
#   ⚠️ 新版 fxtwitter 的 `raw_text` 是**对象**不是字符串，而纯图推的 `text`
#   是**空串** —— 老代码的回退 `str(text or raw_text or "")` 会把整个 dict
#   `str()` 出来，群里收到的是 Python 的 repr。见 `preview._tweet_text`。
_FX_NO_TEXT = {
    **_FX_OK,
    "tweet": {
        **_FX_OK["tweet"],
        "text": "",
        "raw_text": {
            "text": "https://t.co/61cVhplQpn",
            "display_text_range": [0, 0],
            "facets": [
                {
                    "type": "media",
                    "indices": [0, 23],
                    "id": "2112154440102981632",
                    "display": "pic.x.com/61cVhplQpn",
                    "original": "https://t.co/61cVhplQpn",
                    "replacement": "https://x.com/latiechi/status/2112154450785886658/photo/1",
                }
            ],
        },
    },
}

# 三张配图的推文 —— 多图那条路的主力 fixture
_FX_MULTI = {
    **_FX_OK,
    "tweet": {
        **_FX_OK["tweet"],
        "media": {
            "photos": [
                {"url": "https://pbs.twimg.com/media/A.jpg?name=orig"},
                {"url": "https://pbs.twimg.com/media/B.jpg?name=orig"},
                {"url": "https://pbs.twimg.com/media/C.jpg?name=orig"},
            ]
        },
    },
}


class _FakeMediaHttp:
    """YouTube / X 的假客户端：按 URL 路由，图片给字节。"""

    def __init__(self, yti=None, fx=None, oembed=None, image=b"\xff\xd8fake", image_status=200):
        self.calls: list[tuple[str, str]] = []
        self._yti, self._fx, self._oembed = yti, fx, oembed
        self._image, self._image_status = image, image_status

    async def post(self, url, **kw):
        self.calls.append(("post", url))
        if "youtubei" in url:
            return _FakeResp(payload=self._yti)
        raise AssertionError("没预期的 POST: %s" % url)

    async def get(self, url, **kw):
        self.calls.append(("get", url))
        if "youtube.com/oembed" in url:
            return _FakeResp(payload=self._oembed)
        if "fxtwitter" in url:
            return _FakeResp(payload=self._fx)
        if "publish.twitter.com" in url:
            return _FakeResp(payload=self._oembed)
        # 图片下载：ytimg（YouTube 封面）/ twimg（推文配图）
        if "ytimg.com" in url or "twimg.com" in url:
            return _FakeResp(content=self._image, status=self._image_status)
        raise AssertionError("没预期的 GET: %s" % url)


class _MediaApi:
    """假的 QQ 接口，把「上传」和「发消息」分开记 —— 两者是两条独立的路。

    `fail_after` / `msg_fail_after` 用来模拟「第 N 次之后开始失败」，
    多图的部分失败全靠它。
    """

    def __init__(self, upload_ok=True, upload_raises=False,
                 fail_after=None, msg_fail_after=None):
        self.sent: list[dict] = []
        self.uploaded: list[dict] = []
        self._ok, self._raises = upload_ok, upload_raises
        self._fail_after, self._msg_fail_after = fail_after, msg_fail_after
        self._rich_sent = 0

    async def post_group_message(self, **kw):
        self.sent.append(kw)
        # `msg_fail_after` **只数富媒体（msg_type=7）**。按「所有消息」数的话，
        # 兜底的 markdown 卡片会跟着一起挂，就测不出「退回卡片」那条路了。
        if kw.get("msg_type") == 7:
            self._rich_sent += 1
            if self._msg_fail_after is not None and self._rich_sent > self._msg_fail_after:
                raise RuntimeError("send boom")
        return {"code": 0}

    async def post_group_file(self, **kw):
        self.uploaded.append(kw)
        if self._raises:
            raise RuntimeError("upload boom")
        if self._fail_after is not None and len(self.uploaded) > self._fail_after:
            return {}  # 没拿到 file_info = 这张传不上去
        return {"file_info": "FI"} if self._ok else {}


class _SpyNotifier:
    """记账用的 notifier 替身 —— `notify` 只把 (群, 文案, key) 收进列表。

    **不实现冷却**：冷却逻辑属于 `C2CNotifier` 自己，这里要断言的是
    「该不该报、报了什么、key 用的是什么」，把冷却混进来只会让用例难读。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def notify(self, group_openid: str, text: str, *, key: str = "") -> bool:
        self.calls.append((group_openid, text, key))
        return True


class _MediaBotStub:
    """只借 RssBot 的预览出口，不真造 botpy Client（那要事件循环和网络）。"""

    _preview_media = _RssBot._preview_media
    _upload_image = _RssBot._upload_image
    _make_repliers = _RssBot._make_repliers

    def __init__(self, cfg, media_http, api, notifier=None):
        self.cfg, self.media_http, self.api = cfg, media_http, api
        # 配图失败要私聊告警 —— 这里给个真的 `C2CNotifier` 替身，
        # 否则「失败了到底有没有报」这件事测不了。
        self.notifier = notifier or _SpyNotifier()


_YT_TEXT = "看看这个 https://www.youtube.com/watch?v=dQw4w9WgXcQ"
_X_TEXT = "https://x.com/BarackObama/status/896523232098078720"


async def _check_media_preview() -> None:
    import qq_rss_bot.scheduler as _sched

    saved_gap = _sched.IMAGE_RETRY_GAP
    _sched.IMAGE_RETRY_GAP = 0.0
    try:
        await _check_media_preview_inner()
    finally:
        _sched.IMAGE_RETRY_GAP = saved_gap


async def _check_media_preview_inner() -> None:
    cfg = load_config()

    # --- 不该管的消息：一条都不发 ---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(), api)
    check("闲聊返回「没回」", await bot._preview_media(_FakeMsg("测试123")) is False)
    check("★ 闲聊一条都没发", api.sent == [] and api.uploaded == [], repr(api.sent))

    # bilibili 链接必须**留给 bilibili 那条路**，不能被这里截胡
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(), api)
    check("★ bilibili 链接不被 YouTube/X 认领",
          await bot._preview_media(_FakeMsg("https://www.bilibili.com/video/BV15eJt6pEam")) is False)
    check("★ 认领失败时也一条都没发", api.sent == [], repr(api.sent))

    # 开关关掉
    api = _MediaApi()
    bot = _MediaBotStub(dataclasses.replace(cfg, youtube_preview=False), _FakeMediaHttp(yti=_YTI_OK), api)
    check("YOUTUBE_PREVIEW=false 时返回「没回」",
          await bot._preview_media(_FakeMsg(_YT_TEXT)) is False)
    check("★ 开关关掉时一条都没发", api.sent == [] and api.uploaded == [], repr(api.sent))

    # --- YouTube 正常路径：上传字节 + 富媒体 ---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(yti=_YTI_OK, image=b"\xff\xd8yt"), api)
    check("YouTube 返回「回了」", await bot._preview_media(_FakeMsg(_YT_TEXT)) is True)
    check("恰好上传一次", len(api.uploaded) == 1, repr(api.uploaded))
    up = api.uploaded[0]
    check("file_type=1（图片）", up.get("file_type") == 1, repr(up))
    check("srv_send_msg=False（只上传，不单独发一条）", up.get("srv_send_msg") is False, repr(up))
    check("★ 走 file_data 而不是 url", "file_data" in up and "url" not in up, repr(list(up)))
    check("★ file_data 是图片的 base64", _b64.b64decode(up["file_data"]) == b"\xff\xd8yt")
    check("恰好发一条消息", len(api.sent) == 1, repr(api.sent))
    sent = api.sent[0]
    check("★ msg_type=7（富媒体）", sent.get("msg_type") == 7, repr(sent))
    check("★ 带 media.file_info", (sent.get("media") or {}).get("file_info") == "FI", repr(sent))
    check("被动回复：带 msg_id", sent.get("msg_id") == "M1", repr(sent))
    check("被动回复：msg_seq=1", sent.get("msg_seq") == 1, repr(sent))
    body = sent.get("content") or ""
    check("正文含标题", "Rick Astley" in body, repr(body))
    check("★ 时长和播放量在同一行", "03:33 · 播放 18.3亿" in body, repr(body))
    check("正文含原链接", "youtube.com/watch?v=dQw4w9WgXcQ" in body, repr(body))
    check("富媒体消息不带 keyboard", "keyboard" not in sent, repr(list(sent)))

    # --- X：有配图 → 富媒体；且配图要换成 name=small ---
    api = _MediaApi()
    http = _FakeMediaHttp(fx=_FX_OK, image=b"\xff\xd8tw")
    bot = _MediaBotStub(cfg, http, api)
    check("X 返回「回了」", await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    fetched = [u for kind, u in http.calls if kind == "get" and "twimg.com" in u]
    check("★ 下载的是原图 name=orig", fetched and "name=orig" in fetched[0], repr(fetched))
    sent = api.sent[0]
    check("X 也走 msg_type=7", sent.get("msg_type") == 7, repr(sent))
    body = sent.get("content") or ""
    check("正文含作者", "Barack Obama (@BarackObama)" in body, repr(body))
    check("★ 互动数每行最多两项", "点赞 340.1万 · 转发 121.6万" in body, repr(body))

    # --- ★ 多图：一条推文配 3 张图 ---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_MULTI, image=b"\xff\xd8m"), api)
    check("多图推文返回「回了」", await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    check("★ 3 张图各上传一次", len(api.uploaded) == 3, repr(len(api.uploaded)))
    check("★ 3 张图发 3 条消息", len(api.sent) == 3, repr(len(api.sent)))
    check("★ 每条都是 msg_type=7",
          all(m.get("msg_type") == 7 for m in api.sent),
          repr([m.get("msg_type") for m in api.sent]))
    check("★ msg_seq 递增（同一个 msg_id 下撞号会被静默丢弃）",
          [m.get("msg_seq") for m in api.sent] == [1, 2, 3],
          repr([m.get("msg_seq") for m in api.sent]))
    check("第一条带完整正文",
          "Barack Obama" in (api.sent[0].get("content") or ""),
          repr(api.sent[0].get("content")))
    check("★ 后续条只带占位文案",
          api.sent[1].get("content") == "（图 2/3）", repr(api.sent[1].get("content")))
    check("★ 每条各带一个 file_info",
          all((m.get("media") or {}).get("file_info") for m in api.sent), repr(api.sent))

    # --- 开关关掉 → 回到单图（旧行为，必须一模一样）---
    api = _MediaApi()
    bot = _MediaBotStub(dataclasses.replace(cfg, preview_multi_image=False),
                        _FakeMediaHttp(fx=_FX_MULTI, image=b"\xff\xd8m"), api)
    await bot._preview_media(_FakeMsg(_X_TEXT))
    check("★ PREVIEW_MULTI_IMAGE=false 时只发 1 条", len(api.sent) == 1, repr(len(api.sent)))
    check("★ 关掉时只上传 1 次（不多下多传）", len(api.uploaded) == 1, repr(len(api.uploaded)))

    # --- 中间某张传不上去 → 少那一张，其余照发 ---
    api = _MediaApi(fail_after=1)
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_MULTI, image=b"\xff\xd8m"), api)
    check("第 2、3 张上传失败仍返回「回了」",
          await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    check("★ 传不上去的那几张不发，成功的照发", len(api.sent) == 1, repr(len(api.sent)))

    # --- 第 2 条发送失败 → 不能把整条拖回去 ---
    api = _MediaApi(msg_fail_after=1)
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_MULTI, image=b"\xff\xd8m"), api)
    check("★ 后续条发送失败时仍算「回了」（不误报成预览失败）",
          await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    check("★ 第 2 条挂了以后第 3 条照样尝试发",
          any("图 3/3" in (m.get("content") or "") for m in api.sent),
          repr([m.get("content") for m in api.sent]))
    check("★ 后续条失败时**不**退回 markdown（否则正文会重复刷屏）",
          all(m.get("msg_type") == 7 for m in api.sent),
          repr([m.get("msg_type") for m in api.sent]))

    # 第一条（带正文那条）挂了 → 退回 markdown 卡片，不能让分享变成「什么都没回」
    api = _MediaApi(msg_fail_after=0)
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_MULTI, image=b"\xff\xd8m"), api)
    check("★ 第一条发不出去时退回 markdown 卡片",
          await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    check("★ 退回的那条是 msg_type=2（markdown）",
          api.sent and api.sent[-1].get("msg_type") == 2,
          repr([m.get("msg_type") for m in api.sent]))

    # --- ★ 成功日志必须带封面尺寸和档位 ---
    # 这条钉的是**可观测性**，不是功能：推文的图分 orig/large/medium/small
    # 四档（实测 943KB / 434KB / 180KB / 70KB），而「已回图文卡片」四种情况
    # 长得一模一样。日志里不写尺寸的话，「发出去的到底是不是原图」在事后
    # 无法回答 —— 这个已经真发生过一次，只能靠让用户重发来补证据。
    import logging  # noqa: PLC0415 —— 这个文件按节就近 import

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.INFO)
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    grab = _Grab()
    blog = logging.getLogger("qq_rss_bot.bot")
    old_level = blog.level
    blog.addHandler(grab)
    blog.setLevel(logging.INFO)
    try:
        api = _MediaApi()
        bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_OK, image=b"\xff\xd8tw"), api)
        await bot._preview_media(_FakeMsg(_X_TEXT))
    finally:
        blog.removeHandler(grab)
        blog.setLevel(old_level)

    done = [ln for ln in grab.lines if "已回" in ln]
    check("★ 成功日志带配图张数和字节数",
          bool(done) and "配图 1 张" in done[-1] and "KB" in done[-1], repr(done[-1:]))
    check("★ 成功日志带配图 URL（档位写在 ?name= 里）",
          bool(done) and "name=orig" in done[-1], repr(done[-1:]))

    # --- X：没配图 → 退回 markdown 卡片（不传 media）---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_NO_PHOTO), api)
    check("无图推文也返回「回了」", await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    check("★ 无图时不上传", api.uploaded == [], repr(api.uploaded))
    sent = api.sent[0]
    check("★ 无图时 msg_type=2（markdown）", sent.get("msg_type") == 2, repr(sent))
    check("markdown 是外层 content 包裹", "content" in (sent.get("markdown") or {}), repr(sent))
    check("markdown 里没有 media", "media" not in sent, repr(list(sent)))

    # --- ★★ 纯图推（没有正文）：绝不能把 raw_text 那个 dict 打出来 ---
    # 实测 2026-10-20 群里 @latiechi 那条（9913 赞、一张图）就是被这个坑到的：
    # 卡片第一行是
    #   {'text': 'https://t.co/61cVhplQpn', 'display_text_range': [0, 0], 'facets': [...]}
    check("★ 纯图推：_tweet_text 拿不到正文就返回空串，**不是 dict 的 repr**",
          _pv._tweet_text(_FX_NO_TEXT["tweet"]) == "",
          repr(_pv._tweet_text(_FX_NO_TEXT["tweet"])))
    check("★ 正文非空时照常取 text（回退根本不该走到）",
          _pv._tweet_text(_FX_OK["tweet"]).startswith("No one is born"))
    check("老版 API 的 raw_text 是字符串，也要吃",
          _pv._tweet_text({"raw_text": "老版正文"}) == "老版正文")
    check("text 缺失但 raw_text 是对象时，取里面的 text",
          _pv._tweet_text({"raw_text": {"text": "对象里的正文"}}) == "对象里的正文")
    check("★ 整段只有 t.co 链接 → 当没有正文（纯图推的 raw_text 就是它）",
          _pv._tweet_text({"raw_text": {"text": "https://t.co/abc123"}}) == "")
    check("★ raw_text 是 null / 缺字段 / 数字 都不炸",
          [_pv._tweet_text({"raw_text": v}) for v in (None, 42, [], {})] == ["", "", "", ""])

    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(fx=_FX_NO_TEXT, image=b"\xff\xd8t"), api)
    check("纯图推也返回「回了」（图照发）", await bot._preview_media(_FakeMsg(_X_TEXT)) is True)
    body = (api.sent[0].get("content") or "")
    check("★ 卡片正文里没有 `{`（dict 的痕迹）", "{" not in body, repr(body))
    check("★ 也没有 `raw_text` / `display_text_range` / `facets` 这些字段名",
          not any(k in body for k in ("raw_text", "display_text_range", "facets")), repr(body))
    check("★ 正文退化成「作者 + 数据 + 链接」，图还在",
          "Barack Obama (@BarackObama)" in body and "点赞" in body
          and body.rstrip().endswith("status/896523232098078720"),
          repr(body))
    check("★ 纯图推的图还是照传（图走 media，和正文无关）", len(api.uploaded) == 1)

    # --- QQ 的 5M 直传上限 ---
    # 官方对 `file_data` 的原话是「直接上传二进制有大小限制，需要在 5M 以内」，
    # 而**这个 5M 限的是 base64 之后那一串**，不是原始字节。所以
    # 「原始图能有多大」= 5MiB × 3/4，这条关系必须钉死：它一旦被违反，日常
    # 撞到的是平台拒绝，而日志里看着像「上传失败」，排查方向就全错了。
    b64_len = len(_b64.b64encode(b"\x00" * _pv.MAX_IMAGE_BYTES))
    check(
        "★ MAX_IMAGE_BYTES 的图 base64 后不超 5M 直传上限",
        b64_len <= _pv.MAX_FILE_DATA_BYTES,
        f"{_pv.MAX_IMAGE_BYTES} 字节 → base64 {b64_len}，上限 {_pv.MAX_FILE_DATA_BYTES}",
    )

    # 闸门本身：直接喂超限字节，必须在上传**之前**就放弃
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(), api)
    oversize = b"\xff\xd8" + b"\x00" * (4 * 1024 * 1024)  # base64 后 ~5.6MB
    check("★ base64 超 5M 时 _upload_image 返回 None",
          await bot._upload_image("G1", oversize) is None)
    check("★ 超限时根本没发上传请求", api.uploaded == [], repr(api.uploaded))

    # 边界内侧必须能过：3MiB → base64 4MiB
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(), api)
    check("3MiB 的图能过闸",
          await bot._upload_image("G1", b"\xff\xd8" + b"\x00" * (3 * 1024 * 1024)) == "FI")
    check("过闸时确实发了上传请求", len(api.uploaded) == 1, repr(api.uploaded))

    # --- 上传失败 / 上传抛异常 → 都要降级，但**文字必须照发** ---
    for label, api in (("上传没拿到 file_info", _MediaApi(upload_ok=False)),
                       ("上传抛异常", _MediaApi(upload_raises=True))):
        bot = _MediaBotStub(cfg, _FakeMediaHttp(yti=_YTI_OK), api)
        check(f"{label} 时仍返回「回了」",
              await bot._preview_media(_FakeMsg(_YT_TEXT)) is True)
        check(f"★ {label} 时降级成 markdown", api.sent and api.sent[0].get("msg_type") == 2,
              repr(api.sent))

    # --- 图片下载失败（404）→ 同样降级，不发裂图 ---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(yti=_YTI_OK, image_status=404), api)
    check("封面 404 时仍返回「回了」", await bot._preview_media(_FakeMsg(_YT_TEXT)) is True)
    check("★ 封面下载失败时不上传", api.uploaded == [], repr(api.uploaded))
    check("★ 封面下载失败时降级成 markdown", api.sent[0].get("msg_type") == 2, repr(api.sent))

    # --- 解析失败 → 一条都不发 ---
    api = _MediaApi()
    bot = _MediaBotStub(cfg, _FakeMediaHttp(yti={}, oembed=None), api)
    check("YouTube 解析失败返回「没回」",
          await bot._preview_media(_FakeMsg(_YT_TEXT)) is False)
    check("★ 解析失败时一条都没发", api.sent == [] and api.uploaded == [], repr(api.sent))

    # --- ★ 异常要被吃掉：不能冒到 botpy 的 _run_event ---
    class _BoomApi:
        async def post_group_message(self, **kw):
            raise RuntimeError("boom")

        async def post_group_file(self, **kw):
            return {"file_info": "FI"}

    bot = _MediaBotStub(cfg, _FakeMediaHttp(yti=_YTI_OK), _BoomApi())
    check("★ 发送异常被吞掉并返回「没回」",
          await bot._preview_media(_FakeMsg(_YT_TEXT)) is False)


asyncio.run(_check_media_preview())


print("[4.58] 配图失败的私聊告警")


async def _check_image_failure_notify() -> None:
    """配图没发出去 → 私聊报给管理员。

    **为什么要报**：配图失败在群里是**看不见**的。读者只会觉得「这条怎么
    少了两张图」，很容易当成「本来就没图」—— 用户就是这么发现第 3 张一直
    没发出去的（翻了 NAS 日志才知道）。
    """
    import qq_rss_bot.scheduler as sched
    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.scheduler import IMAGE_FAILURE_KEY_PREFIX, FeedPoller

    saved_gap = sched.IMAGE_RETRY_GAP
    sched.IMAGE_RETRY_GAP = 0.0
    try:
        class Fake:
            """按 URL 注入失败 —— 按调用次数注入会被重试绕过，测不出真实语义。"""

            def __init__(self, bad: "set[str] | None" = None) -> None:
                self.text: list[str] = []
                self.rich: list[tuple[str, str]] = []
                self._bad = bad or set()

            async def send_text(self, group_openid: str, content: str) -> None:
                self.text.append(content)

            async def send_rich(self, group_openid: str, content: str, url: str) -> None:
                if url in self._bad:
                    raise RuntimeError("这张图坏了")
                self.rich.append((content, url))

        with tempfile.TemporaryDirectory() as td:
            db = Database(Path(td) / "t.db")
            sid, _ = db.add_subscription("G", "https://a.com/f", "源名", 60)
            db.mark_polled(sid, title="源名", mark_initialized=True)
            sub = db.list_subscriptions("G")[0]

            multi = E("k1", "标题1", "https://a.com/1", None, "",
                      ("https://a.com/1a.jpg", "https://a.com/1b.jpg",
                       "https://a.com/1c.jpg"))

            # --- 全成功：一条私聊都不该有 ---
            spy = _SpyNotifier()
            await FeedPoller(cfg_stub(), db, None, Fake(), notifier=spy)._push_entries(
                sub, "源名", [multi]
            )
            check("★ 配图全成功时不私聊", spy.calls == [], repr(spy.calls))

            # --- 中间那张挂：正文和另两张照发，但要报 ---
            spy = _SpyNotifier()
            await FeedPoller(
                cfg_stub(), db, None, Fake(bad={"https://a.com/1b.jpg"}), notifier=spy
            )._push_entries(sub, "源名", [multi])
            check("★ 有图没发出去时私聊一次", len(spy.calls) == 1, repr(spy.calls))
            if spy.calls:
                group, text, key = spy.calls[0]
                check("发给的是出问题的那个群", group == "G", group)
                check("★ 冷却键按群走，不带条目（防 DM 风暴）",
                      key == f"{IMAGE_FAILURE_KEY_PREFIX}G", key)
                check("文案说了几张没出去", "1/3" in text, text)
                check("文案带上了失败的图片 URL", "1b.jpg" in text, text)
                check("文案带上了源和条目", "源名" in text and "标题1" in text, text)
                check("★ 部分失败时说的是「几张」不是「整条」",
                      "1/3 张没发出去" in text, text)

            # --- 第一条挂：整条退回纯文字，图**一张都没发出去** ---
            spy = _SpyNotifier()
            fake = Fake(bad={"https://a.com/1a.jpg"})
            await FeedPoller(cfg_stub(), db, None, fake, notifier=spy)._push_entries(
                sub, "源名", [multi]
            )
            check("第一条挂 → 退回纯文字", len(fake.text) == 1 and not fake.rich,
                  f"{fake.text} / {fake.rich}")
            check("★ 第一条挂时一条都不发（退回纯文字，不重复刷屏）",
                  fake.rich == [], repr(fake.rich))
            check("★ 退回纯文字时也要报（首图就挂 = 这类图一张都发不出去）",
                  len(spy.calls) == 1, repr(spy.calls))
            if spy.calls:
                _g, _t, _k = spy.calls[0]
                check("★ 退回纯文字时的冷却键和部分失败共用", _k == f"{IMAGE_FAILURE_KEY_PREFIX}G", _k)
                check("★ 说清楚是「整条退回纯文字」而不是「少了一张」",
                      "退回纯文字" in _t and "1/3" not in _t, _t)
                check("★ 只报首图（后面几张根本没试过，算进去就是编数字）",
                      _t.count("·") == 1, _t)
            db.close()

        # --- /new 那条路也要报 ---
        from qq_rss_bot.commands import CommandHandler

        handler = CommandHandler(
            cfg_stub(), None, None, None, notifier=_SpyNotifier()
        )
        check("★ CommandHandler 拿得到 notifier", handler.notifier is not None)
    finally:
        sched.IMAGE_RETRY_GAP = saved_gap


asyncio.run(_check_image_failure_notify())


print("[4.59] 推送文案的摘要行")


def _check_format_entry_summary() -> None:
    """`format_entry` 必须把 `Entry.summary` 发出去。

    回归钉子：这个字段从 2026-08 抓到 2026-10 一直**没人用** —— 抓下来、
    洗了标签、存进对象，然后一行都没发。群里看到的是光秃秃的三行，
    用户直接问「是不是 RSS 省略了」。不是省略，是漏了。
    """
    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.scheduler import MAX_SUMMARY_CHARS, format_entry

    e = E("k", "标题", "https://a.com/1", None, "这是摘要正文", ())
    text = format_entry("源名", "https://a.com/f", e)
    check("★ 摘要出现在推送文案里", "这是摘要正文" in text, repr(text))
    check("源名 / 标题 / 链接都还在",
          "📰 源名" in text and "· 标题" in text and "https://a.com/1" in text, repr(text))
    check("★ 顺序是 源名 → 标题 → 摘要 → 链接",
          text.index("标题") < text.index("这是摘要正文") < text.index("https://a.com/1"),
          repr(text))

    # 关掉开关 → 回到三行
    off = format_entry("源名", "https://a.com/f", e, with_summary=False)
    check("★ PUSH_SUMMARY=false 时摘要不出现", "这是摘要正文" not in off, repr(off))
    check("关掉时行数回到 3", len(off.splitlines()) == 3, repr(off))

    # 没有摘要 → 不能多出一个空行
    empty = format_entry("源名", "https://a.com/f", E("k", "标题", "https://a.com/1", None, "", ()))
    check("★ 没有摘要时不产生空行", len(empty.splitlines()) == 3, repr(empty))

    # summary 就是标题（很多源的 <description> 直接是标题）→ 不要重复一遍
    dup = format_entry(
        "源名", "https://a.com/f",
        E("k", "标题", "https://a.com/1", None, "标题", ()),
    )
    check("★ summary 等于标题时不重复", dup.count("标题") == 1, repr(dup))

    # summary = 标题 + 正文（同样常见）→ 剥掉开头那次，留正文
    dup2 = format_entry(
        "源名", "https://a.com/f",
        E("k", "标题", "https://a.com/1", None, "标题 - 正文在这里", ()),
    )
    check("★ summary 以标题开头时剥掉那一段", "正文在这里" in dup2, repr(dup2))
    check("★ 剥完标题只剩一次", dup2.count("标题") == 1, repr(dup2))

    # 超长摘要要截断。用 `MAX_SUMMARY_CHARS` 的 3 倍，**别写死数字** ——
    # 上限本身调过一次（150 → 400），写死的用例会在调参时假失败。
    long_text = format_entry(
        "源名", "https://a.com/f",
        E("k", "标题", "https://a.com/1", None, "啊" * (MAX_SUMMARY_CHARS * 3), ()),
    )
    check("★ 超长摘要被截断", long_text.count("啊") <= MAX_SUMMARY_CHARS,
          str(long_text.count("啊")))
    check("★ 截断后带省略号", "…" in long_text, repr(long_text[:60]))

    # `SUMMARY_CHARS` 要真的能调 —— 不然它就是个摆设
    short = format_entry(
        "源名", "https://a.com/f",
        E("k", "标题", "https://a.com/1", None, "啊" * 900, ()),
        summary_chars=80,
    )
    check("★ summary_chars 能收窄", short.count("啊") <= 80, str(short.count("啊")))

    # 摘要很长时文案会长，但**这不再是平台问题** —— 实测 40000 字符 QQ 也收
    # （scripts/probe_textlen.py）。这里只保证它别失控。
    check("★ 超长摘要下文案仍有限", len(long_text) < 2000, str(len(long_text)))


_check_format_entry_summary()


print("[4.95.1] 链接识别与渲染（纯函数）")


def _check_pv_pure() -> None:
    cases = [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://youtu.be/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://www.youtube.com/shorts/abc123XYZ_-", ("youtube", "abc123XYZ_-")),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        # ★ 带 list 参数时也得认出 v= 那个
        ("https://www.youtube.com/watch?list=PLx&v=dQw4w9WgXcQ&t=30", ("youtube", "dQw4w9WgXcQ")),
        ("https://x.com/BarackObama/status/896523232098078720", ("twitter", "896523232098078720")),
        ("https://twitter.com/jack/status/20", ("twitter", "20")),
        # 反例：这三个都不该被认领
        ("https://www.bilibili.com/video/BV15eJt6pEam", None),
        ("https://github.com/foo/bar", None),
        ("测试123", None),
    ]
    for text, want in cases:
        t = _pv.find_target(text)
        got = (t.site, t.vid) if t else None
        check(f"识别 {text[:46]}", got == want, f"得到 {got}，期望 {want}")

    # 正文按**显示宽度**截断：汉字算 2，所以 180 宽度只能放 ~91 个汉字
    check("★ 中文按显示宽度截断（不是字符数）", len(_pv._trim("汉" * 300)) < 100,
          str(len(_pv._trim("汉" * 300))))
    check("截断后有省略号", _pv._trim("汉" * 300).endswith("…"))
    check("短文本不截断", _pv._trim("短") == "短")
    check("正文里的换行被压掉", "\n" not in _pv._trim("a\nb\nc"))

    # ★ 推文**要原图**（无损是明确要求），所以任何档位都强制改写成 orig
    check("★ 推文配图强制 name=orig（无损）",
          _pv._twitter_covers({"photos": [{"url": "https://p.twimg.com/x.jpg?name=small"}]})
          == ("https://p.twimg.com/x.jpg?name=orig",))
    check("★ 原本没有 name 参数时补上 orig",
          _pv._twitter_covers({"photos": [{"url": "https://p.twimg.com/x.jpg"}]})
          == ("https://p.twimg.com/x.jpg?name=orig",))
    check("没有 media 时返回空元组", _pv._twitter_covers(None) == ())
    check("media 没有 photos 时返回空元组", _pv._twitter_covers({"photos": []}) == ())

    # 多图：按推文里的顺序，截到 MAX_IMAGES
    many = {"photos": [{"url": f"https://p.twimg.com/{i}.jpg"} for i in range(8)]}
    got = _pv._twitter_covers(many)
    check("★ 多图截到 MAX_IMAGES 张", len(got) == _pv.MAX_IMAGES, str(len(got)))
    check("多图保持推文里的顺序", got[0].startswith("https://p.twimg.com/0.jpg"), got[0])
    check("★ 多图每张都强制 orig", all("name=orig" in u for u in got), str(got))
    check("photos 里有脏数据时跳过、不整条放弃",
          _pv._twitter_covers(
              {"photos": [None, {"url": ""}, {"url": "https://p.twimg.com/ok.jpg"}]})
          == ("https://p.twimg.com/ok.jpg?name=orig",))
    check("★ Card.cover 取第一张（旧调用点靠它）",
          _pv.Card(site="x", url="u", title="t", author="a",
                   covers=("https://a/1.jpg", "https://a/2.jpg")).cover == "https://a/1.jpg")
    check("Card 没有配图时 cover 为 None",
          _pv.Card(site="x", url="u", title="t", author="a").cover is None)

    # oEmbed 的正文藏在 html 的 <p> 里，尾巴（作者/日期/script）必须甩掉
    frag = ('<blockquote class="twitter-tweet"><p lang="en" dir="ltr">just setting up '
            'my twttr</p>&mdash; jack (@jack) <a href="https://x.com/jack/status/20">'
            'March 21, 2006</a></blockquote><script async src="x.js"></script>')
    check("★ 从 oEmbed html 里抠出正文", _pv._text_from_oembed(frag) == "just setting up my twttr",
          repr(_pv._text_from_oembed(frag)))

    check("计数行：值拿不准就整行不要", _pv._count_line("播放", None) == "")
    check("计数行：0 照常显示", _pv._count_line("播放", 0) == "播放 0")
    check("计数行：负数不要", _pv._count_line("播放", -1) == "")

    card = _pv.Card(site="youtube", url="https://y/u", title="标题 | 带 [方括号] *星号*",
                    author="频道", covers=("https://i/c.jpg",), extra=("03:33",))
    text = _pv.render_text(card)
    check("render_text 含全部四行", text.count("\n") == 3, repr(text))
    md = _pv.render_markdown(card)
    check("★ markdown 剥掉会破坏结构的字符", "*星号*" not in md and "**标题" in md, repr(md))
    check("render_markdown 默认**不出图**（图片得 QQ 够得到才有意义）",
          "![" not in md, repr(md))


_check_pv_pure()


print("[4.95.2] compat 的文件上传补丁（file_data）")


def _check_file_data_patch() -> None:
    import inspect

    from botpy.api import BotAPI

    for name in ("post_group_file", "post_c2c_file"):
        sig = inspect.signature(getattr(BotAPI, name))
        check(f"★ {name} 收 file_data", "file_data" in sig.parameters, repr(list(sig.parameters)))
        check(f"{name} 的 file_data 是 keyword-only",
              sig.parameters["file_data"].kind is inspect.Parameter.KEYWORD_ONLY)
        check(f"{name} 的 url 变成可选（原版是必填）",
              sig.parameters["url"].default is None)

    check("补丁是幂等的（重复安装不改动）",
          install_new_protocol_support() == [], repr(install_new_protocol_support()))


_check_file_data_patch()


print("[4.60] AI 摘要（抓正文 + 翻译/精简）")


def _check_ai_parse() -> None:
    """模型回复的解析。**这是全链路最容易烂的一环** —— 它会裹代码块、
    会在 JSON 前后多说话、会偶尔返回半截。这些都不该让推送挂掉。"""
    from qq_rss_bot.ai import parse_digest

    ok = parse_digest('{"title":"译标题","summary":"摘要","translated":true}')
    check("干净 JSON 能解析", ok is not None and ok.title == "译标题", repr(ok))
    check("translated 透传", ok is not None and ok.translated is True, repr(ok))

    fenced = parse_digest('```json\n{"title":"T","summary":"S","translated":false}\n```')
    check("★ 裹了代码块也能解析", fenced is not None and fenced.title == "T", repr(fenced))

    chatty = parse_digest('好的，结果如下：{"title":"T2","summary":"S2","translated":false} 希望有帮助')
    check("★ JSON 前后多说话也能解析", chatty is not None and chatty.title == "T2", repr(chatty))

    check("★ 完全不是 JSON → None（不抛）", parse_digest("我不知道") is None)
    check("★ 空回复 → None", parse_digest("") is None and parse_digest(None) is None)
    check("★ 缺标题 → None（宁可不要也不能发一条没标题的）",
          parse_digest('{"summary":"只有摘要"}') is None)
    check("★ 顶层不是对象 → None", parse_digest('[1,2,3]') is None)
    # summary 允许为空：正文残缺时提示词就是这么要求的
    empty_sum = parse_digest('{"title":"T","summary":"","translated":false}')
    check("summary 为空是合法的", empty_sum is not None and empty_sum.summary == "", repr(empty_sum))


def _check_ai_extract() -> None:
    """正文提取要把导航、脚本、页脚剥掉。"""
    from qq_rss_bot.ai import extract_text, extractor_name

    html = (
        "<html><head><title>站点标题</title><script>var x=1;</script></head>"
        "<body><nav>首页 关于 联系</nav>"
        "<article><p>" + "这是正文第一段，讲了很重要的事情。" * 6 + "</p>"
        "<p>" + "这是正文第二段，继续展开。" * 6 + "</p></article>"
        "<footer>版权所有 2026</footer></body></html>"
    )
    got = extract_text(html)
    check("★ 正文能提出内容", len(got) > 100, f"{extractor_name()} -> {len(got)} 字")
    check("★ 导航被剥掉", "首页 关于" not in got, got[:80])
    check("★ 脚本被剥掉", "var x=1" not in got, got[:80])
    check("★ 页脚被剥掉", "版权所有" not in got, got[:80])
    check("空输入返回空串", extract_text("") == "")
    check("纯文本 HTML 也不炸", isinstance(extract_text("你好"), str))


def _check_ai_format() -> None:
    """有 digest 时文案要用 digest 的标题和摘要，并附上原标题。"""
    from qq_rss_bot.ai import Digest
    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.scheduler import format_entry

    e = E("k", "OpenAI ships GPT-7", "https://a.com/1", None, "英文导语一段", ())
    d = Digest(title="OpenAI 发布 GPT-7", summary="中文摘要正文", translated=True)
    text = format_entry("TechCrunch", "https://a.com/f", e, digest=d)
    check("★ 用译文标题", "OpenAI 发布 GPT-7" in text, repr(text))
    check("★ 用 AI 摘要", "中文摘要正文" in text, repr(text))
    check("★ 不再混进 RSS 原文案", "英文导语一段" not in text, repr(text))
    check("链接还在", "https://a.com/1" in text, repr(text))

    # **不附原标题。** 附过一版（`原题：…`），发出来一条四段太长，
    # 群里一屏放不下，而原题信息量最低。`translated` 现在只写日志。
    check("★ 不再附原标题（内容太长，实测砍掉）", "原题：" not in text, repr(text))
    # 标题和正文之间**有一个空行**（2026-10-18 用户要求）：推送走的是
    # msg_type=0/7 的**纯文本**（不是 markdown），单换行读起来就是
    # 「标题紧贴着正文」一坨。空行把它切成两段。
    check("★ 译文时文案 5 行（标题与正文之间有空行）",
          len(text.splitlines()) == 5, repr(text))
    check("★ 空行确实在标题和正文之间",
          text.splitlines()[2] == "" and "中文摘要正文" in text.splitlines()[3], repr(text))

    zh = Digest(title="OpenAI ships GPT-7", summary="中文摘要", translated=False)
    t2 = format_entry("TechCrunch", "https://a.com/f", e, digest=zh)
    check("未翻译时也不附原标题", "原题：" not in t2, repr(t2))

    same = Digest(title="OpenAI ships GPT-7", summary="S", translated=True)
    t3 = format_entry("TechCrunch", "https://a.com/f", e, digest=same)
    check("译文等于原标题时也不重复", "原题：" not in t3, repr(t3))

    # summary 为空 → 不留空行
    bare = Digest(title="T", summary="", translated=False)
    t4 = format_entry("源", "u", e, digest=bare)
    check("★ summary 为空时不留空行", len(t4.splitlines()) == 3, repr(t4))

    # digest=None → 完全退回原行为
    t5 = format_entry("TechCrunch", "https://a.com/f", e, digest=None)
    check("★ 没有 digest 时退回 RSS 导语", "英文导语一段" in t5, repr(t5))


async def _check_ai_pipeline() -> None:
    """缓存 / 日上限 / 失败静默 —— 三件都必须对，否则就是烧钱或者丢推送。"""
    import json  # noqa: PLC0415 —— 这个文件按节就近 import

    from qq_rss_bot.rss import Entry as E
    from qq_rss_bot.scheduler import prepare_digests

    class FakeHttp:
        """一个客户端同时伺候 GET（抓正文）和 POST（调模型）。"""

        def __init__(self, article="", reply="", get_raises=False, post_raises=False):
            self.article, self.reply = article, reply
            self.gets: list[str] = []
            self.posts: list[dict] = []
            self._get_raises, self._post_raises = get_raises, post_raises

        async def get(self, url, **kw):
            self.gets.append(url)
            if self._get_raises:
                raise httpx.ConnectError("boom")
            return httpx.Response(
                200, text=f"<html><body><p>{self.article}</p></body></html>",
                request=httpx.Request("GET", url),
            )

        async def post(self, url, **kw):
            self.posts.append(kw.get("json") or {})
            if self._post_raises:
                raise httpx.ReadTimeout("slow")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": self.reply}}]},
                request=httpx.Request("POST", url),
            )

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        sid, _ = db.add_subscription("G", "https://a.com/f", "源名", 60)
        db.set_subscription_ai(sid, True)
        db.mark_polled(sid, title="源名", mark_initialized=True)
        sub = db.list_subscriptions("G")[0]

        cfg = dataclasses.replace(
            cfg_stub(), ai_api_key="k", summary_chars=400,
        )
        entry = E("k1", "英文标题", "https://a.com/1", None, "导语", ())

        # --- 正常路径（默认 **不抓文章页**）---
        http = FakeHttp(article="正文" * 200, reply='{"title":"中文标题","summary":"摘要","translated":true}')
        got = await prepare_digests(db, cfg, http, None, sub, [entry])
        check("★ 正常路径拿到摘要", "k1" in got and got["k1"].title == "中文标题", repr(got))
        check("★ 默认不抓文章页（AI_FETCH_ARTICLE 默认 false）", http.gets == [], repr(http.gets))
        check("调了一次模型", len(http.posts) == 1, repr(http.posts))
        check("★ 缓存已落库", db.get_ai_cache("k1") is not None, repr(db.get_ai_cache("k1")))

        # --- 默认关掉 thinking ---
        payload = http.posts[0]
        check("★ 默认关掉 thinking（DeepSeek 默认开着且档位 high）",
              payload.get("thinking") == {"type": "disabled"}, repr(payload.get("thinking")))
        check("temperature 仍带着（thinking 关掉之后它才生效）",
              payload.get("temperature") == 0.3, repr(payload.get("temperature")))
        check("response_format 是 json_object",
              payload.get("response_format") == {"type": "json_object"}, repr(payload))

        # --- 打开抓正文 ---
        e_fetch = E("kfetch", "标题", "https://a.com/fetch", None, "导语", ())
        http_f = FakeHttp(article="文章正文" * 100,
                          reply='{"title":"T","summary":"S","translated":false}')
        await prepare_digests(
            db, dataclasses.replace(cfg, ai_fetch_article=True), http_f, None, sub, [e_fetch]
        )
        check("★ AI_FETCH_ARTICLE=true 时才抓文章页",
              http_f.gets == ["https://a.com/fetch"], repr(http_f.gets))
        check("★ 抓到的正文被喂给模型",
              "文章正文" in http_f.posts[0]["messages"][1]["content"],
              http_f.posts[0]["messages"][1]["content"][:120])

        # --- 缓存命中：**一次调用都不该有** ---
        http2 = FakeHttp(article="x", reply="{}")
        got2 = await prepare_digests(db, cfg, http2, None, sub, [entry])
        check("★ 缓存命中不重复调用", http2.posts == [] and http2.gets == [],
              f"gets={http2.gets} posts={http2.posts}")
        check("★ 缓存命中仍返回摘要", got2.get("k1") is not None, repr(got2))

        # --- 日上限 ---
        e2 = E("k2", "另一条", "https://a.com/2", None, "导语", ())
        db.bump_ai_count(__import__("qq_rss_bot.quota", fromlist=["today"]).today(),
                         cfg.ai_daily_limit)
        http3 = FakeHttp(article="正文" * 200, reply='{"title":"T","summary":"S"}')
        got3 = await prepare_digests(db, cfg, http3, None, sub, [e2])
        check("★ 撞上日上限就不再调用", http3.posts == [], repr(http3.posts))
        check("★ 撞上限时静默返回（不抛、不丢推送）", got3 == {}, repr(got3))

    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        sid, _ = db.add_subscription("G", "https://a.com/f", "源名", 60)
        db.set_subscription_ai(sid, True)
        sub = db.list_subscriptions("G")[0]
        cfg = dataclasses.replace(cfg_stub(), ai_api_key="k")

        # --- 抓正文失败 → 退回 RSS 导语，仍然调模型（翻译标题还有用）---
        e = E("k9", "English title", "https://a.com/9", None, "RSS 导语内容", ())
        http = FakeHttp(reply='{"title":"中文","summary":"S","translated":true}', get_raises=True)
        got = await prepare_digests(db, cfg, http, None, sub, [e])
        check("★ 抓不到正文时仍能翻译标题", got.get("k9") is not None, repr(got))
        check("★ 抓不到正文时把 RSS 导语喂给模型",
              "RSS 导语内容" in json.dumps(http.posts, ensure_ascii=False), repr(http.posts)[:200])

        # --- AI_DISABLE_THINKING=false 时不能带 thinking 字段 ---
        # （非 DeepSeek 的 OpenAI 兼容端点不认它，OpenAI 会直接 400）
        e_th = E("kth", "T", "https://a.com/th", None, "导语", ())
        http_th = FakeHttp(reply='{"title":"T","summary":"S","translated":false}')
        await prepare_digests(
            db, dataclasses.replace(cfg, ai_disable_thinking=False), http_th, None, sub, [e_th]
        )
        check("★ AI_DISABLE_THINKING=false 时不发 thinking 字段",
              "thinking" not in http_th.posts[0], repr(http_th.posts[0].keys()))

        # --- 模型挂了 → 静默 ---
        e2 = E("k10", "T", "https://a.com/10", None, "导语", ())
        http2 = FakeHttp(article="正文" * 100, reply="", post_raises=True)
        got2 = await prepare_digests(db, cfg, http2, None, sub, [e2])
        check("★ 模型超时 → 静默返回，不抛", got2 == {}, repr(got2))

        # --- 没配 key → 一次请求都不发 ---
        e3 = E("k11", "T", "https://a.com/11", None, "导语", ())
        http3 = FakeHttp(article="x", reply="{}")
        got3 = await prepare_digests(
            db, dataclasses.replace(cfg, ai_api_key=""), http3, None, sub, [e3]
        )
        check("★ 没配 key 时一次请求都不发",
              got3 == {} and http3.gets == [] and http3.posts == [],
              f"{http3.gets} {http3.posts}")

        # --- 源没开 AI → 一次请求都不发 ---
        db.set_subscription_ai(sid, False)
        sub_off = db.list_subscriptions("G")[0]
        http4 = FakeHttp(article="x", reply="{}")
        got4 = await prepare_digests(db, cfg, http4, None, sub_off, [e3])
        check("★ 源没开 AI 时一次请求都不发",
              got4 == {} and http4.gets == [] and http4.posts == [],
              f"{http4.gets} {http4.posts}")
        db.close()


async def _check_ai_command() -> None:
    """`/ai` 和 `/sub --ai` 的接线。"""
    import inspect

    from qq_rss_bot import commands as cmds
    from qq_rss_bot.commands import COMMAND_ALIASES, COMMAND_METHODS, CommandHandler

    check("★ /ai 已注册", COMMAND_ALIASES.get("ai") == "ai", repr(COMMAND_ALIASES.get("ai")))
    check("★ /ai 挂到了方法上",
          COMMAND_METHODS.get("ai") == "_ai"
          and hasattr(CommandHandler, "_ai"), repr(COMMAND_METHODS.get("ai")))
    check("★ /ai 是管理员指令", "ai" in cmds.RESTRICTED_CANONICAL, repr(cmds.RESTRICTED_CANONICAL))
    from qq_rss_bot.ai import DEFAULT_MODEL

    # `deepseek-chat` 已经退役（2026-10-18 查官方定价页）。写错模型名是
    # **静默降级** —— 症状是「开了 AI 却什么都没变」，所以钉住这一条。
    check("★ 默认模型是 deepseek-flash（deepseek-chat 已退役）",
          DEFAULT_MODEL == "deepseek-flash", DEFAULT_MODEL)

    # 启动自检那句话**不能自带 `AI 摘要：` 前缀** —— 调用方会加，
    # 两边都写就成了 `AI 摘要：AI 摘要：已配置…`（实测在日志里看到过）。
    from qq_rss_bot.ai import probe as ai_probe

    line = ai_probe("k", configured=True, model=DEFAULT_MODEL)
    check("★ 自检句不带重复前缀（日志里出现过 AI 摘要：AI 摘要：）",
          not line.startswith("AI 摘要"), line)
    check("自检句报出模型名（模型名写错是静默降级）",
          DEFAULT_MODEL in line, line)
    check("没配 key 时也说得清楚",
          "没配" in ai_probe("", configured=False) or "未配置" in ai_probe("", configured=False),
          ai_probe("", configured=False))
    check("★ 别名表里每个方法都真的存在（加了别名忘挂方法就是死路）",
          all(hasattr(CommandHandler, m) for m in COMMAND_METHODS.values()),
          repr([m for m in COMMAND_METHODS.values() if not hasattr(CommandHandler, m)]))

    src = inspect.getsource(cmds.CommandHandler._sub)
    check("★ /sub 认 --ai", '"--ai"' in src, "源码里没有 --ai")
    check("★ --ai 会被摘出位置参数（不能当成 URL 或间隔）",
          'a.lower() != "--ai"' in src, src[:200])


_check_ai_parse()
_check_ai_extract()
_check_ai_format()
asyncio.run(_check_ai_pipeline())
asyncio.run(_check_ai_command())


print("[4.61] 偶像大师的企划/分类标签")

from qq_rss_bot import idolmaster as _imas  # noqa: E402


class _ImasHttp:
    """按 URL 路由的假 httpx。记录每次请求的 URL。"""

    def __init__(self, articles=None, *, fail=False, token="T"):
        self.calls: list[str] = []
        self._articles, self._fail, self._token = articles, fail, token

    async def get(self, url, **kw):
        self.calls.append(url)
        if self._fail:
            raise httpx.ConnectError("boom")
        if url.endswith("/Token/get"):
            data = {} if self._token is None else {"token": self._token}
            return _FakeResp(payload={"data": data})
        return _FakeResp(payload={"data": {"article_list": self._articles or []}})


def _check_imas_pure() -> None:
    check("★ 认出偶像大师订阅（路由形态）", _imas.matches("/idolmaster/news"))
    check("★ 认出偶像大师订阅（完整 URL）",
          _imas.matches("http://rsshub:1200/idolmaster/news"))
    check("★ 认出带 brand 参数的路由",
          _imas.matches("http://rsshub:1200/idolmaster/news/brand=SHINYCOLORS"))
    check("别的源返回 False", not _imas.matches("http://rsshub:1200/v2ex/topics"))
    check("★ 不把长得像的域名误判成偶像大师",
          not _imas.matches("https://example.com/idolmasterish/news"))

    check("★ 单企划单分类",
          _imas.tag_line(("CINDERELLAGIRLS",), ("GOODS",)) == "【灰姑娘】【谷子】",
          _imas.tag_line(("CINDERELLAGIRLS",), ("GOODS",)))
    check("★ 多企划用 / 连",
          _imas.tag_line(("IDOLMASTER", "CINDERELLAGIRLS", "MILLIONLIVE"), ("OTHER",))
          == "【本家/灰姑娘/百万】【其他】")
    check("★ 七个企划全挂（通稿类）",
          _imas.tag_line(tuple(_imas.BRAND_NAMES), ("OTHER",))
          == "【本家/灰姑娘/百万/男人/闪彩/学马/其他】【其他】",
          _imas.tag_line(tuple(_imas.BRAND_NAMES), ("OTHER",)))
    check("★ 多分类也用 / 连（实测 65/180 篇是两个分类）",
          _imas.tag_line(("SHINYCOLORS",), ("LIVE-EVENT", "GOODS")) == "【闪彩】【演出/谷子】")
    check("★ 未知 code 原样显示（静默丢掉的症状是「这条怎么没标签」）",
          _imas.tag_line(("NEWBRAND",), ("NEWTYPE",)) == "【NEWBRAND】【NEWTYPE】")
    check("只有企划没有分类也行", _imas.tag_line(("GAKUEN",), ()) == "【学马】")
    check("两边都空 → 空串（调用方据此不加这一行）", _imas.tag_line((), ()) == "")

    check("★ 企划表 7 个", len(_imas.BRAND_NAMES) == 7, str(sorted(_imas.BRAND_NAMES)))
    check("★ 分类表 11 个（CMS subcategory 全集）",
          len(_imas.TYPE_NAMES) == 11, str(sorted(_imas.TYPE_NAMES)))
    # 这两个 code 名不副实，别照着 code 直译
    check("★ CD 译「音乐」不是「CD」（日文原名是 ミュージック）",
          _imas.TYPE_NAMES["CD"] == "音乐")
    check("★ LIVE-EVENT 译「演出」不是「演唱会」（含线下活动）",
          _imas.TYPE_NAMES["LIVE-EVENT"] == "演出")


_check_imas_pure()

_IMAS_META = [
    {"url": "https://idolmaster-official.jp/news/01_1.html",
     "brand": [{"code": "CINDERELLAGIRLS", "name": "シンデレラガールズ"}],
     "categories": {"code": "NEWS", "subcategory": [{"code": "GOODS", "name": "グッズ"}]}},
    {"url": "https://idolmaster-official.jp/news/01_2.html",
     "brand": [{"code": "IDOLMASTER"}, {"code": "OTHER"}],
     "categories": {"subcategory": [{"code": "LIVE-EVENT"}, {"code": "GOODS"}]}},
    # 脏数据：没有 url、brand 不是 list、元素缺 code —— 全都得滤掉而不是炸
    {"brand": [{"code": "X"}]},
    {"url": "https://idolmaster-official.jp/news/01_3.html",
     "brand": "坏", "categories": None},
    {"url": "https://idolmaster-official.jp/news/01_4.html",
     "brand": [{"name": "没code"}, "字符串"], "categories": {"subcategory": [{"code": ""}]}},
]


def _check_imas_fetch() -> None:
    async def main() -> None:
        http = _ImasHttp(_IMAS_META)
        got = await _imas.fetch_meta(http)
        check("★ 两次请求（先换 token 再查列表）", len(http.calls) == 2, str(http.calls))
        u1 = "https://idolmaster-official.jp/news/01_1.html"
        check("★ 按文章 url 建映射", u1 in got, str(sorted(got)))
        check("★ 企划取的是 code 元组", got[u1][0] == ("CINDERELLAGIRLS",), str(got[u1]))
        check("★ 分类取的是 code（不是 RSS 里那个日文原名）",
              got[u1][1] == ("GOODS",), str(got[u1]))
        check("★ 多值保序",
              got["https://idolmaster-official.jp/news/01_2.html"]
              == (("IDOLMASTER", "OTHER"), ("LIVE-EVENT", "GOODS")))
        check("★ 没有 url 的条目被丢掉（其余 4 条都在）", len(got) == 4, str(sorted(got)))
        check("★ brand 不是 list 时不炸，当空处理",
              got["https://idolmaster-official.jp/news/01_3.html"][0] == ())
        check("★ 元素缺 code / 空串被滤掉",
              got["https://idolmaster-official.jp/news/01_4.html"] == ((), ()))

        e1 = Entry(key=u1, title="t1", link=u1, published=None, summary="")
        e2 = Entry(key="k2", title="t2",
                   link="https://idolmaster-official.jp/news/01_2.html",
                   published=None, summary="")
        e3 = Entry(key="k3", title="t3",
                   link="https://idolmaster-official.jp/news/99.html",
                   published=None, summary="")
        tags = await _imas.tags_for(_ImasHttp(_IMAS_META), "/idolmaster/news", [e1, e2, e3])
        check("★ key 就是 link 时对得上", tags.get(u1) == "【灰姑娘】【谷子】", str(tags))
        check("★ key 和 link 不同也能按 link 对上",
              tags.get("k2") == "【本家/其他】【演出/谷子】", str(tags))
        check("★ CMS 里没有的文章不给标签（宁可不加也不瞎猜）", "k3" not in tags, str(tags))

        spy = _ImasHttp(_IMAS_META)
        check("★ 非偶像大师订阅一次请求都不发",
              await _imas.tags_for(spy, "/v2ex/topics", [e1]) == {} and spy.calls == [],
              str(spy.calls))
        spy2 = _ImasHttp(_IMAS_META)
        check("空 entries 也不发请求",
              await _imas.tags_for(spy2, "/idolmaster/news", []) == {} and spy2.calls == [])

        check("★ 网络挂了静默返回 {}，绝不抛（这条路在推送主循环里）",
              await _imas.tags_for(_ImasHttp(fail=True), "/idolmaster/news", [e1]) == {})
        check("★ token 响应里没有 token 时也不炸",
              await _imas.tags_for(_ImasHttp(_IMAS_META, token=None),
                                   "/idolmaster/news", [e1]) == {})
        check("★ 列表是空数组时返回 {}",
              await _imas.tags_for(_ImasHttp([]), "/idolmaster/news", [e1]) == {})

    asyncio.run(main())


_check_imas_fetch()


def _check_imas_format() -> None:
    e = Entry(key="k", title="【シャニマス】コラボカフェ開催決定！",
              link="https://idolmaster-official.jp/news/01_1.html",
              published=None, summary="プロデューサーの皆さん、こんにちは！")
    plain = format_entry("NEWS | アイドルマスター", "u", e)
    tagged = format_entry("NEWS | アイドルマスター", "u", e, tags="【闪彩】【联动】")

    check("★ 标签紧跟源名（第 2 行）",
          tagged.splitlines()[1] == "【闪彩】【联动】", repr(tagged))
    check("★ 标签在标题**之上**",
          tagged.index("【闪彩】【联动】") < tagged.index("· 【シャニマス】"), repr(tagged))
    check("★ 标签多占一行",
          len(tagged.splitlines()) == len(plain.splitlines()) + 1, repr(tagged))
    check("★ 空 tags 时一行都不多", len(plain.splitlines()) == 5, repr(plain))
    check("★ 空 tags 不留空行", "\n\n\n" not in plain, repr(plain))

    # 正文和标题一样 → 正文那行**和空行**都不加（用户 2026-10-18 明确要求）
    same = Entry(key="k2", title="标题", link="https://a.com/2",
                 published=None, summary="标题")
    t = format_entry("源", "u", same, tags="【本家】【其他】")
    check("★ 正文等于标题时不重复正文", t.count("标题") == 1, repr(t))
    check("★ 也不留空行", "\n\n" not in t, repr(t))
    check("★ 只有 4 行：源名/标签/标题/链接", len(t.splitlines()) == 4, repr(t))

    # 正文里带标题前缀 → 剥掉前缀后如果没剩东西，同样不重复
    pref = Entry(key="k3", title="标题", link="https://a.com/3",
                 published=None, summary="标题 - 后面还有正文")
    t2 = format_entry("源", "u", pref)
    check("★ 剥掉标题前缀后剩下的正文照发", "后面还有正文" in t2, repr(t2))
    check("★ 这时才有空行", "\n\n" in t2, repr(t2))


_check_imas_format()

check("★ IMAS_TAGS 开关存在且默认开", cfg_stub().imas_tags is True)


print("[4.62] markdown 多图（N 张图合并成一条消息）")

from qq_rss_bot import multiimage as _mi  # noqa: E402


def _png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x00" * 8)


def _gif(w: int, h: int) -> bytes:
    return b"GIF89a" + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00" * 24


def _jpeg(w: int, h: int) -> bytes:
    return (b"\xff\xd8" + b"\xff\xe0" + (16).to_bytes(2, "big") + b"\x00" * 14
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x00" * 9)


def _check_mi_size() -> None:
    check("★ PNG 读得出宽高", _mi.image_size(_png(1200, 630)) == (1200, 630))
    check("★ GIF 读得出宽高", _mi.image_size(_gif(300, 200)) == (300, 200))
    check("★ JPEG 要扫到 SOFn 才读得出（不在头几个字节里）",
          _mi.image_size(_jpeg(640, 480)) == (640, 480))
    check("认不出来的返回 None", _mi.image_size(b"definitely not an image" * 3) is None)
    check("只有 2 字节也不炸", _mi.image_size(b"\xff\xd8") is None)
    check("★ 被截断的 JPEG 也不炸（Range 只取 32KB 会碰到）",
          _mi.image_size(b"\xff\xd8\xff\xe0\x00\x10" + b"\x00" * 30) is None)


def _check_mi_box() -> None:
    check("★ 宽固定 208", _mi.box_for((1000, 563))[0] == 208)
    check("★ 高度按真实比例（1000x563 → 117）", _mi.box_for((1000, 563)) == (208, 117))
    check("★ 正方形渲染出来还是正方形（这个坑踩过：统一 16:9 会压扁）",
          _mi.box_for((1000, 1000)) == (208, 208))
    check("★ 竖图也是竖的", _mi.box_for((772, 1000)) == (208, 269))
    check("★ 极宽的夹到 MIN_H", _mi.box_for((10000, 100)) == (208, 40))
    check("★ 极高的夹到 MAX_H", _mi.box_for((100, 10000)) == (208, 400))
    check("★ 尺寸提示必须带 #（漏了手机端**整条 markdown 不渲染**）",
          _mi.image_line(1, "https://a/b.jpg", (1000, 563))
          == "![图1 #208px #117px](https://a/b.jpg)",
          _mi.image_line(1, "https://a/b.jpg", (1000, 563)))


_check_mi_size()
_check_mi_box()


def _check_mi_build() -> None:
    items = [("https://a/1.jpg", (1000, 563)), ("https://a/2.jpg", (1000, 1000))]
    lines = _mi.build("正文", items, "https://a/page").split("\n")
    check("★ 顺序是 正文 → 图 → 原文链接",
          lines[0] == "正文" and lines[1].startswith("![图1 ")
          and lines[2].startswith("![图2 ")
          and lines[3] == "[打开原文](https://a/page)", repr(lines))
    check("★ 每张图各算各的比例",
          lines[1] == "![图1 #208px #117px](https://a/1.jpg)"
          and lines[2] == "![图2 #208px #208px](https://a/2.jpg)", repr(lines))
    check("★ 没有链接时不留一行空链接", "[打开原文]" not in _mi.build("正文", items, ""))

    # 超长：从尾部丢图，**绝不截断**（截断会把图片语法砍成半截，整条不渲染）
    many = [(f"https://example.com/{'x' * 900}/{i}.jpg", (1000, 563)) for i in range(6)]
    long_md = _mi.build("正文", many, "https://a/page")
    got = long_md.split("\n")
    check("★ 超长时从尾部丢图", len(long_md) <= _mi.MAX_CHARS, str(len(long_md)))
    check("★ 丢到最后一张也是完整的图片语法（没有半截的）",
          all(ln.startswith("![图") and ln.endswith(")") for ln in got if ln.startswith("![")),
          long_md[-160:])
    check("★ 第一张永远留着", got[1] == f"![图1 #208px #117px]({many[0][0]})")

    # 真实尺寸的组合：6 张偶像大师图（URL ~152 字符）+ 800 字摘要。
    # **一张都不许丢** —— 上限拍太低就是这个症状：一条 6 图的资讯只出 3、4 张。
    real = [(
        "https://cmsapi-frontend.idolmaster-official.jp/sitern/api/idolmaster/Image/get"
        f"?path=/idolmaster/jp/article/00{i}/2026/10/{'a' * 60}.jpeg", (1000, 563),
    ) for i in range(6)]
    real_md = _mi.build(
        "📰 偶像大师 官方 NEWS\n【灰姑娘/百万】【谷子】\n· 某条很长的标题\n\n  " + "字" * 800,
        real, "https://idolmaster-official.jp/news/01_20180.html",
    )
    check("★ 6 张真实长度的图 + 800 字摘要，一张都不许丢",
          real_md.count("![图") == 6, str(real_md.count("![图")))


_check_mi_build()


class _MiHttp:
    """假 httpx：按 URL 返回构造好的图片头；不在表里的抛异常（模拟抓不到）。"""

    def __init__(self, sizes: dict):
        self.sizes, self.calls = sizes, []

    async def get(self, url, **kw):
        self.calls.append((url, kw.get("headers")))
        size = self.sizes.get(url)
        if size is None:
            raise httpx.ConnectError("boom")
        return _FakeResp(content=_png(*size))


async def _check_mi_measure() -> None:
    http = _MiHttp({"u1": (1000, 563), "u2": (1000, 1000)})
    got = await _mi.measure_all(http, ["u1", "u2", "bad"])
    check("★ 读不到尺寸的那张被丢掉（不猜比例，猜错就是图被拉伸）",
          got == [("u1", (1000, 563)), ("u2", (1000, 1000))], str(got))
    check("★ 每张都请求了（含失败那张）", len(http.calls) == 3)
    check("★ 只取前 32KB —— 这是头部读取不是整张下载",
          all(h and h.get("Range") == f"bytes=0-{_mi._PROBE_BYTES - 1}"
              for _, h in http.calls), str(http.calls))

    http2 = _MiHttp({"u1": (1000, 563)})
    check("★ MAX_IMAGES 封顶，超出的根本不请求",
          len(await _mi.measure_all(
              http2, [f"u{i}" for i in range(_mi.MAX_IMAGES + 3)])) == 1
          and len(http2.calls) == _mi.MAX_IMAGES, str(len(http2.calls)))


asyncio.run(_check_mi_measure())


class _MiPusher:
    """记录三类出口的假 Pusher。`uploads` 给一批布尔值决定每次探针的结果。"""

    def __init__(self, uploads: "list[bool] | None" = None, md_fails: bool = False):
        self.texts: list[str] = []
        self.rich: list[tuple[str, str]] = []
        self.markdowns: list[str] = []
        self.uploads: list[str] = []
        self._queue = list(uploads) if uploads is not None else None
        self._md_fails = md_fails

    async def send_text(self, group_openid, content):
        self.texts.append(content)

    async def send_rich(self, group_openid, content, image_url):
        self.rich.append((content, image_url))

    async def send_markdown(self, group_openid, content):
        if self._md_fails:
            raise RuntimeError("no markdown template")
        self.markdowns.append(content)

    async def upload_image(self, group_openid, image_url):
        self.uploads.append(image_url)
        return self._queue.pop(0) if self._queue else True


async def _check_mi_send() -> None:
    from qq_rss_bot.scheduler import send_entry_images

    http = _MiHttp({"https://a/1.jpg": (1000, 563), "https://a/2.jpg": (1000, 1000)})
    two = ["https://a/1.jpg", "https://a/2.jpg"]

    p = _MiPusher()
    failed = await send_entry_images(
        p, "G", "正文", two, http=http, link="https://a/page", markdown=True)
    check("★ 多图合成**一条** markdown，一条富媒体都不发",
          len(p.markdowns) == 1 and not p.rich, f"{p.markdowns} | {p.rich}")
    check("★ 正文、两张图、原文链接都在同一条里",
          all(x in p.markdowns[0] for x in
              ("正文", "![图1 #208px #117px]", "![图2 #208px #208px]",
               "[打开原文](https://a/page)")), p.markdowns[0])
    check("全成功时没有失败列表", failed == [], str(failed))

    p = _MiPusher()
    await send_entry_images(p, "G", "正文", ["https://a/1.jpg"],
                            http=http, link="l", markdown=True)
    check("★ 只有一张图时不走 markdown（合并没意义，还多一次上传）",
          not p.markdowns and len(p.rich) == 1, f"{p.markdowns} | {p.rich}")

    p = _MiPusher()
    await send_entry_images(p, "G", "正文", two, http=http, link="l", markdown=False)
    check("★ 开关关掉时退回原来的连发",
          not p.markdowns and len(p.rich) == 2 and not p.uploads)

    p = _MiPusher(uploads=[False, False])
    await send_entry_images(p, "G", "正文", two, http=http, link="l", markdown=True)
    check("★ QQ 一张都抓不到时退回连发，一条都不少",
          not p.markdowns and len(p.rich) == 2, f"{p.markdowns} | {p.rich}")

    p = _MiPusher(uploads=[True, False])
    failed = await send_entry_images(p, "G", "正文", two, http=http, link="l", markdown=True)
    check("★ 只有部分抓不到时仍然合并成一条（能抓的照发）",
          len(p.markdowns) == 1 and "![图1" in p.markdowns[0]
          and "![图2" not in p.markdowns[0], str(p.markdowns))
    check("★ 抓不到的那张进告警列表（裂图在群里是看不见的）",
          failed == ["https://a/2.jpg"], str(failed))

    p = _MiPusher(md_fails=True)
    await send_entry_images(p, "G", "正文", two, http=http, link="l", markdown=True)
    check("★ markdown 发不出去（比如没有模板权限）时退回连发",
          len(p.rich) == 2, str(p.rich))

    p = _MiPusher()
    await send_entry_images(p, "G", "正文", ["bad1", "bad2"],
                            http=http, link="l", markdown=True)
    check("★ 尺寸一张都读不到时退回连发（不硬猜比例）",
          not p.markdowns and len(p.rich) == 2, f"{p.markdowns} | {p.rich}")

    p = _MiPusher()
    await send_entry_images(
        p, "G", "正文", ["https://a/1.jpg", "https://a/2.jpg"],
        http=_MiHttp({"https://a/1.jpg": (1000, 563)}), link="l", markdown=True)
    check("★ 两张里只有一张读得到尺寸 → 那条 markdown 只带这一张",
          len(p.markdowns) == 1 and "![图2" not in p.markdowns[0], str(p.markdowns))


asyncio.run(_check_mi_send())

# markdown 发送撞 `50035006` 必须重试一次。**不重试的代价是 1 条变 6 条**
# （退回连发），2026-10-18 一天撞了 4 次。预热那条更亏：连发走
# `post_group_file`，那条路预热不了渲染缓存，退回去等于预热白做。


class _FlakyMarkdownPusher:
    # noqa: 最小实现，只够 `_send_entry_markdown` 用
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.markdowns: list = []

    async def upload_image(self, g, u):
        return True

    async def send_markdown(self, g, c):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("50035006 消息发送异常，请稍后重试")
        self.markdowns.append((g, c))


async def _md_retry(fail_times: int):
    from qq_rss_bot.scheduler import _send_entry_markdown  # noqa: PLC0415

    p = _FlakyMarkdownPusher(fail_times)
    http = _MiHttp({"https://a/1.jpg": (1000, 563), "https://a/2.jpg": (1000, 1000)})
    dropped = await _send_entry_markdown(
        p, "G", "正文", ["https://a/1.jpg", "https://a/2.jpg"], http, "l"
    )
    return p, dropped


_p1, _d1 = asyncio.run(_md_retry(fail_times=1))
check("★ markdown 撞 50035006 → 隔 1 秒重试一次，第二条就成功了（不退回连发）",
      len(_p1.markdowns) == 1 and _p1.calls == 2 and _d1 == [],
      f"calls={_p1.calls} sent={len(_p1.markdowns)}")

_p2, _d2 = asyncio.run(_md_retry(fail_times=2))
check("★ 重试一次仍然失败 → 才退回连发（返回 None）",
      _p2.calls == 2 and _d2 is None and len(_p2.markdowns) == 0,
      f"calls={_p2.calls} ret={_d2}")

check("★ MULTI_IMAGE_MARKDOWN 开关存在且默认开",
      cfg_stub().multi_image_markdown is True)

# 摄入上限（rss.MAX_IMAGES）和渲染上限（multiimage.MAX_IMAGES）**必须一致**。
# 不一致的症状是「这条明明有 6 张怎么只出来 4 张」，而且两处都不报错 ——
# 2026-10-18 就是这个原因，一张 6 图的资讯只出来 4 张。
from qq_rss_bot.rss import MAX_IMAGES as _RSS_MAX  # noqa: E402
from qq_rss_bot.rss import all_images as _all_images  # noqa: E402

check("★ 摄入上限是 6（原来 4 是按「每张图各占一条配额」定的，"
      "多图合并成一条之后这个理由不成立了）", _RSS_MAX == 6, str(_RSS_MAX))
check("★ 摄入上限和渲染上限一致，不许各改各的",
      _RSS_MAX == _mi.MAX_IMAGES, f"rss={_RSS_MAX} multiimage={_mi.MAX_IMAGES}")

_seven = "".join(f'<img src="https://a/{i}.jpg">' for i in range(7))
check("★ 6 张全收", len(_all_images(_seven)) == 6, str(len(_all_images(_seven))))
check("★ 第 7 张及以后丢掉", "https://a/6.jpg" not in _all_images(_seven))
check("★ 去重仍然生效（不少源 `<img>` 和裸链接给了同一张）",
      len(_all_images('<img src="https://a/1.jpg"> https://a/1.jpg')) == 1)


# ---------------------------------------------------------------------------
print("[4.63] markdown 多图预热 / 人工预览（喊「不发」挡下这一条）")
# ---------------------------------------------------------------------------
#
# 为什么要有这一节：QQ 的图片缓存是**服务端、跨群共享**的，但**冷链接第一次
# 渲染必炸** —— 2026-10-18 实测：一批 3.4 MB 的图 6 张全裂；同一批**同样的
# URL** 隔 20 秒发到**另一个群**，6 张全好。冷发那次一张都没抓下来（客户端
# 上什么都没缓存），所以只可能是服务端。
#
# 于是正式群之前先预热两次，中间留一个人工否决窗口。这一节**全部离线**：
# `_poll_one` 换成假的，不碰网络也不发消息。

import dataclasses as _dc  # noqa: E402

from qq_rss_bot.db import Subscription as _Sub  # noqa: E402

check("默认关闭：两个群都留空，行为完全等于改动前",
      cfg_stub().warmup_group == "" and cfg_stub().preview_group == "")
check("默认：预热等 45 秒，否决窗口 30 秒",
      cfg_stub().preview_warmup_sec == 45 and cfg_stub().preview_veto_sec == 30)


def _mk_sub(sid: int, gid: str, url: str = "http://rss/same") -> "_Sub":
    return _Sub(
        id=sid, group_openid=gid, url=url, title=None,
        interval_sec=900, initialized=True, last_polled_at=None, last_error=None,
    )


class _FakeDB:
    """只实现 `_stage_subs` 用到的那一个方法。"""

    def __init__(self, subs: list) -> None:
        self._subs = subs

    def list_subscriptions(self, group_openid: str) -> list:
        return [s for s in self._subs if s.group_openid == group_openid]


async def _staged(*, veto: bool, real_due: bool = True):
    """跑一遍三阶段，返回 (poller, 轮询顺序)。

    **`due` 里默认只放正式群** —— 这才是线上真实情况：三个订阅虽然间隔一样，
    但轮询时刻相差 90 秒（07:25:37 / 07:26:08 / 07:27:38），永远不会落在
    同一个 `due` 批次里。靠 `due` 对齐的写法会一次都不触发。
    """
    cfg = _dc.replace(
        cfg_stub(), warmup_group="W", preview_group="P",
        preview_warmup_sec=0, preview_veto_sec=0.4,
    )
    all_subs = [
        _mk_sub(1, "W"), _mk_sub(2, "P"),
        # 预热群里订阅了**别的源** —— 不该被拉起来预热，预热它对正式群毫无意义
        _mk_sub(5, "W", url="http://other/feed"),
        _mk_sub(3, "REAL-A"), _mk_sub(4, "REAL-B"),
    ]
    poller = FeedPoller(cfg=cfg, db=_FakeDB(all_subs), http=None, pusher=None)
    order: list = []

    async def fake_poll_one(sub):
        order.append(sub.group_openid)
        if sub.group_openid == "P":
            # 预览这条真发出去了 —— 阶段三靠它算出「这一批是哪些 key」
            poller._last_pushed[sub.id] = ["k1", "k2"]
        await asyncio.sleep(0)
        return True

    poller._poll_one = fake_poll_one

    task = None
    if veto:
        async def holler():
            # 等预览窗口真开了再喊，模拟「你看到预览之后才决定」
            for _ in range(500):
                if poller.veto_current():
                    return
                await asyncio.sleep(0.01)

        task = asyncio.create_task(holler())

    if real_due:
        due = [s for s in all_subs if s.group_openid.startswith("REAL")]
    else:
        due = [s for s in all_subs if s.group_openid in ("W", "P")]
    await poller._poll_staged(due)
    if task is not None:
        task.cancel()
    return poller, order


_p_ok, _order_ok = asyncio.run(_staged(veto=False))
check("★ 正式群到点时，预热群/预览群被**主动拉起来**（它们自己在 due 里排不上）",
      _order_ok[:2] == ["W", "P"], str(_order_ok))
check("★ 顺序：预热群 → 预览群 → 正式群（并发发下去预热就白做了）",
      _order_ok == ["W", "P", "REAL-A", "REAL-B"], str(_order_ok))
check("★ 预热群里订阅的**别的源**不许被拉进来",
      "W" not in _order_ok[2:] and _order_ok.count("W") == 1, str(_order_ok))
check("没喊停 → 一条都不挡", _p_ok._vetoed == {}, str(_p_ok._vetoed))

_p_no, _order_no = asyncio.run(_staged(veto=True))
check("★ 喊「不发」→ 正好记下预览那批 key",
      set(_p_no._vetoed) == {"k1", "k2"}, str(_p_no._vetoed))
check("★ 喊停只挡那几条，正式群阶段照常跑",
      set(_order_no[2:]) == {"REAL-A", "REAL-B"}, str(_order_no))

_p_idle, _order_idle = asyncio.run(_staged(veto=False, real_due=False))
check("★ 只有预热群/预览群到点时**什么都不发**"
      "（让它们自己先跑一遍，正式群到点时就预热不出东西了）",
      _order_idle == [], str(_order_idle))

_special2 = _dc.replace(cfg_stub(), warmup_group="W", preview_group="P")
check("★ 只配了群、due 里只有它们自己 → 也不单独跑",
      asyncio.run(_staged(veto=False, real_due=False))[1] == [])


async def _veto_outside() -> bool:
    poller = FeedPoller(cfg=_special2, db=None, http=None, pusher=None)
    return poller.veto_current()


check("★ 预览窗口没开时喊「不发」不生效（防止随口一句吞掉下一条推送）",
      asyncio.run(_veto_outside()) is False)


class _RecorderPusher:
    def __init__(self) -> None:
        self.texts: list = []
        self.rich: list = []
        self.markdowns: list = []

    async def send_text(self, g, c):
        self.texts.append((g, c))

    async def send_rich(self, g, c, u):
        self.rich.append((g, c, u))

    async def send_markdown(self, g, c):
        self.markdowns.append((g, c))

    async def upload_image(self, g, u):
        return True


async def _vetoed_skip():
    cfg = _dc.replace(cfg_stub(), imas_tags=False, attach_images=False)
    pusher = _RecorderPusher()
    poller = FeedPoller(cfg=cfg, db=None, http=None, pusher=pusher)
    poller._vetoed = {"KEY-OF-VETOED": time.monotonic()}
    entry = Entry(key="KEY-OF-VETOED", title="被否掉的那条",
                  link="https://x", published=None, summary="")
    pushed = await poller._push_entries(_mk_sub(3, "REAL-A"), "源", [entry])
    return pushed, pusher


_pushed, _pusher = asyncio.run(_vetoed_skip())
check("★ 被「不发」挡下的条目不进正式群（一条消息都不发）",
      _pusher.texts == [] and _pusher.rich == [] and _pusher.markdowns == [],
      f"text={len(_pusher.texts)} rich={len(_pusher.rich)} md={len(_pusher.markdowns)}")
check("★ 但它要当「已推」返回，否则下一轮又捞出来，你得每轮否一次",
      [e.key for e in _pushed] == ["KEY-OF-VETOED"], str([e.key for e in _pushed]))


print("[4.64] ASOBI TICKET 抽票提醒（全离线）")

from datetime import datetime, timedelta, timezone  # noqa: E402
from qq_rss_bot import tickets as _tk  # noqa: E402

_TK_JST = _tk.JST
# 用一条真实的截止时间当基准：2026-11-15 23:59 JST
_TK_END = datetime(2026, 11, 15, 23, 59, tzinfo=_TK_JST)


def _tk_rec(**kw):
    base = dict(
        id="r1", booth_slug="va-liv-3rdlive", booth_name="活动名",
        name="アソビストア一般会員先行", entry_type="lottery",
        status="within_entry_period",
    )
    base.update(kw)
    return _tk.Reception(**base)


def _tk_payload():
    """一小段**真实结构**的 JSON:API —— 层级和字段名都照抄实测的响应。"""
    return {
        "data": [
            {
                "id": "rec-1", "type": "reception",
                "attributes": {
                    "name": "アソビストア一般会員先行",
                    "entry_type": "lottery",
                    "entry_period_status": "within_entry_period",
                    "entry_period_starts_at": "2026-10-17T21:30:00.000+09:00",
                    "entry_period_ends_at": "2026-11-15T23:59:59.999+09:00",
                    "result_announcement_scheduled_at": "2026-11-28T13:00:00.000+09:00",
                    "deposit_period_starts_at": "2026-11-28T13:00:00.000+09:00",
                    "deposit_period_ends_at": "2026-12-02T23:59:59.999+09:00",
                },
                "relationships": {
                    "tour": {"data": {"id": "t1", "type": "tour"}},
                    "cover_image": {"data": {"id": "img-1", "type": "image"}},
                },
            },
            # 缺 tour（关系是 null）—— 不能让整条炸
            {
                "id": "rec-2", "type": "reception",
                "attributes": {"name": "关系缺失", "entry_period_status": "before_entry_period"},
                "relationships": {"tour": {"data": None}},
            },
            "这一项根本不是 dict，也要跳过",
        ],
        "included": [
            {
                "id": "t1", "type": "tour", "attributes": {"name": "巡演名"},
                "relationships": {"booth": {"data": {"id": "b1", "type": "booth"}}},
            },
            {
                "id": "b1", "type": "booth",
                "attributes": {"name": "PROJECT IM@S vα-liv 3rdLIVE", "slug": "va-liv-3rdlive"},
            },
            {
                "id": "img-1", "type": "image",
                "attributes": {"reading_urls": {"medium": "https://cdn/x.png.800",
                                                "small": "https://cdn/x.png.150"}},
            },
        ],
    }


def _tk_booths_payload():
    """booth 列表。**每场 live 的封面在这里**，结构照抄实测响应。"""
    return {
        "data": [
            {"id": "b1", "type": "booth",
             "attributes": {"slug": "va-liv-3rdlive", "name": "PROJECT IM@S vα-liv 3rdLIVE"},
             "relationships": {"cover_image": {"data": {"id": "bc-1", "type": "image"}}}},
            # 这场站点没配封面 —— 不能因此炸掉，只是不配图
            {"id": "b2", "type": "booth",
             "attributes": {"slug": "sidem-270313", "name": "SideM"},
             "relationships": {"cover_image": {"data": None}}},
            # 关系整个缺失，也要跳过
            {"id": "b3", "type": "booth", "attributes": {"slug": "gkmas-1"}},
            "不是 dict，跳过",
        ],
        "included": [
            {"id": "bc-1", "type": "image",
             "attributes": {"reading_urls": {"medium": "https://cdn/booth.png.800",
                                             "small": "https://cdn/booth.png.150"}}},
        ],
    }


def _check_tk_brand() -> None:
    """★ 品牌判据。**这组钉子钉死「不能改成按活动名匹配」**。"""
    imas_slugs = [
        "va-liv-3rdlive", "cinderellagirls-261128", "cinderellagirls-270111",
        "cinderellagirls-270213", "cinderellagirls-millionlive-20270227",
        "gkmas-261107", "idolmaster-261212", "shinycolors-261219",
        "shinycolors-270322", "shinycolors-270327", "sidem-261226",
        "sidem-270313", "sidem-shinycolors-270605", "765pro-961pro-270724",
    ]
    for slug in imas_slugs:
        check(f"★ 认得 {slug}", _tk.is_imas(slug), slug)

    for slug in ("tales-261103", "denonbu-270220", "zerokyori-261024"):
        check(f"★ 剔掉非 IM@S 的 {slug}", not _tk.is_imas(slug), slug)

    # 三个「名字里根本没有 IDOLM@STER 但确实是 IM@S」的 —— 名字匹配会判错，
    # slug 才是站点自己打的品牌标。
    for slug, why in (
        ("idolmaster-261212", "xR LIVE REVIVAL"),
        ("cinderellagirls-270213", "ATHOME xR LIVE Apple of my アイ"),
        ("shinycolors-261219", 'shiny, the first REFRAC7IONS " Still blue "'),
    ):
        check(f"★ 「{why}」名字里没有 IDOLM@STER，但要认（靠 {slug}）",
              _tk.is_imas(slug), slug)

    check("★ 空 slug 不算", not _tk.is_imas("") and not _tk.is_imas(None))
    check("★ 前缀要凑成完整品牌段（不让 millionlive 误收 millionlivex-）",
          not _tk.is_imas("millionlivex-1") and _tk.is_imas("millionlive-1"))
    check("自定义品牌表能覆盖默认", _tk.is_imas("tales-1", ("tales",)))

    check("filter_imas 只留该留的",
          [r.booth_slug for r in _tk.filter_imas(
              [_tk_rec(booth_slug="va-liv-a"), _tk_rec(booth_slug="tales-b")])
           ] == ["va-liv-a"])


def _check_tk_parse() -> None:
    recs = _tk.parse(_tk_payload())
    check("★ 解析出 2 条（非 dict 那条被跳过）", len(recs) == 2, [r.id for r in recs])

    r = next(x for x in recs if x.id == "rec-1")
    check("★ tour → booth 两跳关联出活动名和 slug",
          r.booth_name == "PROJECT IM@S vα-liv 3rdLIVE" and r.booth_slug == "va-liv-3rdlive",
          f"{r.booth_name!r} {r.booth_slug!r}")
    check("★ 详情页 URL 由 slug 拼出来",
          r.url == "https://asobiticket2.asobistore.jp/booths/va-liv-3rdlive", r.url)
    check("★ 时间全部带上 JST 时区",
          r.entry_end == datetime(2026, 11, 15, 23, 59, 59, 999000, tzinfo=_TK_JST),
          repr(r.entry_end))
    check("★ 封面档次解出来了", r.cover.get("medium") == "https://cdn/x.png.800")
    check("★ 抽選/先着的标", r.kind_label == "抽選" and not _tk.is_imas("") and r.lottery)

    bad = next(x for x in recs if x.id == "rec-2")
    check("★ tour 关系为 null 时不炸，slug 退化成空", bad.booth_slug == "", bad.booth_slug)
    check("★ 缺的日期字段是 None，不是抛异常",
          bad.entry_end is None and bad.result_at is None and bad.deposit_end is None)

    check("空 payload 不炸", _tk.parse({}) == [])


def _check_tk_alerts() -> None:
    """★ 提醒判定 —— 这个功能的核心，必须离线可测。"""
    at = lambda h: _TK_END - timedelta(hours=h)  # noqa: E731
    cases = [
        ("距截止 30 小时（窗口外）", at(30), set(), ["new"]),
        ("距截止 23 小时（刚进 24h 窗口）", at(23), set(), ["new", "entry24"]),
        ("距截止 3 小时（在 24h 内、未进 2h）", at(3), set(), ["new", "entry24"]),
        ("★ 首次见到就已进 2h 窗口 → 只发 entry2，不补发 entry24", at(1), set(), ["new", "entry2"]),
        ("★ 同上但 new 已发过", at(1), {"new"}, ["entry2"]),
        ("★ 同上但 24h 也发过了（不能因为补发而重复）", at(1), {"new", "entry24"}, ["entry2"]),
        ("new 发过、还在 24h 窗口 → 只发 entry24", at(3), {"new"}, ["entry24"]),
        ("★ 已过截止 → 什么都不发", at(-1), set(), []),
        ("★ 已过截止但接口仍说受付中 → 也不发（状态和日期打架时不推）",
         at(-1), set(), []),
    ]
    for label, now, seen, expect in cases:
        got = _tk.due_alerts(_tk_rec(entry_end=_TK_END), now, seen)
        check(label, got == expect, f"got={got} expect={expect}")

    check("★ 入金日期为 null 时不发入金那两条",
          _tk.due_alerts(_tk_rec(entry_end=_TK_END, deposit_end=None), at(1), set())
          == ["new", "entry2"])

    both = _tk_rec(entry_end=_TK_END, deposit_end=_TK_END)
    check("★ 申込和入金各自独立计数",
          _tk.due_alerts(both, at(1), {"new", "entry2"}) == ["deposit2"],
          _tk.due_alerts(both, at(1), {"new", "entry2"}))

    check("已结束的受付什么都不发",
          _tk.due_alerts(_tk_rec(entry_end=_TK_END - timedelta(days=1),
                                 status="after_entry_period"), _TK_END, set()) == [])

    # ★ 时区：now 用 UTC 构造，判定必须一致。服务器 TZ 是 Asia/Shanghai，
    #   直接拿本地时间去比 JST 会差一小时 —— 差一小时就是错过。
    utc_now = at(1).astimezone(timezone.utc)
    check("★ 时区：UTC 构造的 now 与 JST 结果一致",
          _tk.due_alerts(_tk_rec(entry_end=_TK_END), utc_now, set()) == ["new", "entry2"],
          repr(utc_now))
    check("★ now 与 end 都换到 UTC 后比较仍然成立",
          _tk.due_alerts(_tk_rec(entry_end=_TK_END.astimezone(timezone.utc)), utc_now, set())
          == ["new", "entry2"])


def _check_tk_render() -> None:
    r = _tk_rec(entry_start=_TK_END - timedelta(days=30), entry_end=_TK_END,
                result_at=_TK_END + timedelta(days=13),
                deposit_end=_TK_END + timedelta(days=17))
    text = _tk.render_new(r)
    check("★ 新受付文案带受付期間/当落发表/入金期間三行",
          "受付期間：" in text and "当落发表：" in text and "入金期間：" in text, text)
    # ★ 排版照群友那个工具：`2026年11月15日(日) 22:59`，月/日不补零、时分补零。
    #   注意这是**北京时间** —— 接口给的是 23:59+09:00，同一个时刻。
    check("★ 日期是北京时间的长格式（月日不补零）",
          "2026年11月15日(日) 22:59" in text, text)
    check("★ 受付期間带星期，当落发表不带",
          "2026年10月16日(金) 22:59 〜 2026年11月15日(日) 22:59" in text
          and "当落发表：2026年11月28日 22:59" in text, text)
    check("★ 带详情页链接", r.url in text and "[在 ASOBI TICKET 打开]" in text)
    check("★ 抽選标出来了", "抽選" in text)

    d = _tk.render_deadline(r, "entry2", _TK_END - timedelta(hours=1))
    check("★ 締切文案带「申込締切」和剩余时间",
          "申込締切" in d and "あと" in d, d)
    check("★ 締切文案标了北京时间", "北京时间" in d, d)
    # ★ 尾数 24 / 2 位数不同：`entry2` 必须解析成申込、`deposit2` 必须解析成入金。
    #   写成 kind[:-2] 的话 entry2 会静默退到入金日期 —— 推出去是一条日期错的締切。
    check("★ entry2 用的是申込日期，不是入金日期",
          "申込締切" in d and "2026年11月15日 22:59" in d, d)
    d24 = _tk.render_deadline(r, "entry24", _TK_END - timedelta(hours=23))
    check("★ entry24 也用申込日期", "申込締切" in d24 and "2026年11月15日 22:59" in d24, d24)
    dep = _tk.render_deadline(r, "deposit2", _TK_END - timedelta(hours=1))
    check("★ deposit2 用的是入金日期", "入金締切" in dep and "2026年12月2日 22:59" in dep, dep)
    dep24 = _tk.render_deadline(r, "deposit24", _TK_END - timedelta(hours=23))
    check("★ deposit24 也用入金日期", "入金締切" in dep24 and "2026年12月2日 22:59" in dep24, dep24)

    # 活动名里有 markdown 元字符时不能破坏结构
    nasty = _tk_rec(booth_name="A [B] (C) | D * E _ F", name="受付【SP席】")
    for kind in ("new", "entry24"):
        out = _tk.render_alert(nasty, kind, _TK_END - timedelta(hours=1))
        check(f"★ {kind} 在活动名含 []()|*_ 时不产生未闭合的 markdown",
              out.count("[") == out.count("]") and out.count("(") == out.count(")"), out)

    # ---- 列表排版 ----
    async def run() -> None:
        class _Http:
            def __init__(self, ok=True):
                self.ok = ok

            async def get(self, url, **kw):
                if not self.ok:
                    raise RuntimeError("boom")

                class _R:
                    status_code = 200
                    content = _png(800, 450)

                return _R()

        now = _TK_END - timedelta(hours=1)
        cov = {"medium": "https://cdn/a.png.800"}
        _T1 = "**1. 同一场**"
        _T2 = "**2. 另一场**"
        _L1 = "[在 ASOBI TICKET 打开](https://asobiticket2.asobistore.jp/booths/va-liv-3rdlive)"
        two = [
            _tk_rec(id="a1", booth_name="同一场", name="【SP席】先行", cover=cov,
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
            _tk_rec(id="a2", booth_name="同一场", name="【S席】先行", cover=cov,
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
            _tk_rec(id="b1", booth_slug="shinycolors-270322", booth_name="另一场",
                    name="一般先行", cover={"medium": "https://cdn/b.png.800"},
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
        ]
        out = await _tk.render_digest(_Http(), two, now, wanted=("open",), images=False)

        check("★ 列表头是「更新于 …（北京时间）」",
              out.startswith("🎫 更新于 ") and "（北京时间）" in out.splitlines()[0], out)
        check("★ 节标题带场数，不是受付条数，且加粗",
              "**抽选中（2 场）**" in out, out)
        check("★ 场按序号排，序号重新从一开始",
              _T1 in out and _T2 in out, out)
        check("★ 每场下面有一行链接，指向这场的受付页", _L1 in out, out)
        check("★ 链接行在正文之后（场名 → 受付 → 日期 → 链接）",
              out.index(_T1) < out.index(_L1)
              and out.index("当落发表：") < out.index(_L1), out)
        check("★ 同一场的两个受付合成一条，用全角空格行隔开",
              out.count("1. 同一场") == 1 and f"\n{_tk.SEP}\n" in out, out)
        check("★ 受付中那节同时给受付期間和当落发表",
              "受付期間：" in out and "当落发表：" in out, out)
        check("★ 场名里的全角空格原样保留（ATHOME xR LIVE　Apple of my アイ）",
              "同一场" in out, out)

        # 图片：插在该场序号**上面**，不是消息末尾
        with_img = await _tk.render_digest(
            _Http(), two, now, wanted=("open",), images=True, cache={})
        lines = with_img.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.startswith("**1. 同一场"))
        check("★ 封面图在该场序号上面一行",
              lines[idx - 1].startswith("![图1 #208px #117px]"), lines[max(0, idx - 2):idx + 1])
        check("★ 第二场也有自己的图（不是挤在末尾）",
              any(ln.startswith("![图2 ") for ln in lines), lines)

        no_img = await _tk.render_digest(_Http(), two, now, wanted=("open",), images=False)
        check("★ images=False 时一个 ! 都不出现", "![" not in no_img)

        # ---- booth 级封面：这场受付自己没封面也要有图 ----
        #   这正是用户报的「有些图片根本没写到文本里去」：va-liv 那场 3 条受付
        #   一条都没带 cover_image，于是整场没图 —— 而站点明明配了主视觉。
        booth_cov = {
            "va-liv-3rdlive": {"medium": "https://cdn/live-a.png.800"},
            "shinycolors-270322": {"medium": "https://cdn/live-b.png.800"},
        }
        bare = [
            _tk_rec(id="n1", booth_name="没受付封面的一场", entry_end=_TK_END,
                    result_at=_TK_END + timedelta(days=13)),
        ]
        got = await _tk.render_digest(_Http(), bare, now, wanted=("open",), booths=booth_cov)
        check("★ 受付自己没有封面，但这场 live 有 → 照样配图",
              "![图1 #208px #117px](https://cdn/live-a.png.800)" in got, got)

        # ★ 同一场的图**不能取决于哪条受付排在最前面**。
        #   两条受付各带一张不同的受付级封面 —— 图必须还是这场 live 那张，
        #   而且只有一张。
        mixed_covers = [
            _tk_rec(id="m1", booth_name="同一场", name="【A】", cover={"medium": "https://cdn/r1.png.800"},
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
            _tk_rec(id="m2", booth_name="同一场", name="【B】", cover={"medium": "https://cdn/r2.png.800"},
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
        ]
        stable = await _tk.render_digest(_Http(), mixed_covers, now, wanted=("open",), booths=booth_cov)
        check("★ 同一场只配一张图，且是这场 live 的（与受付排序无关）",
              stable.count("![图") == 1 and "live-a.png.800" in stable
              and "r1.png.800" not in stable and "r2.png.800" not in stable, stable)

        # ★ 场数超过 `multiimage.MAX_IMAGES`（6）时**不能从第 7 场开始没图**。
        #   列表的图是「每场一张」，和「一篇资讯最多 6 张图」不是一回事；
        #   用错那个数卡出来的症状正是用户报的「有些图片根本没写进文本里」。
        many = [
            _tk_rec(id=f"x{i}", booth_slug=f"va-liv-{i}", booth_name=f"第{i}场",
                    entry_end=_TK_END, result_at=_TK_END + timedelta(days=13))
            for i in range(1, 9)
        ]
        many_cov = {f"va-liv-{i}": {"medium": f"https://cdn/live{i}.png.800"} for i in range(1, 9)}
        eight = await _tk.render_digest(_Http(), many, now, wanted=("open",), booths=many_cov)
        check("★ 8 场就配 8 张图（不受 multiimage.MAX_IMAGES=6 的卡）",
              eight.count("![图") == 8, f"{eight.count('![图')} 张 / 共 {len(many)} 场")
        check("★ 第 7、8 场也有图，不是从第 7 场开始没了",
              "live7.png.800" in eight and "live8.png.800" in eight,
              [ln for ln in eight.splitlines() if ln.startswith("![")][-2:])
        check("★ 列表的图片预算就是场数上限（每场都有图）",
              _tk.MAX_COVERS == _tk.LIST_LIMIT, _tk.MAX_COVERS)

        # ---- 场名下面那行链接 ----
        # 活动名里**真的出现过方括号**（smoke 一直用 `A [B] (C) | D * E _ F`）。
        # 链接是**单独一行**的，所以名字里的括号不会跑进链接语法里 ——
        # 这正是当初不把链接挂在场名上的原因之一。
        bracketed = [_tk_rec(id="bk", booth_name="A [B] (C) | D * E _ F",
                             entry_end=_TK_END, result_at=_TK_END + timedelta(days=13))]
        btxt = await _tk.render_digest(
            _Http(), bracketed, now, wanted=("open",), images=False)
        blines = btxt.splitlines()
        name_line = next(ln for ln in blines if ln.startswith("**1."))
        link_line = next(ln for ln in blines if ln.startswith("["))
        # 名字里的括号**只出现在加粗那一行**：链接行是单独一行、写死的文案，
        # 所以名字里有多少括号都破坏不了链接语法。
        check("★ 名字里的方括号留在加粗行里，不会跑进链接语法",
              "[B]" in name_line and link_line.count("[") == 1
              and link_line.count("]") == 1 and "[B]" not in link_line,
              f"{name_line!r} / {link_line!r}")
        check("★ 链接行是干净的 `[文字](url)`，且指向这场真正的 slug",
              link_line == "[在 ASOBI TICKET 打开]"
                           "(https://asobiticket2.asobistore.jp/booths/va-liv-3rdlive)",
              link_line)

        # `tour` 关系为 null 的存量数据 slug 是空的 —— 拼出来是 `…/booths/`，点了 404
        noslug = [_tk_rec(id="ns", booth_slug="", booth_name="关系缺失的场",
                          entry_end=_TK_END, result_at=_TK_END + timedelta(days=13))]
        ntxt = await _tk.render_digest(
            _Http(), noslug, now, wanted=("open",), images=False)
        check("★ 没有 slug 时干脆不加链接行（否则点进去是 404）",
              "**1. 关系缺失的场**" in ntxt and "](" not in ntxt, ntxt)

        # 传了 booths 但这场不在表里 → 退回受付级封面，不是没图
        fallback = await _tk.render_digest(
            _Http(), two, now, wanted=("open",), booths={"别的场": {"medium": "https://cdn/z.png.800"}})
        check("★ booth 表里没有这场时退回受付级封面",
              "![图1 #208px #117px](https://cdn/a.png.800)" in fallback, fallback)

        # 读不到尺寸 → 那一场不配图，但文字照出（不猜比例）
        broken = await _tk.render_digest(_Http(ok=False), two, now, wanted=("open",))
        check("★ 读不到尺寸就那场不配图，文字照出",
              "![" not in broken and _T1 in broken, broken)

        # 没有封面 URL 的场也不配图
        nocover = await _tk.render_digest(
            _Http(), [_tk_rec(id="c1", entry_end=_TK_END)], now, wanted=("open",))
        check("★ 这场本来就没封面 → 不配图，也不报错", "![" not in nocover, nocover)

        # ---- 空列表要说人话 ----
        empty_open = await _tk.render_digest(_Http(), [], now, wanted=("open",))
        empty_pend = await _tk.render_digest(_Http(), [], now, wanted=("pending", "deposit"))
        check("★ 没有受付时 /抽选 回一句人话，不是空消息",
              "没有" in empty_open and "抽选" in empty_open, empty_open)
        check("★ 没有待当落时 /当落 回一句人话",
              "没有" in empty_pend and "待当落" in empty_pend, empty_pend)

        # ---- 两节各自取该取的 ----
        mixed = [
            _tk_rec(id="o1", entry_end=_TK_END, result_at=_TK_END + timedelta(days=13)),
            _tk_rec(id="p1", booth_slug="sidem-1", booth_name="已截止",
                    status="after_entry_period", entry_end=_TK_END - timedelta(days=2),
                    result_at=_TK_END + timedelta(days=1)),
            _tk_rec(id="d1", booth_slug="gkmas-1", booth_name="要付钱",
                    status="after_entry_period", entry_end=_TK_END - timedelta(days=5),
                    result_at=_TK_END - timedelta(days=1),
                    deposit_start=_TK_END - timedelta(days=1),
                    deposit_end=_TK_END + timedelta(days=1)),
        ]
        pend = await _tk.render_digest(_Http(), mixed, now, wanted=("pending", "deposit"),
                                       images=False)
        check("★ /当落 两节都出，待当落在前",
              pend.index("待当落（1 场）") < pend.index("入金中（1 场）"), pend)
        check("★ 待当落只列当落未到的", "已截止" in pend and "要付钱" not in pend.split("入金中")[0], pend)
        check("★ 入金中给的是入金期間", "入金期間：" in pend, pend)

        open_only = await _tk.render_digest(_Http(), mixed, now, wanted=("open",),
                                            images=False)
        check("★ /抽选 不带待当落/入金中那两节",
              "抽选中" in open_only and "待当落" not in open_only
              and "入金中" not in open_only, open_only)

        # 已结束的受付不能出现在 /抽选
        closed = await _tk.render_digest(
            _Http(), [_tk_rec(id="x", status="after_entry_period", entry_end=_TK_END)],
            now, wanted=("open",))
        check("★ /抽选 不列已结束的", "没有" in closed, closed)

        # 纯文本兜底：图片行要去掉
        stripped = _tk.strip_images(with_img)
        check("★ strip_images 去掉图片行、留下正文",
              "![" not in stripped and _T1 in stripped, stripped)

        # ---- 渲染宽度（用户反馈「图没占满气泡」）----
        wide = await _tk.render_digest(
            _Http(), two, now, wanted=("open",), cache={}, width=320)
        check("★ width 透传到尺寸提示（不是恒定的 208）",
              "![图1 #320px #180px]" in wide, wide.splitlines()[:4])
        check("★ 不传 width 时还是老的 208（bilibili/RSS 那两条路不能受影响）",
              "![图1 #208px #117px]" in with_img)

        # ---- 尺寸读失败不能缓存住 ----
        class _Flaky:
            """第一次失败、第二次成功。用来钉「失败的尺寸不进缓存」。"""

            def __init__(self):
                self.calls = 0

            async def get(self, url, **kw):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("临时抖动")

                class _R:
                    status_code = 200
                    content = _png(800, 450)

                return _R()

        flaky, fcache = _Flaky(), {}
        one = [_tk_rec(id="f1", cover=cov, entry_end=_TK_END)]
        first = await _tk.render_digest(flaky, one, now, wanted=("open",), cache=fcache)
        second = await _tk.render_digest(flaky, one, now, wanted=("open",), cache=fcache)
        check("★ 尺寸读失败不进缓存（否则一次抖动 = 这张图永久消失）",
              "![" not in first and "![图1 " in second, f"{first[:20]!r} / {second[:20]!r}")

        # ---- 丢了图要有日志：和「接口本来就没封面」必须能区分 ----
        import logging as _logging

        class _Cap(_logging.Handler):
            def __init__(self):
                super().__init__()
                self.rows: list[tuple[int, str]] = []

            def emit(self, record):
                self.rows.append((record.levelno, record.getMessage()))

        cap = _Cap()
        _tk._log.addHandler(cap)
        _tk._log.setLevel(_logging.DEBUG)
        try:
            await _tk.render_digest(_Http(ok=False), two, now, wanted=("open",))
            await _tk.render_digest(
                _Http(), [_tk_rec(id="c1", entry_end=_TK_END)], now, wanted=("open",))
        finally:
            _tk._log.removeHandler(cap)
        joined = " ".join(m for _, m in cap.rows)
        check("★ 尺寸读失败要打 WARNING（原来这里是静默的）",
              any(lv == _logging.WARNING and "读不到尺寸" in m for lv, m in cap.rows),
              cap.rows)
        check("★ 「两个来源都没封面」是另一条日志，和读不到尺寸能区分",
              "这场两个来源都没封面" in joined, joined[:200])

    asyncio.run(run())

    # ★ 时区：显示用北京时间，但**判定必须还是日本时间**。
    #   这条钉住「别把 now() 一起改成 CST」—— 那会让所有判定整体平移一小时。
    utc_now = (_TK_END - timedelta(hours=1)).astimezone(timezone.utc)
    cst_now = (_TK_END - timedelta(hours=1)).astimezone(_tk.CST)
    check("★ 同一时刻，JST / CST / UTC 三种构造的判定完全一致",
          _tk.due_alerts(_tk_rec(entry_end=_TK_END), utc_now, set())
          == _tk.due_alerts(_tk_rec(entry_end=_TK_END), cst_now, set())
          == ["new", "entry2"],
          repr(utc_now))
    check("★ 显示时区是 CST（+08:00）", _tk.CST.utcoffset(None) == timedelta(hours=8))
    # 加粗前要剥掉会破坏 `**…**` 的字符（和 bilibili 同一套）
    check("★ 加粗前剥掉 *_`~\\",
          _tk._md("A*B_C`D~E\\F") == "ABCDEF", _tk._md("A*B_C`D~E\\F"))
    check("★ 宽度参数在 multiimage 层也生效",
          _mi.image_line(1, "https://a/b.jpg", (1000, 563), 320)
          == "![图1 #320px #180px](https://a/b.jpg)")
    check("★ 不传宽度时 multiimage 维持 208（老的两条路）",
          _mi.image_line(1, "https://a/b.jpg", (1000, 563))
          == "![图1 #208px #117px](https://a/b.jpg)")
    check("★ 判定时区还是 JST（+09:00）", _tk.JST.utcoffset(None) == timedelta(hours=9))


def _check_tk_cover() -> None:
    class _Resp:
        status_code = 200

        def __init__(self, content: bytes) -> None:
            self.content = content

    class _Http:
        """假客户端。`payload=None` 时模拟「读不到尺寸」。"""

        def __init__(self, payload: bytes | None) -> None:
            self.payload = payload
            self.calls = 0

        async def get(self, url, **kw):
            self.calls += 1
            if self.payload is None:
                raise RuntimeError("boom")
            return _Resp(self.payload)

    rec = _tk_rec(cover={"medium": "https://cdn/x.png.800", "small": "https://cdn/x.png.150"})

    check("★ 按档位取 URL", _tk.cover_url(rec, "small") == "https://cdn/x.png.150")
    check("★ 档位缺失时退到默认档",
          _tk.cover_url(_tk_rec(cover={"medium": "m"}), "original") == "m")
    check("没有封面返回 None", _tk.cover_url(_tk_rec(), "medium") is None)

    # ---- booth 级封面（每场 live 的主视觉）----
    booth_cov = {"va-liv-3rdlive": {"medium": "https://cdn/booth.png.800"}}
    check("★ 受付自己没有封面时，用这场 live 的封面",
          _tk.cover_url(_tk_rec(), "medium", booth_cov) == "https://cdn/booth.png.800",
          _tk.cover_url(_tk_rec(), "medium", booth_cov))
    check("★ 两个来源都有时**优先 booth 级**（它是这场的主视觉，受付级只是单条受付的横幅）",
          _tk.cover_url(rec, "medium", booth_cov) == "https://cdn/booth.png.800",
          _tk.cover_url(rec, "medium", booth_cov))
    check("booth 表里没有这场时退回受付级",
          _tk.cover_url(rec, "medium", {"别的场": {"medium": "z"}}) == "https://cdn/x.png.800")
    check("booth 表整个拉不到（空 dict）也能退回受付级",
          _tk.cover_url(rec, "medium", {}) == "https://cdn/x.png.800")
    check("传 None 和不传一样",
          _tk.cover_url(rec, "medium", None) == _tk.cover_url(rec, "medium"))

    # booth 列表地址跟着 TICKET_URL 推，推不出来退回默认
    check("★ 从 receptions 地址推出 booths 地址",
          _tk.booths_url("https://x/api/v1/public/receptions") == "https://x/api/v1/public/booths")
    check("★ 推不出来就用默认地址（别拼出个畸形 URL）",
          _tk.booths_url("https://x/别的路径") == _tk.DEFAULT_BOOTHS, _tk.booths_url("https://x/别的路径"))
    check("空字符串也不炸", _tk.booths_url("") == _tk.DEFAULT_BOOTHS)

    parsed = _tk.parse_booths(_tk_booths_payload())
    check("★ booth 列表解出 slug → 档位 URL",
          parsed == {"va-liv-3rdlive": {"medium": "https://cdn/booth.png.800",
                                        "small": "https://cdn/booth.png.150"}}, parsed)
    check("★ 没配封面 / 关系缺失 / 非 dict 的 booth 都不进表，也不炸",
          "sidem-270313" not in parsed and "gkmas-1" not in parsed, parsed)

    async def run() -> None:
        http = _Http(_png(800, 450))
        out = await _tk.with_cover(http, rec, "正文", "medium")
        check("★ 封面拼在最上面，且带尺寸提示（手机端缺了整条不渲染）",
              out.startswith("![图1 #208px #117px](https://cdn/x.png.800)\n正文"), out)
        check("★ URL 原样，没加任何 query 参数（加了 QQ 就抓不到）",
              "?" not in out.split("](")[1].split(")")[0], out)

        cache: dict = {}
        await _tk.with_cover(http, rec, "正文", "medium", cache)
        before = http.calls
        await _tk.with_cover(http, rec, "正文", "medium", cache)
        check("★ 尺寸只读一次（进程内缓存）", http.calls == before)

        broken = _Http(None)
        out2 = await _tk.with_cover(broken, rec, "正文", "medium")
        check("★ 读不到尺寸 → 丢图发纯文字（不猜比例）", out2 == "正文", out2)

        no_cover = await _tk.with_cover(_Http(_png(10, 10)), _tk_rec(), "正文", "medium")
        check("没有封面也是纯文字", no_cover == "正文")

        # 受付级没封面、booth 级有 —— 提醒那条路也要能配上图
        with_booth = await _tk.with_cover(
            _Http(_png(800, 450)), _tk_rec(), "正文", "medium", None,
            _tk.DEFAULT_IMAGE_W, {"va-liv-3rdlive": {"medium": "https://cdn/booth.png.800"}})
        check("★ 受付级没封面时，提醒也能用 booth 级封面",
              with_booth.startswith("![图1 #320px #180px](https://cdn/booth.png.800)\n正文"),
              with_booth)

    asyncio.run(run())


def _check_tk_feed() -> None:
    """TicketFeed 的缓存和 304 处理。"""
    payload = _tk_payload()

    class _Resp:
        def __init__(self, status, body=None, etag=None):
            self.status_code = status
            self._body = body
            self.headers = {"etag": etag} if etag else {}

        def json(self):
            return self._body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class _Http:
        def __init__(self):
            self.calls = []
            self.urls = []
            self.etag = '"v1"'

        async def get(self, url, headers=None, **kw):
            self.calls.append(dict(headers or {}))
            self.urls.append(url)
            if (headers or {}).get("If-None-Match") == self.etag:
                return _Resp(304)
            # 两条接口各自的载荷 —— 封面在 booths 里，不在 receptions 里
            body = _tk_booths_payload() if url.endswith("/booths") else payload
            return _Resp(200, body, self.etag)

    async def run() -> None:
        cfg = cfg_stub()
        cfg = dataclasses.replace(cfg, ticket_url=_tk.DEFAULT_API)
        http = _Http()
        feed = _tk.TicketFeed(cfg, http)

        first = await feed.receptions(max_age=0.0)
        check("★ 首次拉到 2 条", len(first) == 2, len(first))
        check("★ 首次不带 If-None-Match", "If-None-Match" not in http.calls[0])

        again = await feed.receptions(max_age=0.0)
        check("★ 第二次带 If-None-Match 且收到 304 后仍返回上次的缓存",
              "If-None-Match" in http.calls[1] and len(again) == 2,
              f"{http.calls[1]} {len(again)}")

        cached = await feed.receptions(max_age=60.0)
        check("★ 缓存期内不再发请求（群里连点不会刷爆接口）",
              len(http.calls) == 2 and len(cached) == 2, len(http.calls))

        check("★ imas_receptions 顺手过滤品牌",
              [r.booth_slug for r in await feed.imas_receptions()] == ["va-liv-3rdlive"])

        # ---- 封面表 ----
        covers = await feed.covers(max_age=0.0)
        check("★ covers() 按 URL 分发，拿到的是 booth 列表",
              covers == {"va-liv-3rdlive": {"medium": "https://cdn/booth.png.800",
                                            "small": "https://cdn/booth.png.150"}}, covers)
        check("★ 封面表请求的是 booths 地址（不是又拉一遍 receptions）",
              http.urls[-1].endswith("/public/booths"), http.urls[-1])
        calls = len(http.calls)
        await feed.covers(max_age=0.0)
        check("★ 封面表也带 If-None-Match，304 后沿用上次结果",
              "If-None-Match" in http.calls[-1] and len(http.calls) == calls + 1,
              http.calls[-1])
        await feed.covers(max_age=60.0)
        check("★ 封面表在缓存期内不再发请求", len(http.calls) == calls + 1, len(http.calls))

        # 封面表拉失败 → 不抛，退到上次结果（列表照出，只是可能没图）
        class _Dead:
            async def get(self, url, **kw):
                raise RuntimeError("boom")

        dead = _tk.TicketFeed(cfg, _Dead())
        check("★ 封面表拉失败不抛，返回空表（封面只是加分项）",
              await dead.covers(max_age=0.0) == {})

    asyncio.run(run())


def _check_tk_stage() -> None:
    """预热群 → 预览群 → 正式群。**和 RSS 共用同一组配置**，语义必须一致。

    背景：QQ 的图片缓存是服务端、跨群共享的，但**冷链接第一次必炸** ——
    所以正式群之前得先有个群把封面抓进缓存。抽票这边按「一条提醒」组织
    （每条自带一张封面），和 RSS 那边按「订阅」组织不同。
    """
    import tempfile
    from pathlib import Path as _P

    from qq_rss_bot.db import Database as _DB

    def payload(now):
        end = now + timedelta(hours=5)          # 落进 24h 窗口 → 首轮就会推 entry24
        return {
            "data": [{
                "id": "rec-1", "type": "reception",
                "attributes": {
                    "name": "先行", "entry_type": "lottery",
                    "entry_period_status": "within_entry_period",
                    "entry_period_ends_at": end.isoformat(),
                },
                "relationships": {
                    "tour": {"data": {"id": "t1", "type": "tour"}},
                    "cover_image": {"data": None},
                },
            }],
            "included": [
                {"id": "t1", "type": "tour", "attributes": {"name": "巡演"},
                 "relationships": {"booth": {"data": {"id": "b1", "type": "booth"}}}},
                {"id": "b1", "type": "booth",
                 "attributes": {"name": "活动", "slug": "va-liv-3rdlive"}},
            ],
        }

    class _Resp:
        status_code = 200
        headers: dict = {}
        content = b""

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    class _Http:
        async def get(self, url, **kw):
            return _Resp(payload(_tk.now()))

    class _Pusher:
        def __init__(self):
            self.sent: list[str] = []

        async def send_markdown(self, group, text):
            self.sent.append(group)

    def _run(**over):
        """跑一轮，返回 `(发过消息的群, 提醒记账)`。图片关掉 —— 这节测的是
        分发顺序，封面那条路另有钉子（`_check_tk_poller`）。"""
        import asyncio as _a

        async def go():
            with tempfile.TemporaryDirectory() as d:
                db = _DB(_P(d) / "s.db")
                try:
                    cfg = dataclasses.replace(
                        cfg_stub(), ticket_groups=("REAL",), ticket_images=False,
                        preview_warmup_sec=0, preview_veto_sec=0, **over)
                    pusher = _Pusher()
                    poller = _tk.TicketPoller(cfg, db, _Http(), pusher)
                    await poller.poll_once()
                    return pusher.sent, db.ticket_alert_kinds("REAL"), poller
                finally:
                    db.close()

        return _a.run(go())

    # 两个群都没配 → 完全退回改动前的行为
    sent, kinds, _ = _run()
    check("★ 没配预热/预览群时直接发正式群（行为不变）", sent == ["REAL"], sent)

    # 配了预热群：先预热，再正式
    sent, _, _ = _run(warmup_group="WARM")
    check("★ 配了 WARMUP_GROUP → 先预热再发正式群", sent == ["WARM", "REAL"], sent)

    # 只配预览群：预览那条本身就是预热
    sent, _, _ = _run(preview_group="PREV")
    check("★ 只配 PREVIEW_GROUP → 预览那条兼作预热", sent == ["PREV", "REAL"], sent)

    # 两个群配成同一个 → **只发一次**（不是发两遍）
    sent, _, _ = _run(warmup_group="SAME", preview_group="SAME")
    check("★ 预热群和预览群是同一个时只发一次（预览那条就是预热）",
          sent == ["SAME", "REAL"], sent)

    # 预热群本身就是正式群 → 不能发两遍
    sent, _, _ = _run(warmup_group="REAL")
    check("★ 预热群同时是正式群时只发一遍", sent == ["REAL"], sent)
    sent, _, _ = _run(preview_group="REAL")
    check("★ 预览群同时是正式群时只发一遍", sent == ["REAL"], sent)

    # 两个都配 → 三段齐全，顺序不能乱
    sent, _, _ = _run(warmup_group="WARM", preview_group="PREV")
    check("★ 三段顺序：预热 → 预览 → 正式", sent == ["WARM", "PREV", "REAL"], sent)

    # ---- 人工喊停 ----
    import asyncio as _a

    async def veto_case():
        with tempfile.TemporaryDirectory() as d:
            db = _DB(_P(d) / "v.db")
            try:
                cfg = dataclasses.replace(
                    cfg_stub(), ticket_groups=("REAL",), ticket_images=False,
                    preview_group="PREV", preview_warmup_sec=0, preview_veto_sec=5)
                pusher = _Pusher()
                poller = _tk.TicketPoller(cfg, db, _Http(), pusher)

                async def veto_soon():
                    await _a.sleep(0.05)
                    return poller.veto_current()

                _, hit = await _a.gather(poller.poll_once(), veto_soon())
                return pusher.sent, hit, db.ticket_alert_kinds("REAL")
            finally:
                db.close()

    sent, hit, kinds = _a.run(veto_case())
    check("★ 预览窗口内喊「不发」→ 正式群一条都不发", "REAL" not in sent, sent)
    check("★ veto_current 返回 True（真的挡下了，不是回句话而已）", hit is True, hit)
    check("★ 被挡下的提醒照样记账（否则下一轮又捞出来，你得每轮否一次）",
          "entry24" in kinds.get("rec-1", set()), kinds)

    # 窗口没开时喊「不发」不算数
    idle = _tk.TicketPoller(cfg_stub(), None, _Http(), _Pusher())
    check("★ 预览窗口没开时 veto_current 返回 False（闲聊里的「不发」不能吞掉下一条）",
          idle.veto_current() is False)

    # ---- stop() 要能打断预热等待（不然停机干等 45 秒）----
    async def stop_case():
        with tempfile.TemporaryDirectory() as d:
            db = _DB(_P(d) / "st.db")
            try:
                cfg = dataclasses.replace(
                    cfg_stub(), ticket_groups=("REAL",), ticket_images=False,
                    warmup_group="WARM", preview_warmup_sec=30)
                pusher = _Pusher()
                poller = _tk.TicketPoller(cfg, db, _Http(), pusher)

                async def stopper():
                    await _a.sleep(0.05)
                    poller.stop()

                await _a.gather(poller.poll_once(), stopper())
                return pusher.sent
            finally:
                db.close()

    sent = _a.run(stop_case())
    check("★ stop() 能打断预热等待，且不再往正式群发",
          sent == ["WARM"], sent)


def _check_tk_db() -> None:
    import tempfile
    from pathlib import Path as _P

    from qq_rss_bot.db import Database as _DB

    with tempfile.TemporaryDirectory() as d:
        db = _DB(_P(d) / "t.db")
        try:
            check("★ 全新库：表是空的（= 还没建过 baseline）", not db.ticket_alerts_any())
            check("空表查 kinds 得到空 dict", db.ticket_alert_kinds("G") == {})

            db.mark_ticket_alert("rec-1", "G", "new")
            check("★ 记一条之后 baseline 就算建过了", db.ticket_alerts_any())
            check("★ kinds 按受付归组", db.ticket_alert_kinds("G") == {"rec-1": {"new"}})

            db.mark_ticket_alert("rec-1", "G", "entry24")
            db.mark_ticket_alert("rec-1", "G", "new")  # 重复
            check("★ 重复标记幂等",
                  db.ticket_alert_kinds("G") == {"rec-1": {"new", "entry24"}},
                  db.ticket_alert_kinds("G"))

            check("★ 提醒是逐群记的（别的群不受影响）", db.ticket_alert_kinds("H") == {})
            db.mark_ticket_alert("rec-1", "H", "new")
            check("★ 加新群时那个群是干净的 —— 所以 baseline 判据不能按群查",
                  db.ticket_alert_kinds("H") == {"rec-1": {"new"}})
        finally:
            db.close()


def _check_tk_poller() -> None:
    """★ 首轮 baseline 只吞 `new`，**不能吞掉締切提醒**。

    存量 31 条受付必须挡住（否则一开就跑十几条进群），但已经在 24h 窗口里的
    締切要照推 —— 不然第一次启动群里一条消息都没有，没法判断功能通没通。
    """
    import tempfile
    from pathlib import Path as _P

    from qq_rss_bot.db import Database as _DB

    def payload(now):
        end = now + timedelta(hours=5)          # 落在 24h 窗口里、2h 窗口外
        dep = now + timedelta(hours=30)
        return {
            "data": [{
                "id": "rec-1", "type": "reception",
                "attributes": {
                    "name": "アソビストア一般会員先行", "entry_type": "lottery",
                    "entry_period_status": "within_entry_period",
                    "entry_period_ends_at": end.isoformat(),
                    "deposit_period_ends_at": dep.isoformat(),
                },
                "relationships": {
                    "tour": {"data": {"id": "t1", "type": "tour"}},
                    "cover_image": {"data": None},
                },
            }],
            "included": [
                {"id": "t1", "type": "tour", "attributes": {"name": "巡演"},
                 "relationships": {"booth": {"data": {"id": "b1", "type": "booth"}}}},
                {"id": "b1", "type": "booth",
                 "attributes": {"name": "活动", "slug": "va-liv-3rdlive"}},
            ],
        }

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body
            self.headers = {}
            # 尺寸探测要读字节（`multiimage.measure`）—— 给一张真的 800×450
            self.content = _png(800, 450)

        def json(self):
            return self._body

        def raise_for_status(self):
            pass

    def booths_payload():
        """这场 live 的主视觉。受付那条的 `cover_image` 是 null（见上），
        所以图只能来自这里 —— 正好是用户报的那个场景。"""
        return {
            "data": [{"id": "b1", "type": "booth",
                      "attributes": {"slug": "va-liv-3rdlive", "name": "活动"},
                      "relationships": {"cover_image": {"data": {"id": "bc-1", "type": "image"}}}}],
            "included": [{"id": "bc-1", "type": "image",
                          "attributes": {"reading_urls": {"medium": "https://cdn/live.png.800"}}}],
        }

    class _Http:
        async def get(self, url, **kw):
            # 两条接口分开发 —— 封面在 booths 里，混起来这条钉子就白钉了
            return _Resp(booths_payload() if url.endswith("/booths") else payload(_tk.now()))

    class _Pusher:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send_markdown(self, group, text):
            self.sent.append((group, text))

    async def run() -> None:
        with tempfile.TemporaryDirectory() as d:
            db = _DB(_P(d) / "t.db")
            try:
                cfg = dataclasses.replace(cfg_stub(), ticket_groups=("G",))
                pusher = _Pusher()
                poller = _tk.TicketPoller(cfg, db, _Http(), pusher)

                n1 = await poller.poll_once()
                check("★ 首轮：只推締切，不推「新受付」",
                      n1 == 1 and len(pusher.sent) == 1
                      and "申込締切" in pusher.sent[0][1]
                      and "新受付" not in pusher.sent[0][1],
                      f"n={n1} sent={[t[:20] for _, t in pusher.sent]}")
                # ★ 端到端：推出去的提醒真的带上了这场 live 的封面。
                #   受付自己没有封面（cover_image=null），图只能来自 booths 那条。
                check("★ 推送的提醒带上了 booth 级封面（受付级是 null）",
                      pusher.sent[0][1].startswith(
                          "![图1 #320px #180px](https://cdn/live.png.800)\n"),
                      pusher.sent[0][1][:80])
                check("★ 首轮已经把 new 记成已推（下次不会补发）",
                      db.ticket_alert_kinds("G") == {"rec-1": {"new", "entry24"}},
                      db.ticket_alert_kinds("G"))

                n2 = await poller.poll_once()
                check("★ 第二轮：同一条不重复推", n2 == 0, n2)

                # 发失败不能记账，否则那条提醒永远丢了
                class _Boom:
                    async def send_markdown(self, group, text):
                        raise RuntimeError("boom")

                with tempfile.TemporaryDirectory() as d2:
                    db2 = _DB(_P(d2) / "t2.db")
                    try:
                        p2 = _tk.TicketPoller(cfg, db2, _Http(), _Boom())
                        check("★ 发送失败时不记账（下一轮会重试）",
                              await p2.poll_once() == 0
                              and "entry24" not in db2.ticket_alert_kinds("G").get("rec-1", set()),
                              db2.ticket_alert_kinds("G"))
                    finally:
                        db2.close()
            finally:
                db.close()

    asyncio.run(run())


_check_tk_brand()
_check_tk_poller()
_check_tk_stage()
_check_tk_parse()
_check_tk_alerts()
_check_tk_render()
_check_tk_cover()
_check_tk_feed()
_check_tk_db()

print("[4.65] 月曜が近いよ（每周定时播报，全离线）")


class _MondaySender:
    """假 sender。

    `fail` 里的群**发什么都抛**（验逐群隔离）；`fail_video` 只让视频抛
    （验「文案发成了、视频挂了」那条路）。

    `calls` 记调用**顺序**，用来钉死「文案一条、视频一条」这个形状。
    """

    def __init__(
        self,
        fail: set[str] | None = None,
        fail_video: set[str] | None = None,
    ) -> None:
        self.captions: list[tuple[str, str, str | None]] = []  # (群, 文案, 表情包)
        self.videos: list[tuple[str, str]] = []                # (群, 视频) 成功的
        self.video_attempts: list[tuple[str, str]] = []        # 含失败的
        self.calls: list[tuple[str, str]] = []                 # (类型, 群)
        self.fail = fail or set()
        self.fail_video = fail_video or set()

    async def send_text(self, group: str, content: str) -> None:
        if group in self.fail:
            raise RuntimeError("模拟发送失败")
        self.calls.append(("text", group))
        self.captions.append((group, content, None))

    async def send_local_image(self, group: str, content: str, path) -> None:
        if group in self.fail:
            raise RuntimeError("模拟发送失败")
        self.calls.append(("image", group))
        self.captions.append((group, content, str(path)))

    async def send_video(self, group: str, path) -> None:
        # 失败也要记一笔 —— 「重试有没有真的再试一次」只能靠这个看出来
        # （失败的调用不会进 `calls` / `videos`）。
        self.video_attempts.append((group, str(path)))
        if group in self.fail or group in self.fail_video:
            raise RuntimeError("模拟发送失败")
        self.calls.append(("video", group))
        self.videos.append((group, str(path)))


def _md_dir_with(
    tmp: Path, *, big: bool = False, appledouble: bool = False, sticker: bool = False
) -> Path:
    """造一个素材目录。`big` 会放一个**稀疏**的 11 MiB 文件（不真占磁盘）。"""
    d = tmp / "monday"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.mp4").write_bytes(b"x" * 1024)
    (d / "b.mp4").write_bytes(b"y" * 2048)
    (d / "notes.txt").write_bytes(b"not a video")
    (d / "empty.mp4").write_bytes(b"")
    if sticker:
        # 名字学真实素材（`月曜が近いよ.jpg`）—— 认后缀不认名字，钉死这一点
        (d / "月曜が近いよ.jpg").write_bytes(b"\xff\xd8\xff" + b"j" * 512)
    if appledouble:
        # ⚠️ macOS 的 tar / Finder 会带这个，**它也以 .mp4 结尾**
        (d / "._a.mp4").write_bytes(b"\x00\x05\x16\x07")
    if big:
        with (d / "huge.mp4").open("wb") as fh:
            fh.truncate(11 * 1024 * 1024)
    return d


def _check_md_list() -> None:
    import logging as _logging  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import monday as _md  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _md_dir_with(tmp, big=True, appledouble=True)

        records: list[_logging.LogRecord] = []

        class _Cap(_logging.Handler):
            def emit(self, record):  # noqa: ANN001
                records.append(record)

        log = _logging.getLogger("qq_rss_bot.monday")
        cap = _Cap(level=_logging.WARNING)
        log.addHandler(cap)
        try:
            videos = _md.list_videos(d)
        finally:
            log.removeHandler(cap)

        names = [p.name for p in videos]
        check("★ 只挑出真正的 mp4", names == ["a.mp4", "b.mp4"], str(names))
        check("★ `._` 元数据文件不算视频（它同样以 .mp4 结尾）",
              "._a.mp4" not in names, str(names))
        check("★ 0 字节的不算视频", "empty.mp4" not in names, str(names))
        check("★ 非 mp4 不算视频", "notes.txt" not in names, str(names))
        check("★ 超过 10 MiB 的被剔除", "huge.mp4" not in names, str(names))
        check("★ 剔除超限文件时**打了 WARNING**（静默跳过的话没人知道为什么没播）",
              any("huge.mp4" in r.getMessage() for r in records),
              str([r.getMessage() for r in records]))
        check("★ 警告里说明了是哪个文件、多大",
              any("10 MiB" in r.getMessage() for r in records),
              str([r.getMessage() for r in records]))

        # 目录不存在 / 空目录都不该抛
        check("目录不存在 → 空列表、不抛", _md.list_videos(tmp / "nope") == [])
        empty = tmp / "empty_dir"
        empty.mkdir()
        check("空目录 → 空列表、不抛", _md.list_videos(empty) == [])
        check("★ 上限常量就是实测出来的 10 MiB",
              _md.MAX_VIDEO_BYTES == 10 * 1024 * 1024, str(_md.MAX_VIDEO_BYTES))


def _check_md_pick() -> None:
    from qq_rss_bot import monday as _md  # noqa: PLC0415

    a, b = Path("/x/月曜_1.mp4"), Path("/x/月曜_2.mp4")
    both = [a, b]

    # 两个素材时，上周发过 A → 这周必选 B。
    # 纯随机有一半概率连着两周发同一个，看起来就像「随机根本没生效」。
    check("★ 有得选时避开上次那个（跑 30 次都不重复）",
          all(_md.pick_video(both, "月曜_1.mp4") == b for _ in range(30)))
    check("★ 上次那个已不在目录里时不报错", _md.pick_video(both, "没了.mp4") in both)
    check("★ 只有一个素材时照发（不能因为会重复就一条都不发）",
          _md.pick_video([a], "月曜_1.mp4") == a)
    check("★ 两个都能选到时结果不唯一（确实是随机）",
          len({_md.pick_video(both, None).name for _ in range(60)}) == 2)
    try:
        _md.pick_video([], None)
        check("空列表应当抛 ValueError", False)
    except ValueError:
        check("★ 空列表抛 ValueError", True)


def _check_md_slot() -> None:
    from qq_rss_bot import monday as _md  # noqa: PLC0415

    at = _md.parse_time("22:00")
    cases = [
        # (now, 期望 slot, 说明)
        ("2026-10-18T21:59", "2026-10-11T22:00", "周日 21:59 → 上周日（还没到）"),
        ("2026-10-18T22:00", "2026-10-18T22:00", "正好 22:00 → **今天**（否则整点那一下会漏一周）"),
        ("2026-10-18T22:01", "2026-10-18T22:00", "22:01 → 今天（窗口内）"),
        ("2026-10-19T09:00", "2026-10-18T22:00", "周一 09:00 → 上周日（但已出窗口）"),
        ("2026-10-25T12:00", "2026-10-18T22:00", "下周日中午 → 还是上周日"),
    ]
    for raw, want, why in cases:
        got = _md.slot_for(datetime.fromisoformat(raw), 6, at)
        check(f"★ slot：{why}", got.isoformat(timespec="minutes") == want,
              f"{got.isoformat(timespec='minutes')} != {want}")

    # 默认播出时间：2026-10-18 从 22:00 改成 23:30（中国时间）。
    # 钉住它是为了「改时间只该改一个地方」—— .env 没配时也得是 23:30。
    check("★ 默认播出时间是 23:30", _md.DEFAULT_TIME == "23:30", _md.DEFAULT_TIME)
    check("★ 默认值是能吃的 HH:MM",
          _md.parse_time(_md.DEFAULT_TIME).strftime("%H:%M") == "23:30")
    check("★ 不给 at 时 slot_for 用默认时间（23:30 → 当天）",
          _md.slot_for(datetime.fromisoformat("2026-10-18T23:30"), 6).isoformat(
              timespec="minutes") == "2026-10-18T23:30")
    check("★ 23:29 还没到 → 上周日 23:30",
          _md.slot_for(datetime.fromisoformat("2026-10-18T23:29"), 6).isoformat(
              timespec="minutes") == "2026-10-11T23:30")
    check("★ Config 的默认值跟着 DEFAULT_TIME 走（只改一处）",
          cfg_stub().monday_time == _md.DEFAULT_TIME, cfg_stub().monday_time)

    # 窗口判定：只有抖动算数，不是补发
    now = datetime.fromisoformat("2026-10-18T22:02")
    slot = _md.slot_for(now, 6, at)
    check("★ 22:02 在 180 秒窗口内（tick 抖动）",
          (now - slot).total_seconds() <= _md.DEFAULT_WINDOW_SEC)
    now = datetime.fromisoformat("2026-10-18T22:05")
    slot = _md.slot_for(now, 6, at)
    check("★ 22:05 已出窗口 —— 用户明确选了「不补发」",
          (now - slot).total_seconds() > _md.DEFAULT_WINDOW_SEC)


def _check_md_text() -> None:
    from qq_rss_bot import monday as _md  # noqa: PLC0415

    # 2026-10-18 用户明确不要「（あと N 時間）」这个尾巴 —— 就是这一句，一个字不多。
    check("★ 文案就是「月曜が近いよ」，没有后缀",
          _md.render_text(datetime.fromisoformat("2026-10-18T22:00")) == "月曜が近いよ")
    check("★ 文案是**常量**：换任何时刻都一样（不可能再出现「あと -8 時間」）",
          len({_md.render_text(datetime.fromisoformat(t)) for t in (
              "2026-10-17T12:00", "2026-10-18T22:00", "2026-10-19T00:30",
              "2026-10-19T09:00", "2026-10-25T23:59")}) == 1)
    check("★ 不带参数也能调（探测脚本 / 调用方不该被 now 绑住）",
          _md.render_text() == "月曜が近いよ")
    check("★ 文案里没有裸的 markdown 语法符号（可能走 msg_type=0）",
          all(ch not in _md.render_text(datetime.fromisoformat("2026-10-18T22:00"))
              for ch in "**[]()`#>|"))

    # 时间配置写错要**当场退出**，不能静默用一个默认值
    for bad in ("25:00", "2200", "", "abc"):
        try:
            _md.parse_time(bad)
            check(f"MONDAY_TIME={bad!r} 应当退出", False)
        except SystemExit:
            check(f"★ MONDAY_TIME={bad!r} → SystemExit", True)


def _check_md_sticker() -> None:
    """表情包：按**后缀**认，不要求文件名；没有就退化成纯文字。"""
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import monday as _md  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        d = _md_dir_with(tmp, sticker=True)
        got = _md.find_sticker(d)
        check("★ 认得出表情包（jpg）",
              got is not None and got.name == "月曜が近いよ.jpg", str(got))
        check("★ 表情包**按后缀认**，不看文件名（真实素材就叫 月曜が近いよ.jpg）",
              got is not None and got.stem == "月曜が近いよ", str(got))
        check("★ 表情包不会被当成视频",
              got.name not in {q.name for q in _md.list_videos(d)})
        check("★ 视频不会被当成表情包",
              all(q.suffix.lower() not in _md._STICKER_SUFFIXES
                  for q in _md.list_videos(d)))

        # `._` 元数据：AppleDouble 的图片同样要跳过
        ad = tmp / "ad"
        ad.mkdir()
        (ad / "._x.jpg").write_bytes(b"junk")
        check("★ `._x.jpg` 不当表情包", _md.find_sticker(ad) is None)
        (ad / "x.jpg").write_bytes(b"j")
        one = _md.find_sticker(ad)
        check("★ 有真图时 `._` 那个也不干扰",
              one is not None and one.name == "x.jpg", str(one))

        # 没有图 → None（调用方退化成纯文字），不能抛
        plain = tmp / "plain"
        plain.mkdir()
        (plain / "a.mp4").write_bytes(b"v")
        check("★ 没有图 → None（文案退化成纯文字）", _md.find_sticker(plain) is None)
        check("★ 目录不存在 → None，不抛", _md.find_sticker(tmp / "nope") is None)

        # 0 字节的图不算
        z = tmp / "zero"
        z.mkdir()
        (z / "empty.jpg").write_bytes(b"")
        check("★ 0 字节的图不算", _md.find_sticker(z) is None)

        # 多张图：挑排第一的（确定性，不是随机）
        m = tmp / "many"
        m.mkdir()
        (m / "b.png").write_bytes(b"b")
        (m / "a.png").write_bytes(b"a")
        many = _md.find_sticker(m)
        check("★ 多张图时取按名字排第一的（确定性，不是随机）",
              many is not None and many.name == "a.png", str(many))

        # 各种后缀
        for suf in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            box = tmp / ("one" + suf.strip("."))
            box.mkdir()
            (box / ("x" + suf)).write_bytes(b"i")
            check("★ 认 " + suf, _md.find_sticker(box) is not None)


def _check_md_poller() -> None:
    import tempfile  # noqa: PLC0415
    from dataclasses import replace  # noqa: PLC0415

    from qq_rss_bot import monday as _md  # noqa: PLC0415

    at = "2026-10-18T22:00"

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            d = _md_dir_with(tmp, appledouble=True, sticker=True)
            db = Database(tmp / "t.db")
            try:
                cfg = replace(
                    cfg_stub(),
                    monday_groups=("G1", "G2"),
                    monday_video_dir=d,
                    monday_weekday=6,
                    monday_time="22:00",
                    monday_window_sec=180,
                )
                now = datetime.fromisoformat(at)

                # --- 没配群 = 功能关闭 ---
                off = replace(cfg, monday_groups=())
                s0 = _MondaySender()
                check("★ 没配 MONDAY_GROUPS → 一条都不发",
                      await _md.MondayPoller(off, db, s0).poll_once(now=now) == 0)
                check("★ 没配群时不发（sender 没被调用）",
                      s0.videos == [] and s0.captions == [])

                # --- 正常播报 ---
                sender = _MondaySender()
                poller = _md.MondayPoller(cfg, db, sender)
                check("★ 窗口内发到 2 个群",
                      await poller.poll_once(now=now) == 2, str(sender.videos))
                check("★ 文案就是「月曜が近いよ」（不带「あと N 時間」）",
                      all(c == "月曜が近いよ" for _, c, _ in sender.captions),
                      str(sender.captions))
                check("★ 文案跟**表情包**走（send_local_image，不是纯文本）",
                      all(st is not None and st.endswith(".jpg")
                          for _, _, st in sender.captions), str(sender.captions))
                check("★ **两条消息**：文案一条、视频一条（不是一条）",
                      sender.calls == [("image", "G1"), ("video", "G1"),
                                       ("image", "G2"), ("video", "G2")],
                      str(sender.calls))
                check("★ 顺序是文案在前、视频在后",
                      all(c[0] == "image" for c in sender.calls[::2])
                      and all(c[0] == "video" for c in sender.calls[1::2]),
                      str(sender.calls))
                check("★ 视频那条**不带文案**（带了客户端也丢掉，只会误导）",
                      all(len(v) == 2 for v in sender.videos), str(sender.videos))
                check("★ 发的都是同一个视频（一周一条，不是每群随机）",
                      len({p for _, p in sender.videos}) == 1, str(sender.videos))
                check("★ 发的是真 mp4，不是 `._` 那个",
                      all(Path(p).name in ("a.mp4", "b.mp4") for _, p in sender.videos),
                      str(sender.videos))
                check("★ 落库了（重启后同一周不会重发）",
                      db.monday_posted("2026-10-18T22:00") == {"G1", "G2"})

                # --- 幂等：同一个 slot 再跑一次，一条都不发 ---
                again = _MondaySender()
                check("★ 同一 slot 第二次 → 0 条",
                      await _md.MondayPoller(cfg, db, again).poll_once(now=now) == 0)
                check("★ 第二次真的没调用 sender",
                      again.videos == [] and again.captions == [])

                # --- 窗口外不发（不补发）---
                late = datetime.fromisoformat("2026-10-19T09:00")
                db2 = Database(tmp / "t2.db")
                try:
                    s2 = _MondaySender()
                    check("★ 周一才起来 → 不发（不补发）",
                          await _md.MondayPoller(cfg, db2, s2).poll_once(now=late) == 0)
                    check("★ 窗口外真的没调用 sender",
                          s2.videos == [] and s2.captions == [])
                finally:
                    db2.close()

                # --- 逐群隔离：一个群挂了不影响别的群 ---
                db3 = Database(tmp / "t3.db")
                try:
                    s3 = _MondaySender(fail={"G1"})
                    check("★ 一个群失败时其余群照发",
                          await _md.MondayPoller(cfg, db3, s3).poll_once(now=now) == 1,
                          str(s3.videos))
                    check("★ G1 挂了之后 G2 的两条都发出去了",
                          s3.calls == [("image", "G2"), ("video", "G2")], str(s3.calls))
                    check("★ 失败的群**不记账**（窗口内下次 tick 会重试）",
                          db3.monday_posted("2026-10-18T22:00") == {"G2"},
                          str(db3.monday_posted("2026-10-18T22:00")))
                finally:
                    db3.close()

                # --- 没有素材：一条不发，也不能抛 ---
                db4 = Database(tmp / "t4.db")
                try:
                    empty = tmp / "nothing"
                    empty.mkdir()
                    s4 = _MondaySender()
                    cfg4 = replace(cfg, monday_video_dir=empty)
                    check("★ 目录里没有素材 → 0 条、不抛",
                          await _md.MondayPoller(cfg4, db4, s4).poll_once(now=now) == 0)
                    check("★ 没素材时没调用 sender",
                          s4.videos == [] and s4.captions == [])
                    check("★ 没素材时不记账（下周还能补上）",
                          db4.monday_posted("2026-10-18T22:00") == set())
                finally:
                    db4.close()

                # --- 去重：上周发过 A，这周不会又发 A ---
                db5 = Database(tmp / "t5.db")
                try:
                    db5.mark_monday_posted("2026-10-11T22:00", "G1", "a.mp4")
                    check("★ 记住了上次发的", db5.monday_last_video() == "a.mp4")
                    s5 = _MondaySender()
                    await _md.MondayPoller(cfg, db5, s5).poll_once(now=now)
                    check("★ 这周避开了上周那个",
                          all(Path(p).name == "b.mp4" for _, p in s5.videos),
                          str(s5.videos))
                finally:
                    db5.close()

                # --- 视频挂了：那个群不记账；重试时**文案不重发** ---
                db6 = Database(tmp / "t6.db")
                try:
                    s6 = _MondaySender(fail_video={"G1"})
                    p6 = _md.MondayPoller(cfg, db6, s6)
                    check("★ 视频挂了 → 那个群不算发成功",
                          await p6.poll_once(now=now) == 1, str(s6.videos))
                    check("★ 视频挂了但文案照样发（两条互不连累）",
                          ("image", "G1") in s6.calls, str(s6.calls))
                    check("★ 视频挂了 → 那个群**不记账**（窗口内会重试）",
                          db6.monday_posted("2026-10-18T22:00") == {"G2"},
                          str(db6.monday_posted("2026-10-18T22:00")))
                    before = len(s6.captions)
                    # 第一轮：G1 视频失败、G2 成功 → 2 次尝试
                    check("★ 第一轮试了 2 个群的视频",
                          len(s6.video_attempts) == 2, str(s6.video_attempts))
                    await p6.poll_once(now=now)
                    check("★ 重试时**文案不重发**（否则群里两条一样的文案）",
                          len(s6.captions) == before, str(s6.captions))
                    check("★ 重试时**真的又试了一次**视频（只有 G1，G2 已记账）",
                          len(s6.video_attempts) == 3
                          and s6.video_attempts[-1][0] == "G1",
                          str(s6.video_attempts))
                finally:
                    db6.close()

                # --- 没有表情包：文案退回纯文字，其余完全不变 ---
                db7 = Database(tmp / "t7.db")
                try:
                    s7 = _MondaySender()
                    noimg = _md_dir_with(tmp / "nosticker", appledouble=True)
                    cfg7 = replace(cfg, monday_video_dir=noimg)
                    check("★ 没表情包也照发",
                          await _md.MondayPoller(cfg7, db7, s7).poll_once(now=now) == 2)
                    check("★ 没表情包时文案走 msg_type=0（send_text）",
                          [c[0] for c in s7.calls] == ["text", "video", "text", "video"],
                          str(s7.calls))
                    check("★ 没表情包时文案内容不变",
                          all(c == "月曜が近いよ" for _, c, _ in s7.captions))
                finally:
                    db7.close()
            finally:
                db.close()

    asyncio.run(run())


class _MondayApi:
    """记录 `post_group_file` / `post_group_message` 的调用参数。"""

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.sent: list[dict] = []
        self.fail_send: int | None = None
        self.no_file_info = False

    async def post_group_file(self, **kw):
        self.uploads.append(kw)
        return {} if self.no_file_info else {"file_info": "FI-123"}

    async def post_group_message(self, **kw):
        self.sent.append(kw)
        return {"code": self.fail_send} if self.fail_send else {"code": 0}


class _MondayBotStub:
    """只借 `RssBot` 的两个发送方法，不真造 botpy Client。"""

    send_video = _RssBot.send_video
    send_local_image = _RssBot.send_local_image

    def __init__(self, api) -> None:
        self.api = api


def _check_md_send_video() -> None:
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import monday as _md  # noqa: PLC0415
    from qq_rss_bot import preview as _pv  # noqa: PLC0415

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            small = tmp / "ok.mp4"
            small.write_bytes(b"v" * 4096)
            big = tmp / "huge.mp4"
            with big.open("wb") as fh:
                fh.truncate(_md.MAX_VIDEO_BYTES + 1)

            api = _MondayApi()
            bot = _MondayBotStub(api)
            await bot.send_video("G1", small)

            check("★ 上传走 file_type=2（视频 mp4）",
                  api.uploads and api.uploads[0]["file_type"] == 2, str(api.uploads))
            check("★ 上传带的是 file_data（字节），不是 url",
                  "file_data" in api.uploads[0] and "url" not in api.uploads[0],
                  str(list(api.uploads[0])))
            check("★ srv_send_msg=False（只上传，不单独发一条）",
                  api.uploads[0]["srv_send_msg"] is False, str(api.uploads[0]))
            check("★ 正文走 msg_type=7（富媒体）",
                  api.sent and api.sent[0]["msg_type"] == 7, str(api.sent))
            check("★ media 里带上传拿到的 file_info",
                  api.sent[0]["media"] == {"file_info": "FI-123"}, str(api.sent))
            check("★ 视频那条**正文是空的** —— content 客户端不渲染，塞了只会误导",
                  api.sent[0]["content"] == "", str(api.sent))
            check("★ `send_video` 的签名里根本没有 content 形参（从根上不给塞）",
                  "content" not in _RssBot.send_video.__code__.co_varnames,
                  str(_RssBot.send_video.__code__.co_varnames))

            # 硬闸：超过 10 MiB 连上传都不该发生
            api2 = _MondayApi()
            bot2 = _MondayBotStub(api2)
            try:
                await bot2.send_video("G1", big)
                check("★ 超 10 MiB 应当抛", False)
            except RuntimeError as exc:
                check("★ 超 10 MiB 抛 RuntimeError", "10 MiB" in str(exc), str(exc))
            check("★ 超限时**一个请求都没发出去**",
                  api2.uploads == [] and api2.sent == [], str(api2.uploads))

            # 业务错误码要当失败（botpy 只在 HTTP 非 2xx 时抛）
            api3 = _MondayApi()
            api3.fail_send = 40093011
            try:
                await _MondayBotStub(api3).send_video("G1", small)
                check("★ 业务错误码应当抛", False)
            except RuntimeError as exc:
                check("★ 200 带 code 也算失败（40093011）",
                      "40093011" in str(exc), str(exc))

            # ---- send_local_image：文案 + 表情包挤进**一条**消息 ----
            img = tmp / "月曜が近いよ.jpg"
            img.write_bytes(b"\xff\xd8\xff" + b"j" * 4096)
            api4 = _MondayApi()
            await _MondayBotStub(api4).send_local_image("G1", "月曜が近いよ", img)
            check("★ 图片上传走 file_type=1（图片，不是 2 视频）",
                  api4.uploads and api4.uploads[0]["file_type"] == 1, str(api4.uploads))
            check("★ 图片上传带 file_data、不带 url",
                  "file_data" in api4.uploads[0] and "url" not in api4.uploads[0],
                  str(list(api4.uploads[0])))
            check("★ 图片也是 srv_send_msg=False（先传后发，两步）",
                  api4.uploads[0]["srv_send_msg"] is False, str(api4.uploads[0]))
            check("★ 图文走 msg_type=7",
                  api4.sent and api4.sent[0]["msg_type"] == 7, str(api4.sent))
            check("★ **文案和表情包在同一条消息里**（图片这条路 content 是渲染的）",
                  api4.sent[0]["content"] == "月曜が近いよ", str(api4.sent))
            check("★ 图文只发了**一条**消息（不是拆两条）",
                  len(api4.sent) == 1, str(api4.sent))

            # 图片 base64 后超 5M 要抛，不能静默少一张图
            huge_img = tmp / "huge.jpg"
            with huge_img.open("wb") as fh:
                fh.truncate(_pv.MAX_FILE_DATA_BYTES)   # base64 后必然超
            api5 = _MondayApi()
            try:
                await _MondayBotStub(api5).send_local_image("G1", "x", huge_img)
                check("★ 图片超 5M 应当抛", False)
            except RuntimeError as exc:
                check("★ 图片 base64 后超 5M 抛 RuntimeError（不静默少一张图）",
                      "上限" in str(exc), str(exc))
            check("★ 图片超限时一个请求都没发",
                  api5.uploads == [] and api5.sent == [], str(api5.uploads))

            # 上传拿不到 file_info 也要抛
            api6 = _MondayApi()
            api6.no_file_info = True
            try:
                await _MondayBotStub(api6).send_local_image("G1", "x", img)
                check("★ 没拿到 file_info 应当抛", False)
            except RuntimeError as exc:
                check("★ 上传没 file_info → 抛（不当成发送成功）",
                      "file_info" in str(exc), str(exc))

    asyncio.run(run())


_check_md_list()
_check_md_sticker()
_check_md_pick()
_check_md_slot()
_check_md_text()
_check_md_poller()
_check_md_send_video()

print("[4.9] 小偶像早安语音（全离线）")

_MN_ROOT = Path(__file__).resolve().parents[1]


class _MorningDb:
    """只实现 `MorningPoller` 用到的那四个方法的假库。

    `rows` 按 (slot, 群, 偶像, 文件) 追加 —— `morning_recent_idols` 直接吐
    出现过的偶像，测试里够用了（真实实现按 slot 前缀筛，见 db.py 的注释）。
    """

    def __init__(self) -> None:
        self.posted: dict[str, set[str]] = {}
        self.rows: list[tuple[str, str, str, str]] = []

    def morning_posted(self, slot: str) -> set[str]:
        return set(self.posted.get(slot, ()))

    def mark_morning_posted(self, slot: str, group: str, idol: str, line: str) -> None:
        self.posted.setdefault(slot, set()).add(group)
        self.rows.append((slot, group, idol, line))

    def morning_last_line(self) -> str | None:
        return self.rows[-1][3] if self.rows else None

    def morning_recent_idols(self, days: int) -> set[str]:
        return {row[2] for row in self.rows}


class _MorningSender:
    """假 sender。`fail` 里的群发语音抛异常、`fail_image` 里的发图抛异常。

    `send_local_image` 是 2026-10-20 加早安卡片之后才被用到的 ——
    在此之前 `MorningPoller` 只会调 `send_voice`，所以这个假类**曾经没有它**，
    于是 1299 项里**没有一条**走过「头像+文字」那条路（缺方法会 AttributeError，
    但那时所有用例的 `avatar` 都是 None，根本走不到）。加卡片必须补上这个假方法。
    """

    def __init__(
        self, fail: set[str] | None = None, fail_image: set[str] | None = None,
    ) -> None:
        self.voices: list[tuple[str, str]] = []
        self.images: list[tuple[str, str, str]] = []   # (群, 文字, 图片路径)
        self.attempts: list[tuple[str, str]] = []      # 含失败的语音
        self.events: list[tuple[str, str]] = []        # **真正到达的顺序**
        self.fail = fail or set()
        self.fail_image = fail_image or set()

    async def send_voice(self, group: str, path) -> None:
        self.attempts.append((group, str(path)))
        if group in self.fail:
            raise RuntimeError("模拟发送失败")
        self.voices.append((group, str(path)))
        self.events.append(("voice", group))

    async def send_local_image(self, group: str, content: str, path) -> None:
        if group in self.fail_image:
            raise RuntimeError("模拟发图失败")
        self.images.append((group, content, str(path)))
        self.events.append(("image", group))


# 假 manifest 用的角色名。**故意让 `ja` 带 `[softly] ` 语气提示** ——
# 「发到群里之前要把提示剥掉」这件事必须在这一层就被测到。
_MN_NAMES = {"mano": "櫻木真乃", "hiori": "風野灯織", "meguru": "八宮めぐる"}
_MN_JA = "おはよう、プロデューサーさん"
# 假的 180×180 PNG 字节。假 sender 不看内容，但**真 sender 会读**
# （`bot.send_local_image` 要 base64 它、还要量 base64 之后的长度）。
_MN_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512


def _mn_write_manifest(mdir: Path, mapping: dict) -> None:
    """往 `morning/.manifest.json` 写一份**任意**内容（覆盖）。

    给边界用例用（坏 JSON、顶层是数组、记录不是对象、缺 `name`……），
    这些形态 `_mn_dir` 的 `names=True` 造不出来。
    """
    import json  # noqa: PLC0415

    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / ".manifest.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    )


def _mn_dir(
    tmp: Path, *, idols: tuple[str, ...] = ("mano",), per: int = 2,
    bad_magic: bool = False, appledouble: bool = False,
    avatars: bool = False, names: bool = False,
) -> Path:
    """造一个 `morning/` 目录。**默认只填 `audio/`。**

    `avatars` / `names` 要**显式打开**，这不是懒：两个开关都关着时
    `MorningPoller` 走的是**纯语音**那条老路，而绝大多数用例要的就是那条路。
    默认打开的话所有老用例都会变成「发图+语音」，断言全得跟着改。
    """
    from qq_rss_bot import morning as _mn  # noqa: PLC0415

    d = tmp / "morning" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    for iid in idols:
        for n in range(per):
            (d / f"{iid}_{n}.silk").write_bytes(_mn.SILK_MAGIC + b"x" * 128)
    (d / "notes.txt").write_bytes(b"not a voice")
    (d / "empty_0.silk").write_bytes(b"")
    (d / "nodigit_x.silk").write_bytes(_mn.SILK_MAGIC + b"x" * 16)
    if bad_magic:
        # ⚠️ 名字完全合法，只是文件头不对 —— 上传接口**看不出来**，只有这里能挡
        (d / "broken_0.silk").write_bytes(b"ID3\x03\x00" + b"x" * 128)
    if appledouble:
        # macOS 的 tar / Finder 带出来的，**同样以 .silk 结尾**
        (d / "._mano_0.silk").write_bytes(b"\x00\x05\x16\x07")
    if avatars:
        ad = tmp / "morning" / "avatar"
        ad.mkdir(parents=True, exist_ok=True)
        for iid in idols:
            (ad / f"{iid}.png").write_bytes(_MN_PNG)
    if names:
        # 键就是 silk 文件名 —— 运行时手里正好有 `line.name`，不用索引换算。
        _mn_write_manifest(
            tmp / "morning",
            {f"{iid}_{n}.silk": {"name": _MN_NAMES.get(iid, iid),
                                 "ja": f"[softly] {_MN_JA}"}
             for iid in idols for n in range(per)},
        )
    return tmp / "morning"


def _check_mn_parts() -> None:
    from datetime import datetime  # noqa: PLC0415

    from qq_rss_bot import morning as _mn  # noqa: PLC0415

    check("★ 默认播报时间是 07:30（用户定的）",
          _mn.DEFAULT_TIME == "07:30", _mn.DEFAULT_TIME)
    check("cfg_stub 的 morning_time 就是那个默认值",
          cfg_stub().morning_time == _mn.DEFAULT_TIME, cfg_stub().morning_time)

    at = _mn.parse_time("07:30")
    check("07:29 → 昨天 07:30",
          _mn.slot_for(datetime(2026, 10, 20, 7, 29), at) == datetime(2026, 10, 19, 7, 30),
          str(_mn.slot_for(datetime(2026, 10, 20, 7, 29), at)))
    check("★ 正好 07:30 → **今天** 07:30（含边界，否则整点那一拍会算成昨天）",
          _mn.slot_for(datetime(2026, 10, 20, 7, 30), at) == datetime(2026, 10, 20, 7, 30),
          str(_mn.slot_for(datetime(2026, 10, 20, 7, 30), at)))
    check("07:31 → 今天 07:30",
          _mn.slot_for(datetime(2026, 10, 20, 7, 31), at) == datetime(2026, 10, 20, 7, 30),
          str(_mn.slot_for(datetime(2026, 10, 20, 7, 31), at)))
    # 周一是 2026-10-19，周三是 10-21 —— 两天都要落在**当天**，不能像月曜那样差 7 天
    check("★ 是**每天**一次，不是每周（周一和周三各自落在当天）",
          _mn.slot_for(datetime(2026, 10, 19, 7, 30), at) == datetime(2026, 10, 19, 7, 30)
          and _mn.slot_for(datetime(2026, 10, 21, 7, 30), at) == datetime(2026, 10, 21, 7, 30))

    # ---- 选人 ----
    voices = {f"idol{i}": [Path(f"idol{i}_0.silk")] for i in range(28)}
    recent = {f"idol{i}" for i in range(27)}
    check("★ 避开最近发过的",
          all(_mn.pick_idol(voices, recent) == "idol27" for _ in range(20)),
          str(_mn.pick_idol(voices, recent)))
    check("★ 全都在 recent 里时**照挑一位**（不死循环、不空转）",
          _mn.pick_idol(voices, set(voices)) in voices)
    check("只有一位偶像时照发",
          _mn.pick_idol({"a": [Path("a_0.silk")]}, {"a"}) == "a")

    # ---- 选台词 ----
    two = [Path("a_0.silk"), Path("a_1.silk")]
    check("★ 避开上一条（只有两条时要 100% 换）",
          all(_mn.pick_line(two, "a_0.silk") == two[1] for _ in range(20)))
    check("只有一条台词时照发",
          _mn.pick_line([two[0]], "a_0.silk") == two[0])


def _check_mn_list_voices() -> None:
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import morning as _mn  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        mdir = _mn_dir(tmp, idols=("mano", "hiori"), per=2,
                       bad_magic=True, appledouble=True)
        got = _mn.list_voices(mdir / "audio")
        names = [p.name for ps in got.values() for p in ps]
        check("按 `<偶像id>_<序号>.silk` 归组",
              set(got) == {"mano", "hiori"}, str(sorted(got)))
        check("每位两条",
              all(len(ps) == 2 for ps in got.values()), str({k: len(v) for k, v in got.items()}))
        check("★ **魔数不对的 .silk 被跳过** —— 上传接口不校验格式，这是唯一的把关",
              "broken_0.silk" not in names, str(names))
        check("★ `._` 开头的（AppleDouble）不当成语音",
              all(not n.startswith("._") for n in names), str(names))
        check("0 字节的跳过", "empty_0.silk" not in names, str(names))
        check("名字不合 `<id>_<序号>` 的跳过", "nodigit_x.silk" not in names, str(names))
        check("非 .silk 后缀的跳过", "notes.txt" not in names, str(names))

        check("目录不存在 → 空表，不抛", _mn.list_voices(tmp / "nope") == {})


def _check_mn_poller() -> None:
    import dataclasses  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from qq_rss_bot import morning as _mn  # noqa: PLC0415

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mdir = _mn_dir(tmp, idols=("mano", "hiori"), per=3)
            cfg = dataclasses.replace(
                cfg_stub(), morning_groups=("G1", "G2"),
                morning_dir=mdir, morning_time="07:30", morning_recent_days=7,
            )
            slot_key = "2026-10-20T07:30"

            db, sender = _MorningDb(), _MorningSender()
            p = _mn.MorningPoller(cfg, db, sender)
            n = await p.poll_once(now=datetime(2026, 10, 20, 7, 30))
            check("07:30 准点发给两个群", n == 2, str(n))
            check("★ 两个群拿到**同一条**语音（选人在循环外，多群不会精神分裂）",
                  len({v[1] for v in sender.voices}) == 1, str(sender.voices))
            check("落库：这个 slot 记了两个群",
                  db.morning_posted(slot_key) == {"G1", "G2"}, str(db.posted))
            check("落了偶像和文件名",
                  all(len(r) == 4 and r[2] and r[3].endswith(".silk") for r in db.rows),
                  str(db.rows))

            n2 = await p.poll_once(now=datetime(2026, 10, 20, 7, 31))
            check("★ 同一 slot 再跑不会重发（幂等靠库，不靠内存）", n2 == 0, str(n2))

            # 窗口外 —— 不补发
            db2, s2 = _MorningDb(), _MorningSender()
            p2 = _mn.MorningPoller(cfg, db2, s2)
            n3 = await p2.poll_once(now=datetime(2026, 10, 20, 8, 0))
            check("★ 窗口外（08:00）**不发** —— 不补发", n3 == 0 and not s2.voices, str(n3))

            # 一个群挂了不连累别的
            cfg3 = dataclasses.replace(cfg, morning_groups=("G1", "G2", "G3"))
            db3, s3 = _MorningDb(), _MorningSender(fail={"G2"})
            p3 = _mn.MorningPoller(cfg3, db3, s3)
            n4 = await p3.poll_once(now=datetime(2026, 10, 20, 7, 30))
            check("★ 一个群发不出去，其余群照发",
                  n4 == 2 and {v[0] for v in s3.voices} == {"G1", "G3"},
                  f"{n4} {s3.voices}")
            check("失败的群**不记账**（窗口内下一拍还会重试）",
                  "G2" not in db3.morning_posted(slot_key), str(db3.posted))

            # 目录空 / 不存在
            for label, mdir2 in (("目录不存在", tmp / "nope"),
                                 ("audio 空", (lambda d: (d.mkdir(parents=True), d)[1])(tmp / "e" / "audio"))):
                cfg4 = dataclasses.replace(cfg, morning_groups=("G1",), morning_dir=mdir2)
                s4 = _MorningSender()
                n5 = await _mn.MorningPoller(cfg4, _MorningDb(), s4).poll_once(
                    now=datetime(2026, 10, 20, 7, 30)
                )
                check(f"{label} → 返回 0、不抛、不发", n5 == 0 and not s4.voices, str(n5))

            # 只有一位偶像、只有一条台词
            cfg5 = dataclasses.replace(
                cfg, morning_groups=("G1",), morning_dir=_mn_dir(tmp / "solo", idols=("mano",), per=1),
            )
            db5, s5 = _MorningDb(), _MorningSender()
            n6 = await _mn.MorningPoller(cfg5, db5, s5).poll_once(
                now=datetime(2026, 10, 20, 7, 30)
            )
            check("只有一位偶像、一条台词时照发", n6 == 1, str(n6))

            # 未配置群 = 功能关闭
            cfg6 = dataclasses.replace(cfg, morning_groups=())
            s6 = _MorningSender()
            n7 = await _mn.MorningPoller(cfg6, _MorningDb(), s6).poll_once(
                now=datetime(2026, 10, 20, 7, 30)
            )
            check("MORNING_GROUPS 空 → 整个功能关闭（返回 0）", n7 == 0 and not s6.voices, str(n7))

    asyncio.run(run())


def _check_mn_send_voice() -> None:
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import morning as _mn  # noqa: PLC0415
    from qq_rss_bot.bot import RssBot as _RB  # noqa: PLC0415

    class Stub:
        send_voice = _RB.send_voice

        def __init__(self, api) -> None:
            self.api = api

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            good = tmp / "mano_0.silk"
            good.write_bytes(_mn.SILK_MAGIC + b"x" * 512)

            api = _MondayApi()
            await Stub(api).send_voice("G1", good)
            check("★ 上传走 file_type=3（语音 silk）",
                  api.uploads and api.uploads[0]["file_type"] == 3, str(api.uploads))
            check("上传带的是 file_data（字节），不是 url",
                  "file_data" in api.uploads[0] and "url" not in api.uploads[0],
                  str(list(api.uploads[0])))
            check("srv_send_msg=False（先传后发，两步）",
                  api.uploads[0]["srv_send_msg"] is False, str(api.uploads[0]))
            check("★ 正文走 msg_type=7（富媒体）",
                  api.sent and api.sent[0]["msg_type"] == 7, str(api.sent))
            check("media 里带上传拿到的 file_info",
                  api.sent[0]["media"] == {"file_info": "FI-123"}, str(api.sent))
            check("★ **content 是空的** —— 只发语音气泡，用户明确不要字幕",
                  api.sent[0]["content"] == "", str(api.sent))
            check("`send_voice` 的签名里根本没有 content 形参（从根上不给塞）",
                  "content" not in _RB.send_voice.__code__.co_varnames,
                  str(_RB.send_voice.__code__.co_varnames))

            # 200 带业务错误码也算失败
            api_bad = _MondayApi()
            api_bad.fail_send = 40093011
            try:
                await Stub(api_bad).send_voice("G1", good)
                raised = False
            except RuntimeError:
                raised = True
            check("200 带 code 也算失败（不静默当成发送成功）", raised)

            # 非 silk —— **一个请求都不能发出去**
            fake = tmp / "bad.silk"
            fake.write_bytes(b"ID3\x03\x00" + b"x" * 512)
            api2 = _MondayApi()
            try:
                await Stub(api2).send_voice("G1", fake)
                raised2 = False
            except RuntimeError:
                raised2 = True
            check("★ 非 silk → 抛 RuntimeError（上传接口不校验，只能自己挡）", raised2)
            check("★ 非 silk 时**一个请求都没发出去**",
                  not api2.uploads and not api2.sent, f"{api2.uploads} {api2.sent}")

    asyncio.run(run())


def _check_mn_card() -> None:
    """早安卡片：`avatar/` + `.manifest.json` 都在时走「头像+文字」那条路。

    ⚠️ **这一节是补课。** 加卡片之前 `_MorningSender` 连 `send_local_image`
    都没有、`_mn_dir` 也不造 `avatar/`，所以那一千多项里**没有一条**走过
    「头像+文字」—— 缺方法会 AttributeError，但那时所有用例的 `avatar`
    都是 None，根本走不到那条分支。
    """
    import dataclasses  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from qq_rss_bot import morning as _mn  # noqa: PLC0415

    # ---- 纯函数：剥 TTS 语气提示 ----
    check("★ `[softly] ` 剥掉 —— 那是喂 TTS 的，不是给人看的",
          _mn.strip_cue("[softly] あ……おはよう") == "あ……おはよう",
          repr(_mn.strip_cue("[softly] あ……おはよう")))
    check("提示里允许逗号/撇号/连字符（上游只保证这几个字符）",
          _mn.strip_cue("[cheerfully, don't-stop] おはよう") == "おはよう",
          repr(_mn.strip_cue("[cheerfully, don't-stop] おはよう")))
    check("没有提示时原样返回", _mn.strip_cue("おはよう") == "おはよう")
    check("★ 中文方括号**不是**提示（上游只认英文字母），原样留着",
          _mn.strip_cue("[高兴] おはよう") == "[高兴] おはよう",
          repr(_mn.strip_cue("[高兴] おはよう")))
    check("提示后面没词时剥成空串（上游：句尾提示永远不发音）",
          _mn.strip_cue("[softly]") == "", repr(_mn.strip_cue("[softly]")))

    # ---- 纯函数：拼正文 ----
    check("★ 正文是「角色名\\n日语原文」两行",
          _mn.render_content({"name": "櫻木真乃", "ja": "おはよう"})
          == "櫻木真乃\nおはよう",
          repr(_mn.render_content({"name": "櫻木真乃", "ja": "おはよう"})))
    check("缺角色名就少一行（不补空行）",
          _mn.render_content({"name": "", "ja": "おはよう"}) == "おはよう")
    check("缺原文就少一行", _mn.render_content({"name": "櫻木真乃"}) == "櫻木真乃")
    check("★ 两行都空 → 空串（调用方据此退回纯语音）",
          _mn.render_content({"name": "", "ja": ""}) == "")
    check("None / 空 dict → 空串",
          _mn.render_content(None) == "" and _mn.render_content({}) == "")

    # ---- load_lines：读不到一律返回空 dict，**不抛** ----
    with tempfile.TemporaryDirectory() as td:
        mdir = Path(td) / "morning"
        check("★ manifest 不存在 → `{}`（不抛，语音照发）",
              _mn.load_lines(mdir) == {}, str(_mn.load_lines(mdir)))
        mdir.mkdir(parents=True)
        (mdir / ".manifest.json").write_text("{ 这不是 json")
        check("★ JSON 坏了 → `{}`（不抛）", _mn.load_lines(mdir) == {})
        _mn_write_manifest(mdir, ["不是对象"])
        check("★ 顶层不是对象 → `{}`", _mn.load_lines(mdir) == {})
        _mn_write_manifest(mdir, {"a.silk": "不是对象",
                                  "b.silk": {"name": "B", "ja": "x"}})
        check("★ 单条记录不是对象 → 跳过那一条，其余照收",
              list(_mn.load_lines(mdir)) == ["b.silk"], str(_mn.load_lines(mdir)))
        _mn_write_manifest(mdir, {"c.silk": {"ja": "おはよう"}})
        check("缺 `name` → 空串（不是 None，也不 KeyError）",
              _mn.load_lines(mdir)["c.silk"]["name"] == "")
        _mn_write_manifest(mdir, {"d.silk": {"name": "D", "ja": "[softly] おはよう"}})
        check("★ `load_lines` 就把提示剥掉了（不是发的时候才剥）",
              _mn.load_lines(mdir)["d.silk"]["ja"] == "おはよう",
              repr(_mn.load_lines(mdir)["d.silk"]["ja"]))
        _mn_write_manifest(mdir, {"e.silk": {"name": None, "ja": None}})
        check("`name` / `ja` 是 null → 空串（`or \"\"` 兜住）",
              _mn.load_lines(mdir)["e.silk"] == {"name": "", "ja": ""},
              str(_mn.load_lines(mdir)["e.silk"]))
        check("★ 键**就是文件名**，不做任何索引换算",
              list(_mn.load_lines(mdir)) == ["e.silk"], str(list(_mn.load_lines(mdir))))

    # ---- 轮询器：走「头像+文字+语音」那条路 ----
    base = dataclasses.replace(cfg_stub(), morning_time="07:30",
                               morning_recent_days=7)
    old_gap = _mn.MESSAGE_GAP_SEC
    _mn.MESSAGE_GAP_SEC = 0        # 别让 smoke 真等 2 秒 × 群数
    try:
        with tempfile.TemporaryDirectory() as td:
            mdir = _mn_dir(Path(td), idols=("mano", "hiori"), per=2,
                           avatars=True, names=True)
            cfg = dataclasses.replace(base, morning_groups=("G1",), morning_dir=mdir)
            db, sender = _MorningDb(), _MorningSender()
            n = asyncio.run(_mn.MorningPoller(cfg, db, sender).poll_once(
                now=datetime(2026, 10, 20, 7, 30)))

            check("★ 一个群发**两条**：图一条、语音一条",
                  n == 1 and len(sender.images) == 1 and len(sender.voices) == 1,
                  f"n={n} images={sender.images} voices={sender.voices}")
            check("★ 到达顺序是 image → voice（反了群里看着莫名其妙）",
                  sender.events == [("image", "G1"), ("voice", "G1")],
                  str(sender.events))
            idol = Path(sender.images[0][2]).stem
            check("★ 图和语音是**同一位偶像**（选人在循环外，不会错位）",
                  Path(sender.voices[0][1]).stem.rpartition("_")[0] == idol,
                  f"{sender.images[0][2]} vs {sender.voices[0][1]}")
            check("★ 正文 = 角色名 + 剥过提示的原文",
                  sender.images[0][1] == f"{_MN_NAMES[idol]}\n{_MN_JA}",
                  repr(sender.images[0][1]))
            check("头像按 id 取 `avatar/<偶像 id>.png`",
                  Path(sender.images[0][2]).parent.name == "avatar"
                  and Path(sender.images[0][2]).name == f"{idol}.png",
                  sender.images[0][2])
            check("记账记的是 **silk 文件名**，不是索引",
                  db.rows[0][3] == Path(sender.voices[0][1]).name, str(db.rows))

            s2 = _MorningSender()
            n2 = asyncio.run(_mn.MorningPoller(cfg, db, s2).poll_once(
                now=datetime(2026, 10, 20, 7, 30)))
            check("同一 slot 第二次 → 0 条，且**一条消息都没发**",
                  n2 == 0 and not s2.images and not s2.voices, f"n={n2}")

        # ---- 失败语义（两条路的处理**故意不一样**）----
        with tempfile.TemporaryDirectory() as td:
            mdir = _mn_dir(Path(td), idols=("mano",), per=1,
                           avatars=True, names=True)
            cfg = dataclasses.replace(base, morning_groups=("G1", "G2", "G3"),
                                      morning_dir=mdir)
            slot_key = "2026-10-20T07:30"
            now = datetime(2026, 10, 20, 7, 30)

            # ① 发图挂了 → 整轮不算数：不记账，窗口内下一拍重试
            db1, s1 = _MorningDb(), _MorningSender(fail_image={"G2"})
            n1 = asyncio.run(_mn.MorningPoller(cfg, db1, s1).poll_once(now=now))
            check("★ 发图失败 → 那个群**不记账**（窗口内下一拍会重试）",
                  n1 == 2 and db1.morning_posted(slot_key) == {"G1", "G3"},
                  f"n={n1} posted={db1.morning_posted(slot_key)}")
            check("★ 发图失败的群**连语音都没发**（不能只发半条）",
                  {g for g, _ in s1.voices} == {"G1", "G3"}, str(s1.voices))

            # ② 语音挂了 → 只记 WARNING，**照样记账**（图已经发出去了）
            db2, s2 = _MorningDb(), _MorningSender(fail={"G2"})
            n2 = asyncio.run(_mn.MorningPoller(cfg, db2, s2).poll_once(now=now))
            check("★ 语音失败 → **仍然记账**（图已发，再退一次就是重复刷屏）",
                  n2 == 3 and db2.morning_posted(slot_key) == {"G1", "G2", "G3"},
                  f"n={n2} posted={db2.morning_posted(slot_key)}")
            check("语音失败的那个群**图照样发出去了**",
                  {g for g, _, _ in s2.images} == {"G1", "G2", "G3"}, str(s2.images))
            check("★ 一个群失败不连累别的群（三群各自独立）",
                  s1.events == [("image", "G1"), ("voice", "G1"),
                                ("image", "G3"), ("voice", "G3")],
                  str(s1.events))

        # ---- 缺材料 → 退回**纯语音**（= 加这个功能之前的行为）----
        for label, kw in (("缺头像", {"avatars": False, "names": True}),
                          ("缺台词表", {"avatars": True, "names": False}),
                          ("两个都缺", {"avatars": False, "names": False})):
            with tempfile.TemporaryDirectory() as td:
                mdir = _mn_dir(Path(td), idols=("mano",), per=1, **kw)
                cfg = dataclasses.replace(base, morning_groups=("G1",),
                                          morning_dir=mdir)
                s = _MorningSender()
                n = asyncio.run(_mn.MorningPoller(cfg, _MorningDb(), s).poll_once(
                    now=datetime(2026, 10, 20, 7, 30)))
                check(f"★ {label} → 退回**纯语音**（不是发一条光秃秃的图，也不是不发）",
                      n == 1 and not s.images and len(s.voices) == 1,
                      f"n={n} images={s.images} voices={s.voices}")
    finally:
        _mn.MESSAGE_GAP_SEC = old_gap

    # ---- 源码钉子 ----
    src = inspect.getsource(_mn.MorningPoller.poll_once)
    check("★ 间隔读的是**模块全局** `MESSAGE_GAP_SEC`（不是构造时捕获的副本，"
          "否则上面那些 patch 全是假的）",
          "asyncio.sleep(MESSAGE_GAP_SEC)" in src,
          str([ln.strip() for ln in src.splitlines() if "sleep" in ln]))
    check("默认间隔是 2.0 秒（2026-10-20 手工实测用的就是这个）",
          _mn.MESSAGE_GAP_SEC == 2.0, str(_mn.MESSAGE_GAP_SEC))
    check("两条消息之间**只有一次** sleep（多发一次就是白等）",
          src.count("asyncio.sleep") == 1, str(src.count("asyncio.sleep")))
    check("★ 头像**按 id 取文件，不扫目录**（源码里没有 glob）",
          "avatar_dir /" in src and "glob" not in src, "")


def _check_mn_send_local_image() -> None:
    """`bot.send_local_image` —— 「文字 + 本地图片」那条路（`file_type=1`）。"""
    import base64  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import preview as _pv  # noqa: PLC0415
    from qq_rss_bot.bot import RssBot as _RB  # noqa: PLC0415

    class Stub:
        send_local_image = _RB.send_local_image

        def __init__(self, api) -> None:
            self.api = api

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "mano.png"
            png.write_bytes(_MN_PNG)

            api = _MondayApi()
            await Stub(api).send_local_image("G1", "櫻木真乃\nおはよう", png)
            check("★ 上传走 file_type=1（图片）",
                  api.uploads and api.uploads[0]["file_type"] == 1, str(api.uploads))
            check("上传带的是 file_data（base64），不是 url",
                  "file_data" in api.uploads[0] and "url" not in api.uploads[0],
                  str(list(api.uploads[0])))
            check("srv_send_msg=False（先传后发，两步）",
                  api.uploads[0]["srv_send_msg"] is False, str(api.uploads[0]))
            check("★ base64 的**是文件字节**（不是路径、不是空）",
                  base64.b64decode(api.uploads[0]["file_data"]) == _MN_PNG)
            check("★ 正文走 msg_type=7（富媒体）",
                  api.sent and api.sent[0]["msg_type"] == 7, str(api.sent))
            check("media 里带上传拿到的 file_info",
                  api.sent[0]["media"] == {"file_info": "FI-123"}, str(api.sent))
            check("★ **content 是渲染的** —— 文字就搭在图上（和视频那条正好相反）",
                  api.sent[0]["content"] == "櫻木真乃\nおはよう",
                  repr(api.sent[0]["content"]))
            check("★ 签名里有 content 形参（文字只能从这儿进去）",
                  "content" in _RB.send_local_image.__code__.co_varnames,
                  str(_RB.send_local_image.__code__.co_varnames))
            check("对照：`send_video` 签名里**没有** content（视频的 content 会被丢掉）",
                  "content" not in _RB.send_video.__code__.co_varnames,
                  str(_RB.send_video.__code__.co_varnames))

            # 上传没拿到 file_info → 抛，且**一条消息都不发**
            api2 = _MondayApi()
            api2.no_file_info = True
            try:
                await Stub(api2).send_local_image("G1", "x", png)
                r2 = False
            except RuntimeError:
                r2 = True
            check("★ 上传没拿到 file_info → 抛 RuntimeError", r2)
            check("★ 那时**一条消息都没发**（不发空图）",
                  not api2.sent, str(api2.sent))

            # 200 带业务错误码也算失败
            api3 = _MondayApi()
            api3.fail_send = 40093011
            try:
                await Stub(api3).send_local_image("G1", "x", png)
                r3 = False
            except RuntimeError:
                r3 = True
            check("200 带 code 也算失败（不静默当成发送成功）", r3)

            # base64 之后超限 → **一个请求都不发**
            api4 = _MondayApi()
            old = _pv.MAX_FILE_DATA_BYTES
            _pv.MAX_FILE_DATA_BYTES = 16
            try:
                try:
                    await Stub(api4).send_local_image("G1", "x", png)
                    r4 = False
                except RuntimeError:
                    r4 = True
            finally:
                _pv.MAX_FILE_DATA_BYTES = old
            check("★ base64 后超 5 MiB → 抛 RuntimeError（**限的是 base64，不是原始字节**）",
                  r4)
            check("★ 超限时**一个请求都没发出去**",
                  not api4.uploads and not api4.sent, f"{api4.uploads} {api4.sent}")
            check("对照：`MAX_FILE_DATA_BYTES` 是 5 MiB（= 原图 3.75 MiB 余量）",
                  old == 5 * 1024 * 1024, str(old))

    asyncio.run(run())


def _check_mn_lines_table() -> None:
    """台词表本身的校验 —— 有 `data/morning/lines.toml` 才跑（`data/` 不进 git）。"""
    import sys as _sys  # noqa: PLC0415

    _sys.path.insert(0, str(_MN_ROOT / "scripts"))
    import make_morning as _mm  # noqa: PLC0415

    bad = [{
        "id": "x", "voice_id": "a" * 32,
        "lines": [
            {"ja": "[softly] おはようございます", "zh": ""},            # 缺 zh
            {"ja": "おはようございます", "zh": "早上好"},                # 没有提示
            {"ja": "[quietly] おはようございます", "zh": "x"},           # 词表外
            {"ja": "[softly] おはようございます[gently]", "zh": "x"},    # 提示在句尾
            {"ja": "[softly] おはよう ABC", "zh": "x"},                  # 拉丁字母
            {"ja": "[softly] おはよう♡", "zh": "x"},                     # 符号
            {"ja": "[softly] あ", "zh": "x"},                            # 太短
        ],
    }]
    problems = _mm.validate_lines(bad)
    joined = "\n".join(problems)
    check("缺 zh 被点名", "缺 zh" in joined, joined[:200])
    check("★ 没有语气提示的台词被点名（否则整句只会平着念）",
          "没有语气提示" in joined, joined[:200])
    check("词表外的提示被点名", "不在词表里" in joined, joined[:200])
    check("★ 提示写在**句尾**被点名（上游：句尾的提示永远不会被念出来）",
          "[softly] おはようございます[gently]" in joined and "方括号不是恰好一对" in joined,
          joined[:300])
    check("正文里的拉丁字母被点名", "拉丁字母" in joined, joined[:200])
    check("正文里的符号/颜文字被点名", "表情/颜文字" in joined, joined[:200])
    check("太短的正文被点名", "超出" in joined, joined[:200])

    table = _MN_ROOT / "data" / "morning" / "lines.toml"
    if not table.is_file():
        print("  SKIP  没有 data/morning/lines.toml（data/ 不进 git）")
        return
    try:
        idols = _mm.load_lines(table)
        total = sum(len(i["lines"]) for i in idols)
        check(f"★ 真实台词表 {len(idols)} 位 / {total} 条全部通过校验",
              len(idols) == 28 and total >= 28, f"{len(idols)} 位 {total} 条")
        check("每位都有 voice_id（32 位十六进制）",
              all(len(i["voice_id"]) == 32 and all(c in "0123456789abcdef" for c in i["voice_id"])
                  for i in idols))
    except _mm.Fatal as exc:
        check("真实台词表通过校验", False, str(exc)[:400])


_check_mn_parts()
_check_mn_list_voices()
_check_mn_poller()
_check_mn_send_voice()
_check_mn_card()
_check_mn_send_local_image()
_check_mn_lines_table()

# ---------------------------------------------------------------------------
# 偶像生日推送
# ---------------------------------------------------------------------------

print("[4.96] 偶像生日推送（全离线）")

from datetime import date  # noqa: E402


def _bd_html() -> str:
    """一份形似 x.com 时间线的片段（**结构照着 2026-10-19 抓的真实页面写**）。

    三个条目：当天的生日推（带图）、前一天那条（该被日期判据排掉）、
    以及一条**被引用成卡片**的同一条生日推（该被 id 去重排掉）。
    """
    def entry(tid: str, ts: str, text: str, media: str = "") -> str:
        img = f'<img src="https://pbs.twimg.com/media/{media}?format=jpg&amp;name=900x900">' if media else ""
        return (
            f'<li><div><a data-href="/imas_ch/status/{tid}" href="/imas_ch/status/{tid}">'
            f'<time datetime="x"><script>{{"timestamp":{ts}}}</script></time>'
            f'<div class="css-1 whitespace-pre-wrap break-words dir="auto">{text}</div>'
            f'{img}</a></div></li>'
        )

    return (
        "<html><body><ol>"
        + entry("2111834690130674164", "1792335600000",
                "10/19 杜野凛世さん お誕生日おめでとうございます🎂", "HTnBd4ebEAAEJjB")
        + entry("2111472303410217188", "1792249201000",
                "10/18 小早川紗枝さん 水本ゆかりさん お誕生日おめでとうございます🎂",
                "HTnBd4ebEAAEJjA")
        # 同一条又被引用了一次 —— 不去重的话图片会发两张
        + entry("2111834690130674164", "1792335600000",
                "10/19 杜野凛世さん お誕生日おめでとうございます🎂", "HTnBd4ebEAAEJjB")
        # 非生日推（同一天的配信通知），日期对不上也不该被挑中
        + entry("2110659129304047706", "1792084125000", "10月メンバー限定配信のお知らせ")
        + "</ol></body></html>"
    )


class _BdDb:
    """只实现 `BirthdayPoller` 用到的那两个方法。"""

    def __init__(self) -> None:
        self.posted: dict[str, set[str]] = {}
        self.rows: list[tuple[str, str, str]] = []   # (day, group, tweet)

    def birthday_posted(self, day: str) -> set[str]:
        return set(self.posted.get(day, ()))

    def mark_birthday_posted(self, day: str, group: str, tweet: str) -> None:
        self.posted.setdefault(day, set()).add(group)
        self.rows.append((day, group, tweet))


class _BdSender:
    """假 sender。`fail` 里的群发图片抛异常（验逐群隔离）。"""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.images: list[tuple[str, str, bytes]] = []
        self.texts: list[tuple[str, str]] = []
        self.fail = fail or set()

    async def send_text(self, group: str, content: str) -> None:
        self.texts.append((group, content))

    async def send_local_image(self, group: str, content: str, path) -> None:
        if group in self.fail:
            raise RuntimeError("模拟发送失败")
        self.images.append((group, content, Path(path).read_bytes()))


class _BdResp:
    def __init__(self, text: str, blob: bytes = b"") -> None:
        self.text = text
        self.content = blob

    def raise_for_status(self) -> None:
        pass


class _BdHttp:
    """假客户端 —— **记录请求过哪些 URL**，好钉住「走的是 media_http 那条路」。"""

    def __init__(self, page: str, blob: bytes = b"\xff\xd8JPEG") -> None:
        self.page = page
        self.blob = blob
        self.urls: list[str] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        # ⚠️ 图片那条要把字节放进 **content**（`preview.fetch_image` 读的是它），
        # 塞进 text 的话 `content` 是空的，会被当成「下载失败」。
        if "pbs.twimg.com" in url:
            return _BdResp("", self.blob)
        return _BdResp(self.page)


def _bd_cal(tmp: Path) -> Path:
    """造一份小冷数据（**不读真实的 `data/birthday/`**，那份在下面单独验）。"""
    d = tmp / "birthday"
    d.mkdir(parents=True, exist_ok=True)
    (d / "birthdays.toml").write_text(
        'source = "https://imas-db.jp/calendar/birthdays.html"\n'
        "[brands]\nshinycolors = \"シャイニーカラーズ\"\nsidem = \"SideM\"\n"
        '[[days]]\ndate = "10-19"\n'
        'idols = [ { name = "杜野凛世", brand = "shinycolors" } ]\n'
        'people = [ { name = "榎木淳弥", role = "舞田類", brand = "sidem" },'
        ' { name = "無役さん", brand = "sidem" } ]\n'
        '[[days]]\ndate = "10-20"\n',   # ⚠️ 没人过生日的那天**没有 idols/people 键**
        encoding="utf-8",
    )
    return d


def _check_bd_parts() -> None:
    from datetime import datetime  # noqa: PLC0415

    from qq_rss_bot import birthday as _bd  # noqa: PLC0415

    check("★ 默认推送时间是 23:05（用户定的）",
          _bd.DEFAULT_TIME == "23:05", _bd.DEFAULT_TIME)
    check("cfg_stub 的 birthday_time 就是那个默认值",
          cfg_stub().birthday_time == _bd.DEFAULT_TIME, cfg_stub().birthday_time)
    check("cfg_stub 的 birthday_window_sec 就是那个默认值",
          cfg_stub().birthday_window_sec == _bd.DEFAULT_WINDOW_SEC)
    check("窗口比早安那条长（要留抓图重试的时间）",
          _bd.DEFAULT_WINDOW_SEC > 180, str(_bd.DEFAULT_WINDOW_SEC))

    at = _bd.parse_time("23:05")
    check("23:04 → 昨天 23:05",
          _bd.slot_for(datetime(2026, 10, 18, 23, 4), at) == datetime(2026, 10, 17, 23, 5),
          str(_bd.slot_for(datetime(2026, 10, 18, 23, 4), at)))
    check("★ 正好 23:05 → **今天** 23:05（含边界）",
          _bd.slot_for(datetime(2026, 10, 18, 23, 5), at) == datetime(2026, 10, 18, 23, 5),
          str(_bd.slot_for(datetime(2026, 10, 18, 23, 5), at)))
    check("23:06 → 今天 23:05",
          _bd.slot_for(datetime(2026, 10, 18, 23, 6), at) == datetime(2026, 10, 18, 23, 5),
          str(_bd.slot_for(datetime(2026, 10, 18, 23, 6), at)))

    # ★★ 整个功能最容易写错的一行
    check("★★ 23:05 的 slot 祝福的是**第二天**（10-18 推 10-19）",
          _bd.birthday_date(datetime(2026, 10, 18, 23, 5)) == date(2026, 10, 19),
          str(_bd.birthday_date(datetime(2026, 10, 18, 23, 5))))
    check("★★ 跨月也对（10-31 推 11-01）",
          _bd.birthday_date(datetime(2026, 10, 31, 23, 5)) == date(2026, 11, 1),
          str(_bd.birthday_date(datetime(2026, 10, 31, 23, 5))))
    check("★★ 跨年也对（12-31 推 01-01）",
          _bd.birthday_date(datetime(2026, 12, 31, 23, 5)) == date(2027, 1, 1),
          str(_bd.birthday_date(datetime(2026, 12, 31, 23, 5))))
    check("★★ 闰日也对（02-28 推 02-29）",
          _bd.birthday_date(datetime(2028, 2, 28, 23, 5)) == date(2028, 2, 29),
          str(_bd.birthday_date(datetime(2028, 2, 28, 23, 5))))

    # ---- 文案 ----
    day = _bd.Day(
        idols=(_bd.Person("杜野凛世", "shinycolors"),),
        people=(_bd.Person("榎木淳弥", "sidem", "舞田類"), _bd.Person("無役さん", "sidem")),
    )
    text = _bd.render(date(2026, 10, 19), day)
    check("★ 文案逐字等于用户给的样例（偶像在前、声优在后、带 役）",
          text == "🎂 2026/10/19 本日のお誕生日:\n杜野凛世\n榎木淳弥 (舞田類役)\n無役さん",
          repr(text))
    check("声优没有角色时不硬拼括号", "(役)" not in text.splitlines()[-1], text)
    check("空名单渲染出来只有一行标题", _bd.render(date(2026, 10, 19), _bd.Day()).count("\n") == 0)


def _check_bd_calendar() -> None:
    import tempfile  # noqa: PLC0415

    from qq_rss_bot import birthday as _bd  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        cal = _bd.load_calendar(_bd_cal(Path(td)) / "birthdays.toml")
        check("读到两天", set(cal.days) == {"10-19", "10-20"}, str(sorted(cal.days)))
        check("品牌表读进来", cal.brands.get("shinycolors") == "シャイニーカラーズ", str(cal.brands))
        d19 = cal.for_date(date(2026, 10, 19))
        check("10-19 有 1 位偶像 + 2 位声优", len(d19.idols) == 1 and len(d19.people) == 2, str(d19))
        check("声优带角色", d19.people[0].role == "舞田類", str(d19.people))
        check("空的那天不是错（total=0）", cal.for_date(date(2026, 10, 20)).empty)
        check("查不到的日子返回空 Day，不抛", cal.for_date(date(2026, 5, 1)).empty)
        # ⚠️ 没人过生日的那天**根本没有 idols/people 键**（真实数据就是这样），
        # 读取端必须 .get —— 写成 entry["idols"] 会 KeyError
        check("★ 没人过生日的那天没有 idols/people 键，也能读",
              cal.days["10-20"].idols == () and cal.days["10-20"].people == ())
        try:
            _bd.load_calendar(Path(td) / "nope.toml")
            raised = False
        except Exception:
            raised = True
        check("★ 冷数据读不了就抛（上层记 ERROR 并关掉功能，绝不静默当成「今天没人过生日」）",
              raised)


def _check_bd_timeline() -> None:
    from qq_rss_bot import birthday as _bd  # noqa: PLC0415

    tweets = _bd.parse_timeline(_bd_html())
    check("解析出 3 条（4 个 <li>，被引用的那条去重掉一个）",
          len(tweets) == 3, str([t.id for t in tweets]))
    check("★ 被引用成卡片的同一条**去重**（不去重会发两张一样的图）",
          [t.id for t in tweets].count("2111834690130674164") == 1,
          str([t.id for t in tweets]))
    first = tweets[0]
    check("正文取出来了", "杜野凛世" in first.text and "お誕生日おめでとう" in first.text,
          repr(first.text))
    check("配图 id 取出来了", first.images == ("HTnBd4ebEAAEJjB",), str(first.images))
    check("时间戳换成了本地 datetime",
          first.posted_at is not None and first.posted_at.year == 2026, str(first.posted_at))
    check("★ 图 URL 强制 `name=orig`（原图，产品明确要求）",
          first.image_url().endswith("?format=jpg&name=orig"), first.image_url())
    check("结构不认识时返回空表（上层按「没抓到图」发纯文字）",
          _bd.parse_timeline("<html>什么都没有</html>") == [])

    hits = _bd.pick_birthday_tweets(tweets, date(2026, 10, 19))
    check("★ 挑当天的生日推（按**正文日期 + 祝福语**，不是「最新一条」）",
          [t.id for t in hits] == ["2111834690130674164"], str([t.id for t in hits]))
    check("日期对不上的不挑", _bd.pick_birthday_tweets(tweets, date(2026, 10, 15)) == [])
    check("只有祝福语没有日期的不挑（非生日推）",
          _bd.pick_birthday_tweets(tweets, date(2026, 10, 10)) == [])
    check("11/1 不会被 1/1 之类的前缀误匹配",
          _bd.pick_birthday_tweets(
              [_bd.Tweet("1", None, "11/1 お誕生日おめでとう", ())], date(2026, 1, 1)) == [])


def _check_bd_poller() -> None:
    import dataclasses  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from qq_rss_bot import birthday as _bd  # noqa: PLC0415

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            bdir = _bd_cal(Path(td))
            cfg = dataclasses.replace(
                cfg_stub(), birthday_groups=("G1", "G2"),
                birthday_dir=bdir, birthday_time="23:05", birthday_window_sec=300,
            )
            page = _bd_html()
            tweets = _bd.parse_timeline(page)
            at_slot = datetime(2026, 10, 18, 23, 5)

            # ---- 抓图那拍：发图文，两个群同一条 ----
            db, sender, http = _BdDb(), _BdSender(), _BdHttp(page)
            p = _bd.BirthdayPoller(cfg, db, sender, http)
            n = await p.poll_once(now=at_slot)
            check("★ 23:05 准点抓到图，发给两个群", n == 2, str(n))
            check("★ 两个群拿到**同一份文案和同一张图**（多群不会精神分裂）",
                  len({i[1] for i in sender.images}) == 1
                  and len({i[2] for i in sender.images}) == 1, str(sender.images))
            check("发的是**图文**（`send_local_image`），不是纯文字",
                  not sender.texts and len(sender.images) == 2, f"{sender.images} {sender.texts}")
            check("★ 文案就是渲染出来的那份",
                  sender.images[0][1].startswith("🎂 2026/10/19"), sender.images[0][1])
            check("★ 落库的键是**被祝福的那天**（10-19），不是推送那天（10-18）",
                  db.birthday_posted("2026-10-19") == {"G1", "G2"} and not db.birthday_posted("2026-10-18"),
                  str(db.posted))
            check("落库记了推文 id", all(r[2] == "2111834690130674164" for r in db.rows), str(db.rows))
            check("★ 时间线走的是**传进来的那个 http**（线上是带代理的 media_http）",
                  http.urls and http.urls[0] == _bd.X_PROFILE_URL, str(http.urls))
            check("★ 抓的是 imas_ch 那条推的图，且 `name=orig`",
                  any("pbs.twimg.com/media/HTnBd4ebEAAEJjB" in u and "name=orig" in u
                      for u in http.urls), str(http.urls))

            n2 = await p.poll_once(now=datetime(2026, 10, 18, 23, 7))
            check("★ 同一天再跑不会重发（幂等靠库，不靠内存）", n2 == 0, str(n2))

            # ---- 没抓到图：还早 → 留着下一拍重试 ----
            db2, snd2 = _BdDb(), _BdSender()
            p2 = _bd.BirthdayPoller(cfg, db2, snd2, _BdHttp("<html>空</html>"))
            n3 = await p2.poll_once(now=at_slot)
            check("★ 没抓到图 + 还在窗口前段 → **不发**，留着下一拍重试",
                  n3 == 0 and not snd2.images and not snd2.texts, f"{n3}")
            check("没发就**不记账**", not db2.rows, str(db2.rows))

            # ---- 没抓到图：到窗口末尾 → 发纯文字 ----
            n4 = await p2.poll_once(now=datetime(2026, 10, 18, 23, 9))
            check("★ 窗口末尾还没图 → **发纯文字**（名单从不依赖推文）",
                  n4 == 2 and len(snd2.texts) == 2 and not snd2.images, str(n4))
            check("纯文字也带完整名单",
                  "杜野凛世" in snd2.texts[0][1] and "榎木淳弥 (舞田類役)" in snd2.texts[0][1],
                  snd2.texts[0][1])
            check("★ 没图时记账写**空串**（能分清「当时没图」和「当时没记」）",
                  all(r[2] == "" for r in db2.rows), str(db2.rows))

            # ---- 窗口外不补发 ----
            db3, snd3 = _BdDb(), _BdSender()
            p3 = _bd.BirthdayPoller(cfg, db3, snd3, _BdHttp(page))
            n5 = await p3.poll_once(now=datetime(2026, 10, 18, 23, 20))
            check("★ 窗口外（23:20）不发", n5 == 0 and not snd3.images and not snd3.texts, str(n5))

            # ---- 那天没人过生日 ----
            db4, snd4 = _BdDb(), _BdSender()
            p4 = _bd.BirthdayPoller(cfg, db4, snd4, _BdHttp(page))
            n6 = await p4.poll_once(now=datetime(2026, 10, 19, 23, 5))   # → 祝福 10-20
            check("★ 那天没人过生日 → 静默跳过、不记账、不报错",
                  n6 == 0 and not db4.rows and not snd4.images, f"{n6} {db4.rows}")

            # ---- 一个群挂了不连累别的 ----
            db5, snd5 = _BdDb(), _BdSender(fail={"G1"})
            p5 = _bd.BirthdayPoller(cfg, db5, snd5, _BdHttp(page))
            n7 = await p5.poll_once(now=at_slot)
            check("★ 一个群发不出去，其余群照发",
                  n7 == 1 and db5.birthday_posted("2026-10-19") == {"G2"}, f"{n7} {db5.posted}")

            # ---- 冷数据缺失 / 未配置群 ----
            cfg6 = dataclasses.replace(cfg, birthday_dir=Path(td) / "nope")
            snd6 = _BdSender()
            n8 = await _bd.BirthdayPoller(cfg6, _BdDb(), snd6, _BdHttp(page)).poll_once(now=at_slot)
            check("★ 冷数据不存在 → 返回 0、**不抛**（不能连累机器人起不来）",
                  n8 == 0 and not snd6.images, str(n8))

            cfg7 = dataclasses.replace(cfg, birthday_groups=())
            snd7 = _BdSender()
            n9 = await _bd.BirthdayPoller(cfg7, _BdDb(), snd7, _BdHttp(page)).poll_once(now=at_slot)
            check("BIRTHDAY_GROUPS 空 → 整个功能关闭（返回 0）",
                  n9 == 0 and not snd7.images, str(n9))

            # ---- 抓时间线炸了 → 当成没图，到窗口末尾发纯文字 ----
            class _BoomHttp:
                async def get(self, url, **kw):
                    raise RuntimeError("模拟 SSL 超时")

            db8, snd8 = _BdDb(), _BdSender()
            p8 = _bd.BirthdayPoller(cfg, db8, snd8, _BoomHttp())
            n10 = await p8.poll_once(now=at_slot)
            n11 = await p8.poll_once(now=datetime(2026, 10, 18, 23, 9))
            check("★ 抓推文抛异常也不炸：窗口末尾照样发纯文字",
                  n10 == 0 and n11 == 2 and len(snd8.texts) == 2, f"{n10} {n11}")

    asyncio.run(run())


def _check_bd_real_table() -> None:
    """真实冷数据 —— 有 `data/birthday/birthdays.toml` 才跑（`data/` 不进 git）。"""
    from qq_rss_bot import birthday as _bd  # noqa: PLC0415

    table = _MN_ROOT / "data" / "birthday" / "birthdays.toml"
    if not table.is_file():
        print("  SKIP  没有 data/birthday/birthdays.toml（data/ 不进 git）")
        return
    cal = _bd.load_calendar(table)
    check("★ 真实冷数据：366 天", len(cal.days) == 366, str(len(cal.days)))
    check("★ 02-29 在里面（闰年才查得到）", "02-29" in cal.days)
    d = cal.for_date(date(2026, 10, 19))
    check("★ 2026-10-19 的名单逐字等于用户给的样例",
          _bd.render(date(2026, 10, 19), d) == (
              "🎂 2026/10/19 本日のお誕生日:\n杜野凛世\n"
              "榎木淳弥 (舞田類役)\n大塚剛央 (眉見鋭心役)\n土屋翔 (舞台SideM 伊瀬谷四季役)"
          ),
          repr(_bd.render(date(2026, 10, 19), d)))
    check("★ 不过滤品牌：961pro / xenoglossia / others 都在名单里",
          {"961pro", "xenoglossia", "others"} <= {
              p.brand for day in cal.days.values() for p in day.idols + day.people
          },
          str(sorted(cal.brands)))
    check("有人的天数和空的天数加起来等于 366",
          sum(1 for x in cal.days.values() if not x.empty)
          + sum(1 for x in cal.days.values() if x.empty) == 366)


def _check_bd_wiring() -> None:
    """接线钉子。这三条都是**只看源码**的 —— 但改错了后果很隐蔽。

    尤其第一条：把 `media_http` 换成 `rss_http` 之后代码照样跑、日志照样正常，
    只是容器里永远抓不到推文，症状是「每天都没配图」。
    """
    import inspect  # noqa: PLC0415

    from qq_rss_bot.bot import RssBot  # noqa: PLC0415

    init = inspect.getsource(RssBot.__init__)
    line = next((ln for ln in init.splitlines() if "BirthdayPoller(" in ln), "")
    check("★ 生日轮询器接的是 **media_http**（带代理的那个）",
          "self.media_http" in line and "rss_http" not in line, line.strip())
    check("挂的是 self.birthday / self._birthday_task",
          "self.birthday = BirthdayPoller" in init and "self._birthday_task" in init)
    check("on_ready 里起了这个 task",
          "self.birthday.run()" in inspect.getsource(RssBot.on_ready))
    check("close() 里停了它（否则停机要等满窗口）",
          "self.birthday.stop()" in inspect.getsource(RssBot.close))

    from qq_rss_bot import quota as _q  # noqa: PLC0415

    check("Pusher 协议里有 send_text / send_local_image（生日两条路都用到）",
          hasattr(_q.Pusher, "send_text") and hasattr(_q.Pusher, "send_local_image"))


_check_bd_parts()
_check_bd_calendar()
_check_bd_timeline()
_check_bd_poller()
_check_bd_real_table()
_check_bd_wiring()

print("[4.97] 誕生祭 贺图聚合（全离线）")


def _ba_db():
    class _Db:
        """只实现 `BirthdayArtPoller` 用到的那两个方法。"""

        def __init__(self) -> None:
            self.posted: dict[str, set[str]] = {}
            self.rows: list[tuple[str, str, int, str]] = []

        def birthday_art_posted(self, day: str) -> set[str]:
            return set(self.posted.get(day, ()))

        def mark_birthday_art(self, day, group, sent, tweets) -> None:
            self.posted.setdefault(day, set()).add(group)
            self.rows.append((day, group, sent, tweets))

        def clear_birthday_art(self, day: str, group: str) -> bool:
            had = group in self.posted.get(day, set())
            self.posted.get(day, set()).discard(group)
            return had

    return _Db()


class _BaSender:
    """假 sender。`fail_first` 里的群**第一张**就抛；`fail_at` 精确到 (群, 第几个)。"""

    def __init__(self, fail_first=(), fail_at=()) -> None:
        self.sent: list[tuple[str, str, bytes]] = []
        self.fail_first = set(fail_first)
        self.fail_at = set(fail_at)
        self._n: dict[str, int] = {}

    async def send_local_image(self, group: str, content: str, path) -> None:
        i = self._n.get(group, 0)
        self._n[group] = i + 1
        if group in self.fail_first and i == 0:
            raise RuntimeError("模拟首图失败")
        if (group, i) in self.fail_at:
            raise RuntimeError("模拟中间某张失败")
        self.sent.append((group, content, Path(path).read_bytes()))


class _BaResp:
    def __init__(self, blob: bytes) -> None:
        self.content = blob
        self.text = ""

    def raise_for_status(self) -> None:
        pass


class _BaHttp:
    """假客户端。`dead` 里的 URL 抛错（模拟下不动 / 原图过大）。"""

    def __init__(self, blob: bytes = b"\xff\xd8JPEG-ba", dead: set[str] | None = None) -> None:
        self.blob = blob
        self.dead = dead or set()
        self.urls: list[str] = []

    async def get(self, url, **kw):
        self.urls.append(url)
        if any(d in url for d in self.dead):
            raise httpx.HTTPError("模拟下载失败")
        return _BaResp(self.blob)


def _ba_cfg(tmp: Path, **over):
    import dataclasses as _dc  # noqa: PLC0415

    base = _dc.replace(
        cfg_stub(),
        birthday_dir=tmp / "birthday",
        birthday_art_groups=("G1",),
        x_auth_token="a" * 40,
        x_ct0="c" * 160,
    )
    return _dc.replace(base, **over) if over else base


def _ba_art(
    i: int, *, likes: int, idol="杜野凛世", author=None, name=None, image=True, sensitive=False
):
    from qq_rss_bot.birthday_art import Art  # noqa: PLC0415

    return Art(
        id=str(2111000000000000000 + i),
        idol=idol,
        author=author or f"artist{i}",
        author_name=name or f"画师{i}",
        likes=likes,
        posted_at=datetime(2026, 10, 19, 12, 0, tzinfo=ba.JST),
        image=f"https://pbs.twimg.com/media/IMG{i}.jpg?name=orig" if image else "",
        sensitive=sensitive,
    )


def _ba_parts() -> None:
    from datetime import time as _t  # noqa: PLC0415

    check("★ 默认时刻是 22:30（不是用户原来说的 12:00）", ba.DEFAULT_TIME == "22:30")
    check("四个 tag 顺序：带年份的在前面",
          ba.tags_for("杜野凛世", 2026) == [
              "#杜野凛世誕生祭2026", "#杜野凛世生誕祭2026",
              "#杜野凛世誕生祭", "#杜野凛世生誕祭"],
          str(ba.tags_for("杜野凛世", 2026)))
    check("is_yearful 判得对",
          ba.is_yearful("#杜野凛世誕生祭2026", 2026)
          and not ba.is_yearful("#杜野凛世誕生祭", 2026))
    check("parse_time 收 HH:MM", ba.parse_time("22:30") == _t(22, 30))
    try:
        ba.parse_time("22.30")
        check("parse_time 写错要 SystemExit", False)
    except SystemExit:
        check("parse_time 写错要 SystemExit", True)

    # ★ 「被庆祝的那天」= slot **当天**，和 birthday.birthday_date 差一天
    for iso, want in [("2026-10-19T22:30", "2026-10-19"),
                      ("2026-12-31T22:30", "2026-12-31"),
                      ("2026-11-01T22:30", "2026-11-01"),
                      ("2028-02-29T22:30", "2028-02-29")]:
        got = ba.art_date(datetime.fromisoformat(iso)).isoformat()
        check(f"★ art_date({iso}) = {want}（slot 当天，不是 +1）", got == want, got)
    check("★ 和 birthday.birthday_date 确实差一天",
          ba.art_date(datetime(2026, 10, 19, 22, 30))
          != birthday.birthday_date(datetime(2026, 10, 19, 22, 30)))

    # slot_for
    check("22:29 → 昨天 22:30",
          ba.slot_for(datetime(2026, 10, 19, 22, 29), _t(22, 30))
          == datetime(2026, 10, 18, 22, 30))
    check("正好 22:30 → 当天（不是昨天）",
          ba.slot_for(datetime(2026, 10, 19, 22, 30), _t(22, 30))
          == datetime(2026, 10, 19, 22, 30))

    # 时间窗用 JST 判
    d = date(2026, 10, 19)
    lo, hi = ba.window_bounds(d)
    check("时间窗是 [D-1, D+2) 的 JST 零点",
          (lo, hi) == (datetime(2026, 10, 18, tzinfo=ba.JST),
                       datetime(2026, 10, 21, tzinfo=ba.JST)))
    check("D-1 那天的在窗内（提前画好的）",
          ba.in_window(datetime(2026, 10, 18, 5, 0, tzinfo=ba.JST), d))
    check("D+1 那天的在窗内（迟到的）",
          ba.in_window(datetime(2026, 10, 20, 23, 0, tzinfo=ba.JST), d))
    check("★ 去年的同类 tag 结果被排掉（不带年份的 tag 会搜到往年作品）",
          not ba.in_window(datetime(2025, 10, 19, 12, 0, tzinfo=ba.JST), d))
    check("D+2 之后排掉", not ba.in_window(datetime(2026, 10, 21, 0, 0, tzinfo=ba.JST), d))

    # 图片 URL
    check("_orig 补 ?name=orig", ba._orig("https://pbs.twimg.com/media/X.jpg")
          == "https://pbs.twimg.com/media/X.jpg?name=orig")
    check("_orig 已有 name= 就不动",
          ba._orig("https://pbs.twimg.com/media/X.jpg?name=small")
          == "https://pbs.twimg.com/media/X.jpg?name=small")
    check("_with_name 换档",
          ba._with_name("https://pbs.twimg.com/media/X.jpg?name=orig", "large")
          == "https://pbs.twimg.com/media/X.jpg?name=large")

    # 点赞格式化
    check("_likes：999 原样", ba._likes(999) == "999")
    check("_likes：1000 → 1k", ba._likes(1000) == "1k")
    check("_likes：3200 → 3.2k", ba._likes(3200) == "3.2k")
    check("_likes：12000 → 12k", ba._likes(12000) == "12k")


def _ba_queryid() -> None:
    # ★ 真实踩过的坑：松匹配会抓到 BookmarkSearchTimeline 的 id，然后 404
    js = ('x={queryId:"VxJMx0-NXe-GO_4jC1lNhw",operationName:"BookmarkSearchTimeline",'
          'operationType:"query"},y={queryId:"irezafrnzav4Ulf1vDkEBg",'
          'operationName:"SearchTimeline",operationType:"query"}')
    got = ba.extract_query_id(js)
    check("★ 从 bundle 里抠出 SearchTimeline 的 queryId", got == "irezafrnzav4Ulf1vDkEBg", str(got))
    check("★ 没有抓成紧邻的 BookmarkSearchTimeline", got != "VxJMx0-NXe-GO_4jC1lNhw")
    check("bundle 里没有那个操作时返回 None",
          ba.extract_query_id("nothing here") is None)


def _ba_parse() -> None:
    def tweet(tid="2112190149820121424", likes=100, media=True, sens=False, wrap=False):
        leg = {
            "id_str": tid, "favorite_count": likes,
            "created_at": "Mon Oct 19 14:32:28 +0000 2026",
            "full_text": "贺图 #杜野凛世誕生祭2026",
            "possibly_sensitive": sens,
        }
        if media:
            leg["extended_entities"] = {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/HVA.jpg"},
                {"type": "video", "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/X.jpg"},
            ]}
        t = {
            "rest_id": tid, "legacy": leg,
            "core": {"user_results": {"result": {"core": {
                "screen_name": "fakecolor_mono", "name": "ふぇいく"}}}},
        }
        return {"__typename": "TweetWithVisibilityResults", "tweet": t} if wrap else t

    a = ba.parse_tweet(tweet(), "杜野凛世")
    check("parse_tweet 正常解出", a is not None and a.id == "2112190149820121424")
    check("作者从 core 里取（新版结构）", a.author == "fakecolor_mono" and a.author_name == "ふぇいく")
    check("★ 图只取 photo，跳过 video 缩略图", a.image == "https://pbs.twimg.com/media/HVA.jpg?name=orig", a.image)
    check("时间解出来了且带时区", a.posted_at.year == 2026 and a.posted_at.tzinfo is not None)
    check("原推链接拼得对", a.url == "https://x.com/fakecolor_mono/status/2112190149820121424")

    check("★ TweetWithVisibilityResults 要拆一层", ba.parse_tweet(tweet(wrap=True), "X").id == "2112190149820121424")
    check("没有 media 的推文被丢掉", ba.parse_tweet(tweet(media=False), "X") is None)
    check("tombstone（空 dict）被丢掉", ba.parse_tweet({}, "X") is None)
    check("没有 created_at 被丢掉", ba.parse_tweet({"legacy": {}}, "X") is None)
    s = ba.parse_tweet(tweet(sens=True), "X")
    check("possibly_sensitive 带出来了", s.sensitive is True)

    payload = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [
        {"entries": [
            {"content": {"itemContent": {"tweet_results": {"result": tweet("1")}}}},
            {"content": {"itemContent": {"tweet_results": {"result": tweet("2")}}}},
            {"content": {"cursorType": "Bottom", "value": "CUR123"}},
            {"content": {"cursorType": "Top", "value": "CUR0"}},
        ]},
    ]}}}}}
    arts, cur = ba.parse_search(payload, "杜野凛世")
    check("parse_search 出两条", len(arts) == 2, str(len(arts)))
    check("★ 游标只取 Bottom（Top 会绕回开头，翻页死循环）", cur == "CUR123", str(cur))
    check("idol 名被带上（多偶像那天要分开保底）", arts[0].idol == "杜野凛世")


def _ba_product() -> None:
    """★ `product` 必须是 **Top**。这个 bug 不报错，只是**静默变差**。

    第一版写的是 `"Latest"`（时间栏），于是带年份的 tag 只捞得到深夜新发的
    小号，当天最高才 464 赞，我据此把用户「2k 以上」的判断当成了错的。
    实测同一 tag 同一时刻：Top 有 6~8 条 ≥2000 赞，Latest **一条都没有**。
    """
    import inspect as _inspect  # noqa: PLC0415

    sig = _inspect.signature(ba.XSearch.search_page)
    check("★ search_page 的 product 默认是 **Top**（写成 Latest 不报错，只是全变低赞）",
          sig.parameters["product"].default == "Top", str(sig.parameters["product"].default))
    check("★ PAGE_PLAN_YEARFUL 第一页是 Top", ba.PAGE_PLAN_YEARFUL[0] == "Top",
          str(ba.PAGE_PLAN_YEARFUL))
    check("PAGE_PLAN_BARE 只用 Latest（不带年份的 Top 全是往年，实测窗口内 0 条）",
          ba.PAGE_PLAN_BARE == ("Latest",), str(ba.PAGE_PLAN_BARE))

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self.content = b"{}"
            self._p = payload

        def json(self):
            return self._p

    class _Rec:
        """记下每次 POST 的 (product, cursor)，并回一个**按栏递增**的游标。"""

        def __init__(self):
            self.calls: list[tuple[str, str | None]] = []
            self.n: dict[str, int] = {}

        async def post(self, url, params=None, headers=None, timeout=None):
            import json as _json  # noqa: PLC0415

            v = _json.loads(params["variables"])
            prod, cur = v["product"], v.get("cursor")
            self.calls.append((prod, cur))
            self.n[prod] = self.n.get(prod, 0) + 1
            nxt = f"{prod}-cur-{self.n[prod]}"
            payload = {"data": {"search_by_raw_query": {"search_timeline": {
                "timeline": {"instructions": [{"entries": [
                    {"content": {"itemContent": {"tweet_results": {"result": {
                        "rest_id": "1", "legacy": {
                            "favorite_count": 100,
                            "created_at": "Mon Oct 19 14:32:28 +0000 2026",
                            "extended_entities": {"media": [
                                {"type": "photo",
                                 "media_url_https": "https://pbs.twimg.com/media/HVA.jpg"}]},
                        },
                        "core": {"user_results": {"result": {"core": {
                            "screen_name": "a", "name": "A"}}}},
                    }}}}},
                    {"content": {"cursorType": "Bottom", "value": nxt}},
                ]}]}}}}}
            return _Resp(payload)

    rec = _Rec()
    x = ba.XSearch(rec, "a" * 40, "c" * 160)
    x._query_id = "QID"  # 跳过 /home + bundle 那条发现链路
    old_gap = ba.SEARCH_GAP_SEC
    ba.SEARCH_GAP_SEC = 0
    try:
        asyncio.run(x.search_tag("#杜野凛世誕生祭2026", "杜野凛世",
                                 __import__("datetime").date(2026, 10, 19)))
    finally:
        ba.SEARCH_GAP_SEC = old_gap

    check("★ 带年份的 tag 按 Top → Top → Latest 各发一次",
          [c[0] for c in rec.calls] == ["Top", "Top", "Latest"], str(rec.calls))
    check("★ **游标按栏分开存**（Top 的游标喂给 Latest 是未定义行为）",
          rec.calls == [("Top", None), ("Top", "Top-cur-1"), ("Latest", None)],
          str(rec.calls))


def _ba_official() -> None:
    """★ 官方号不占名额。用户 2026-10-20 点名要求排掉。"""
    # 真实那一行（实测 2026-10-19 排第一，8596 赞）
    off = _ba_art(1, likes=8596, author="imassc_official",
                  name="アイドルマスター シャイニーカラーズ公式")
    check("★ 官方号认出来了（handle 表 + 名字里的「公式」）", ba.is_official(off))
    # 只靠名字也要认得出 —— 官方号会新增，handle 表追不上
    check("★ 换个没见过的 handle，名字带「公式」照样认出来",
          ba.is_official(_ba_art(2, likes=1, author="brand_new_official",
                                 name="アイドルマスター ミリオンライブ！公式")))
    check("只靠 handle 表也认得出（名字里没「公式」的那种官方号）",
          ba.is_official(_ba_art(3, likes=1, author="imas_ch", name="アイマスch")))
    check("普通画师不误伤", not ba.is_official(_ba_art(4, likes=1, author="KiouRi_39", name="KiouRi")))
    check("名字里带「公式」的也认（大小写 / 前后缀无关）",
          ba.is_official(_ba_art(5, likes=1, author="x", name="なんとか公式")))
    check("★ handle 比较不分大小写", ba.is_official(_ba_art(6, likes=1, author="IMASSC_Official")))
    check("★ BIRTHDAY_ART_EXCLUDE 能补，且容忍 @ 前缀 / 大小写",
          ba.is_official(_ba_art(7, likes=1, author="SomeBrand", name="ブランド"),
                         exclude=("@somebrand",))
          and not ba.is_official(_ba_art(8, likes=1, author="SomeBrand", name="ブランド")))

    # ★ 行为钉子：真实那天的前几名喂进去，第一名应该是画师而不是官方
    arts = [
        off,
        _ba_art(10, likes=7878, author="BossMonkey0112", name="ボスモンキー"),
        _ba_art(11, likes=6195, author="eluthel1", name="アイルル"),
    ]
    got = ba.rank_art(arts, ["杜野凛世"], min_likes=50)
    check("★ 排掉官方号之后，第一名是画师 @BossMonkey0112（不是那个 8596 赞的官推）",
          [a.author for a in got] == ["BossMonkey0112", "eluthel1"],
          str([a.author for a in got]))
    got2 = ba.rank_art(arts, ["杜野凛世"], min_likes=50, skip_official=False)
    check("★ skip_official=False 就放回来（这个开关真起作用，不是摆设）",
          got2[0].author == "imassc_official", str([a.author for a in got2]))


def _ba_rank() -> None:
    from qq_rss_bot.birthday_art import rank_art  # noqa: PLC0415

    arts = [_ba_art(1, likes=10), _ba_art(2, likes=500), _ba_art(3, likes=300)]
    got = rank_art(arts, ["杜野凛世"], min_likes=50)
    check("★ 按点赞降序", [a.likes for a in got] == [500, 300], str([a.likes for a in got]))
    check("低于下限的被排掉", all(a.likes >= 50 for a in got))

    # 同分按 id → 结果确定（可复现、能写测试）
    tie = [_ba_art(9, likes=100), _ba_art(4, likes=100), _ba_art(7, likes=100)]
    got = rank_art(tie, ["杜野凛世"], min_likes=0)
    check("★ 同分按 id 定序（不然每次顺序都可能不一样）",
          [a.id for a in got] == sorted(a.id for a in tie), str([a.id for a in got]))

    # 每位画师最多 1 张
    same = [_ba_art(1, likes=900, author="prolific"), _ba_art(2, likes=800, author="prolific"),
            _ba_art(3, likes=100, author="other")]
    got = rank_art(same, ["杜野凛世"], min_likes=0)
    check("★ 同一位画师只留一张（不然一个高产画师吃掉全部名额）",
          [a.author for a in got] == ["prolific", "other"], str([a.author for a in got]))

    # 每位偶像保底一张
    mixed = [_ba_art(1, likes=900, idol="A"), _ba_art(2, likes=800, idol="A"),
             _ba_art(3, likes=700, idol="A"), _ba_art(4, likes=5, idol="B")]
    got = rank_art(mixed, ["A", "B"], min_likes=0)
    check("★ 冷门偶像也保底一张（否则最火的那位把名额吃光）",
          any(a.idol == "B" for a in got), str([(a.idol, a.likes) for a in got]))
    check("保底那张排在最前面", got[0].idol == "B", str(got[0].idol))

    check("敏感的排掉", not rank_art([_ba_art(1, likes=900, sensitive=True)], ["X"], min_likes=0))
    check("没图的排掉", not rank_art([_ba_art(1, likes=900, image=False)], ["X"], min_likes=0))
    dup = [_ba_art(1, likes=900), _ba_art(1, likes=900)]
    check("同一条推文只留一次", len(rank_art(dup, ["X"], min_likes=0)) == 1)

    # 排序本身**不截断** —— 截多少由「下载成功了几张」决定
    many = [_ba_art(i, likes=i * 10) for i in range(1, 20)]
    check("rank_art 不截断（截断交给下载那一步）",
          len(rank_art(many, ["杜野凛世"], min_likes=0)) == 19)


def _ba_render() -> None:
    a = _ba_art(1, likes=3200, author="fakecolor_mono")
    got = ba.render(a, 2, 6, 2026)
    check("★ 文案逐字（三行：标题 / 点赞+署名 / 原推链接）",
          got == ("🎨 杜野凛世 誕生祭 2026（2/6）\n"
                  "♥ 3.2k · 画师 @fakecolor_mono\n"
                  f"{a.url}"), repr(got))
    check("文案里带原推链接（署名是礼节也是实用）", "/status/" in got)
    check("_id_of 能从文案里取回推文 id", ba._id_of(got) == a.id, ba._id_of(got))
    check("_id_of 取不到时给空串", ba._id_of("没有链接") == "")


async def _ba_poller() -> None:
    import tempfile as _tf  # noqa: PLC0415

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        _bd_cal(tmp)  # 复用 4.96 造的那份小冷数据（10-19 杜野凛世 / 10-20 空）

        # ---- enabled 要三样都齐 ----
        cfg = _ba_cfg(tmp)
        p = ba.BirthdayArtPoller(cfg, _ba_db(), _BaSender(), _BaHttp())
        check("三样齐了才 enabled", p.enabled)
        for over, why in [({"birthday_art_enabled": False}, "开关关了"),
                          ({"birthday_art_groups": ()}, "没有目标群"),
                          ({"x_auth_token": ""}, "缺 auth_token"),
                          ({"x_ct0": ""}, "缺 ct0")]:
            q = ba.BirthdayArtPoller(
                _ba_cfg(tmp, **over), _ba_db(), _BaSender(), _BaHttp())
            check(f"★ {why} → 功能关闭", not q.enabled)
            n = await q.poll_once(now=datetime(2026, 10, 19, 22, 30, 30), arts=[_ba_art(1, likes=900)])
            check(f"  {why}时不发也不炸", n == 0)

        # ---- 正常一轮 ----
        db, sender = _ba_db(), _BaSender()
        p = ba.BirthdayArtPoller(_ba_cfg(tmp), db, sender, _BaHttp())
        now = datetime(2026, 10, 19, 22, 30, 30)
        arts = [_ba_art(i, likes=1000 - i * 10) for i in range(1, 4)]
        n = await p.poll_once(now=now, arts=arts)
        check("★ 一轮发出去的条数 = 挑中的张数", n == 3, str(n))
        check("★ 一条一张（消息条数 == 图片张数）", len(sender.sent) == 3, str(len(sender.sent)))
        check("文案编号是 (i/N)", "（1/3）" in sender.sent[0][1] and "（3/3）" in sender.sent[2][1])
        check("图片字节真的传进去了", sender.sent[0][2].startswith(b"\xff\xd8"))
        check("落库了，记了条数和推文 id",
              db.rows and db.rows[0][2] == 3 and len(db.rows[0][3].split(",")) == 3, str(db.rows))

        # ---- 幂等 ----
        n = await p.poll_once(now=now, arts=arts)
        check("★ 同一个 slot 再跑一次，一条都不发", n == 0 and len(sender.sent) == 3)

        # ---- 清账之后能重发（探针 --reset 走的就是这条路）----
        # 实测踩过：改完挑选规则想拿同一天重测，忘了清账 → 永远「发出去 0 条」，
        # 看起来像坏了，其实是幂等在挡。
        check("★ clear_birthday_art 说「原来有一行」", db.clear_birthday_art("2026-10-19", "G1") is True)
        check("清完就不在「已发」里了", db.birthday_art_posted("2026-10-19") == set())
        n = await p.poll_once(now=now, arts=arts)
        check("★ 清账之后同一天能重发（否则重测永远是 0 条）", n == 3, str(n))
        check("重发之后又记上了", db.birthday_art_posted("2026-10-19") == {"G1"})
        check("★ 清一行不存在的，返回 False（不会谎报）",
              db.clear_birthday_art("2026-10-19", "没这个群") is False)

        # ---- 窗口外不发 ----
        q = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), _BaHttp())
        n = await q.poll_once(now=datetime(2026, 10, 19, 23, 30), arts=arts)
        check("窗口外（超 300s）不发", n == 0)

        # ---- 没人过生日的那天 ----
        q = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), _BaHttp())
        n = await q.poll_once(now=datetime(2026, 10, 20, 22, 30, 30), arts=arts)
        check("★ 那天没有偶像过生日 → 不发、不记账、不打日志刷屏", n == 0)

        # ---- 候选为空 / 全不过下限 ----
        q, s2 = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), _BaHttp()), _BaSender()
        q = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), s2, _BaHttp())
        check("没有候选 → 不发", await q.poll_once(now=now, arts=[]) == 0)
        check("★ 全不过下限 → 不发（**不退回纯文字**）",
              await q.poll_once(now=now, arts=[_ba_art(1, likes=1, author="a1")]) == 0)
        check("  ……而且一条消息都没发", not s2.sent)

        # ---- 首图挂 → 整批放弃、不记账（下一拍重试）----
        db3 = _ba_db()
        p3 = ba.BirthdayArtPoller(_ba_cfg(tmp), db3, _BaSender(fail_first={"G1"}), _BaHttp())
        n = await p3.poll_once(now=now, arts=arts)
        check("★ 首图发不出去 → 不记账（窗口内下一拍会重试）", n == 0 and not db3.rows, str(db3.rows))

        # ---- 中间某张挂 → 照记账，sent 反映真实条数 ----
        db4 = _ba_db()
        p4 = ba.BirthdayArtPoller(_ba_cfg(tmp), db4, _BaSender(fail_at={("G1", 1)}), _BaHttp())
        n = await p4.poll_once(now=now, arts=arts)
        check("★ 中间某张挂 → 仍记账，且记的是**真的发出去了几张**",
              n == 2 and db4.rows[0][2] == 2, str(db4.rows))
        check("  ……记的 id 也是真发出去的那几条（不是前 N 个）",
              ",".join(a.id for a in arts[:1] + arts[2:3]) == db4.rows[0][3], db4.rows[0][3])

        # ---- 逐群隔离 ----
        db5 = _ba_db()
        cfg5 = _ba_cfg(tmp, birthday_art_groups=("G1", "G2"))
        p5 = ba.BirthdayArtPoller(cfg5, db5, _BaSender(fail_first={"G1"}), _BaHttp())
        n = await p5.poll_once(now=now, arts=arts)
        check("★ 一个群发失败不连累别的群", n == 3 and db5.posted.get("2026-10-19") == {"G2"},
              str(db5.posted))

        # ---- 一张图都下不下来 → 这拍不发、不记账 ----
        db6 = _ba_db()
        p6 = ba.BirthdayArtPoller(_ba_cfg(tmp), db6, _BaSender(),
                                  _BaHttp(dead={"name=orig", "name=large"}))
        n = await p6.poll_once(now=now, arts=arts)
        check("★ 图一张都下不来 → 不记账，留窗口内重试", n == 0 and not db6.rows)


async def _ba_fetch_fallback() -> None:
    import tempfile as _tf  # noqa: PLC0415

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        _bd_cal(tmp)
        art = _ba_art(1, likes=900)

        # 原图下得动 → 用原图
        http = _BaHttp()
        p = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), http)
        got = await p._download([art], 1)
        check("原图能下就直接用", len(got) == 1 and any("name=orig" in u for u in http.urls))

        # ★ 原图下不动（多半是超过 3.5MB 的直传上限）→ 退到 large
        http = _BaHttp(dead={"name=orig"})
        p = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), http)
        got = await p._download([art], 1)
        check("★ 原图下不动时退到 large（实测 5 张里有 1 张原图超限）",
              len(got) == 1 and any("name=large" in u for u in http.urls), str(http.urls))

        # 两档都下不动 → 跳过，往后补位
        arts = [_ba_art(i, likes=900 - i) for i in range(1, 5)]
        http = _BaHttp(dead={"IMG1"})
        p = ba.BirthdayArtPoller(_ba_cfg(tmp), _ba_db(), _BaSender(), http)
        got = await p._download(arts, 2)
        check("★ 下不动的往后补位，凑满想要的张数",
              len(got) == 2 and all(a.id != arts[0].id for a, _ in got),
              str([a.id for a, _ in got]))


def _ba_real_table() -> None:
    from qq_rss_bot.db import Database  # noqa: PLC0415
    import tempfile as _tf  # noqa: PLC0415

    with _tf.TemporaryDirectory() as td:
        db = Database(Path(td) / "t.db")
        check("birthday_art_posted 空库返回空集", db.birthday_art_posted("2026-10-19") == set())
        db.mark_birthday_art("2026-10-19", "G1", 6, "1,2,3,4,5,6")
        check("★ 落库后查得到", db.birthday_art_posted("2026-10-19") == {"G1"})
        db.mark_birthday_art("2026-10-19", "G1", 3, "1,2,3")
        check("重复标记不炸（PRIMARY KEY 是 day+群）",
              db.birthday_art_posted("2026-10-19") == {"G1"})
        row = db._conn.execute(
            "SELECT sent, tweets FROM birthday_art_posts WHERE day = ?", ("2026-10-19",)
        ).fetchone()
        check("★ 重复标记是**覆盖**（sent 反映最后一次的真实条数）",
              row["sent"] == 3 and row["tweets"] == "1,2,3", str(dict(row)))
        # ★ 真库上的 clear（探针 --reset 用的就是它）
        check("★ 真库 clear 说「原来有」", db.clear_birthday_art("2026-10-19", "G1") is True)
        check("★ 真库 clear 之后查不到了", db.birthday_art_posted("2026-10-19") == set())
        check("★ 再 clear 一次返回 False（幂等，不炸）",
              db.clear_birthday_art("2026-10-19", "G1") is False)
        check("★ clear 只删指定群，别的群不受影响",
              (db.mark_birthday_art("2026-10-19", "G1", 1, "1"),
               db.mark_birthday_art("2026-10-19", "G2", 1, "2"),
               db.clear_birthday_art("2026-10-19", "G1"),
               db.birthday_art_posted("2026-10-19"))[3] == {"G2"})
        db.close()
        # 真实冷数据的分布（只在文件在的时候跑）
        path = Path(__file__).resolve().parents[1] / "data" / "birthday" / "birthdays.toml"
        if path.exists():
            cal = birthday.load_calendar(path)
            day = cal.for_date(date(2026, 10, 19))
            check("★ 真实冷数据里 10-19 是杜野凛世",
                  [i.name for i in day.idols] == ["杜野凛世"], str([i.name for i in day.idols]))
            n_idol = sum(1 for d in cal.days.values() if d.idols)
            check("真实冷数据 366 天里有偶像的天数 = 245", n_idol == 245, str(n_idol))


def _ba_wiring() -> None:
    import inspect  # noqa: PLC0415

    from qq_rss_bot.bot import RssBot  # noqa: PLC0415

    init = inspect.getsource(RssBot.__init__)
    # 构造是跨行的，得取匹配行**往后的几行**，只看那一行会漏掉参数
    lines = init.splitlines()
    i = next((n for n, ln in enumerate(lines) if "BirthdayArtPoller(" in ln), -1)
    call = " ".join(ln.strip() for ln in lines[i : i + 4]) if i >= 0 else ""
    check("★ 贺图轮询器接的是 **media_http**（带代理那个），不是 rss_http",
          "self.media_http" in call and "rss_http" not in call, call)
    check("★ 传了 notifier（cookie 失效要靠它私聊告警）", "notifier=self.notifier" in call, call)
    check("挂的是 self.birthday_art / self._birthday_art_task",
          "self.birthday_art = BirthdayArtPoller" in init and "self._birthday_art_task" in init)
    check("on_ready 里起了这个 task",
          "self.birthday_art.run()" in inspect.getsource(RssBot.on_ready))
    check("close() 里停了它", "self.birthday_art.stop()" in inspect.getsource(RssBot.close))

    # ★ cookie 绝不进日志
    src = Path(ba.__file__).read_text()
    log_src = "\n".join(ln for ln in src.splitlines() if "_log." in ln)
    check("★ 日志里只出现长度，不出现 cookie 内容",
          "x_auth_token" not in log_src and "x_ct0" not in log_src and "_auth" not in log_src)
    # ★ 请求计数器必须每轮清零 —— 否则跑几天就撞上限、永远不再搜
    p = ba.BirthdayArtPoller(_ba_cfg(Path("/tmp")), _ba_db(), _BaSender(), _BaHttp())
    p.x._requests = ba.MAX_REQUESTS - 1
    p.x.cookie_dead = True
    p.x.reset()
    check("★ XSearch.reset() 把请求计数和 cookie_dead 都清零（每轮一次，不是进程一次）",
          p.x.requests_made == 0 and p.x.cookie_dead is False)
    import inspect as _insp  # noqa: PLC0415
    check("★ _collect 每轮开头调了 reset()",
          "self.x.reset()" in _insp.getsource(ba.BirthdayArtPoller._collect))

    check("★ 两个凭据字段都没被写进任何一条日志",
          "auth_token=%s" not in src and "ct0=%s" not in src)

    c = cfg_stub()
    check("cfg_stub 带上贺图字段了（默认值和模块常量一致）",
          c.birthday_art_time == ba.DEFAULT_TIME
          and c.birthday_art_max == ba.DEFAULT_MAX
          and c.birthday_art_min_likes == ba.DEFAULT_MIN_LIKES
          and c.birthday_art_groups == ()
          and c.x_auth_token == "")

    # ★ --send 必须**在 httpx 客户端的 with 里**。第一版写在 with 外面，
    #   而 poll_once 会再搜一次、再下一次图 —— 客户端已经关了，
    #   必崩 `RuntimeError: Cannot send a request, as the client has been closed`。
    #   这个 bug 只有真按 --send 才会现形（离线 smoke 和 --search 都照不出来），
    #   所以在这儿用源码缩进把它钉死。
    probe = (Path(__file__).resolve().parents[1] / "scripts" / "probe_birthday_art.py").read_text()
    plines = probe.splitlines()
    iw = next((n for n, ln in enumerate(plines) if "async with preview.make_client" in ln), -1)
    ip = next((n for n, ln in enumerate(plines) if "await poller.poll_once(" in ln), -1)
    # ⚠️ **只比缩进是不够的** —— 错版里 `if do_send:` 在 4、它的 body 在 8，
    #    while with 的 body 也在 8，缩进看着「在里面」。要比的是**块的结束位置**：
    #    with 之后第一条缩进回到 with 那一档（或更浅）的非空行，就是块外了。
    base = len(plines[iw]) - len(plines[iw].lstrip()) if iw >= 0 else -1
    iend = next(
        (
            n
            for n in range(iw + 1, len(plines))
            if plines[n].strip() and len(plines[n]) - len(plines[n].lstrip()) <= base
        ),
        len(plines),
    )
    inside = iw >= 0 and iw < ip < iend
    check("★ --send 的 poll_once 在 make_client 的 with **里面**（写在外面必崩）", inside,
          f"with@{iw}..{iend} poll_once@{ip}")
    check("★ 收尾的 sender.close() 在 with 外面（关客户端不能漏掉关 sender）",
          "await sender.close()" in probe and ip < next(
              (n for n, ln in enumerate(plines) if "await sender.close()" in ln), -1))


from qq_rss_bot import birthday  # noqa: E402
from qq_rss_bot import birthday_art as ba  # noqa: E402

_ba_parts()
_ba_queryid()
_ba_parse()
_ba_product()
_ba_official()
_ba_rank()
_ba_render()
asyncio.run(_ba_poller())
asyncio.run(_ba_fetch_fallback())
_ba_real_table()
_ba_wiring()

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
