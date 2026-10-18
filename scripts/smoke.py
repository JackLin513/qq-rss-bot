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
