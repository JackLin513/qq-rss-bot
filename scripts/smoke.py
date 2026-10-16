"""冒烟测试：不连 QQ，只验证解析/去重/推送格式化/真实抓取。

跑法：uv run python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 造假环境变量，避免 load_config 因缺凭证退出
os.environ.setdefault("QQ_BOT_APPID", "1")
os.environ.setdefault("QQ_BOT_SECRET", "x")

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database, utcnow  # noqa: E402
from qq_rss_bot.commands import HELP_TEXT, is_bot_mentioned, strip_mention  # noqa: E402
from qq_rss_bot.compat import install_new_protocol_support  # noqa: E402
from qq_rss_bot.rss import Entry, fetch_feed, resolve_url  # noqa: E402
from qq_rss_bot.scheduler import format_entry  # noqa: E402

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
    RESTRICTED_TOKENS,
    CommandContext,
    CommandHandler,
)
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


async def _check_permissions() -> None:
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
        check("群主发 /unsub 能执行（走到底：没这个订阅）",
              any("没找到" in r for r in replies), str(replies))

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
_remote = [
    {"name": "sub", "desc": "订阅 RSS 源，可加间隔分钟", "type": "command", "only_admin": True},
    {"name": "unsub", "desc": "取消本群某个订阅", "type": "command", "only_admin": True},
    {"name": "list", "desc": "查看本群所有订阅", "type": "command"},
    {"name": "check", "desc": "立刻抓一次验证源是否可用", "type": "command"},
    {"name": "ping", "desc": "检查机器人在线状态", "type": "command"},
    {"name": "help", "desc": "查看完整用法说明", "type": "command"},
]
check("真实回读形态 == 完全一致", setup_panel.compare_items(_remote) == [],
      str(setup_panel.compare_items(_remote)))

# 带斜杠的历史数据也该被认成一致（不能报「缺少 /sub / 多出 sub」）
_slashy = [dict(i, name="/" + i["name"]) for i in _remote]
check("带斜杠的旧数据不算差异", setup_panel.compare_items(_slashy) == [],
      str(setup_panel.compare_items(_slashy)))

# 真差异必须仍然抓得到，归一化不能把问题一起抹掉
check("真缺元素仍被抓", "缺少元素 ping" in
      " ".join(setup_panel.compare_items([i for i in _remote if i["name"] != "ping"])))
check("描述改错仍被抓", any("desc" in d for d in setup_panel.compare_items(
    [dict(i, desc="改过了") if i["name"] == "list" else i for i in _remote])))
check("顺序变了仍被抓", any("顺序" in d for d in setup_panel.compare_items(
    list(reversed(_remote)))))
check("重名被抓", setup_panel.validate(
    [{"name": "sub", "desc": "a", "type": "command"},
     {"name": "/sub", "desc": "b", "type": "command"}]) != [])

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
