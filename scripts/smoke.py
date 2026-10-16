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
from qq_rss_bot.commands import is_bot_mentioned, strip_mention  # noqa: E402
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
check("重复安装安全", install_new_protocol_support() is False)

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
