"""端到端集成测试：真实抓 RSSHub，灌进真实 SQLite，验证推送时机与内容。

推送出口换成假的记录器，所以不碰 QQ、不需要凭证。

跑法：
    uv run python scripts/integration.py
    FEED=http://127.0.0.1:1200/idolmaster/news uv run python scripts/integration.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
logging.disable(logging.CRITICAL)

# 默认用自建 RSSHub 的路由；可用 FEED 环境变量覆盖
FEED = os.environ.get("FEED", "http://127.0.0.1:1200/v2ex/topics/hot")
MAX_ITEMS = 3

ok = fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {detail}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        os.environ.update(
            QQ_BOT_APPID="1",
            QQ_BOT_SECRET="x",
            DB_PATH=str(Path(td) / "i.db"),
            DEFAULT_INTERVAL_SEC="60",
            MAX_ITEMS_PER_PUSH=str(MAX_ITEMS),
            ATTACH_IMAGES="true",
        )
        from qq_rss_bot.bot import RssBot
        from qq_rss_bot.config import load_config

        bot = RssBot(load_config())
        pushes: list[tuple[str, str]] = []  # (群, 正文)
        rich: list[str] = []  # 走图文的图片 URL

        class FakePusher:
            async def send_text(self, group_openid: str, content: str) -> None:
                pushes.append((group_openid, content))

            async def send_rich(
                self, group_openid: str, content: str, image_url: str
            ) -> None:
                pushes.append((group_openid, content))
                rich.append(image_url)

        bot.poller.pusher = FakePusher()
        group = "GROUP_OPENID_TEST"

        def unblock() -> None:
            """让订阅立刻到期，不用等真实间隔。"""
            bot.db._conn.execute("UPDATE subscriptions SET last_polled_at = NULL")
            bot.db._conn.commit()

        sub_id, _ = bot.db.add_subscription(group, FEED, None, 60)
        print(f"[1] 首抓（真实网络）：{FEED}")
        await bot.poller.poll_once()

        row0 = bot.db._conn.execute(
            "SELECT title FROM subscriptions WHERE id = ?", (sub_id,)
        ).fetchone()
        feed_title = row0["title"]
        check("首抓建好 baseline", bot.db.due_subscriptions("2099-01-01T00:00:00+00:00")[0].initialized)
        check("首抓不推送（避免倒灌旧文）", pushes == [], f"实际 {len(pushes)} 条")
        check("抓到了源标题", bool(feed_title), repr(feed_title))

        print("[2] 再抓一次（无新条目）")
        unblock()
        await bot.poller.poll_once()
        check("无新条目不推送", pushes == [], f"实际 {len(pushes)} 条")

        print("[3] 模拟出现 5 条新条目")
        bot.db._conn.execute(
            "DELETE FROM seen_entries WHERE subscription_id = ? AND entry_key IN"
            " (SELECT entry_key FROM seen_entries WHERE subscription_id = ? LIMIT 5)",
            (sub_id, sub_id),
        )
        unblock()
        await bot.poller.poll_once()

        # 逐条推送：最多 MAX_ITEMS 条新闻 + 至多 1 条「另有 N 条」提示。
        # 后者不是新闻，凡是针对新闻的断言都必须先把它排除掉。
        news = [c for _, c in pushes if "另有" not in c]
        note = [c for _, c in pushes if "另有" in c]

        check(f"新闻条数受 MAX_ITEMS 限制（≤{MAX_ITEMS}）", len(news) <= MAX_ITEMS,
              f"实际 {len(news)} 条")
        check("提示至多一条", len(note) <= 1, str(note))
        check("每条的群都正确", all(g == group for g, _ in pushes),
              str(set(g for g, _ in pushes)))
        check("每条新闻都带源名", all(feed_title[:6] in c for c in news),
              str([c[:30] for c in news]))
        check("每条新闻都是 源名+标题+链接",
              all("📰" in c and "http" in c for c in news))
        check("超出的条目有提示（或无超出）",
              bool(note) or len(news) < MAX_ITEMS, f"新闻 {len(news)} 条 / 提示 {len(note)} 条")

        # 各源带图比例不同（idolmaster 全带、v2ex 少数、36kr 没有），
        # 所以这里只断言「不超过」；精确的图文路由逻辑在 smoke.py 里用合成数据测
        check("配图数不超过新闻条数", len(rich) <= len(news),
              f"图文 {len(rich)} / 新闻 {len(news)}")
        check("配图用源里的图片 URL", all(u.startswith("http") for u in rich), str(rich[:1]))
        print(f"        图文 {len(rich)} 条 / 纯文字 {len(news) - len(rich)} 条")
        print("        ---- 推送内容 ----")
        for i, (_, content) in enumerate(pushes, 1):
            tag = " [配图]" if i <= len(rich) else ""
            print(f"        {i}.{tag} " + content.replace("\n", "\n           "))

        before = len(pushes)
        print("[4] 去重持久化")
        unblock()
        await bot.poller.poll_once()
        check("同一批不会重复推送", len(pushes) == before, f"{before} → {len(pushes)}")

        print("[5] 坏 URL 的错误处理")
        bad_id, _ = bot.db.add_subscription(
            group, "http://127.0.0.1:1200/github/trending/daily/python", None, 60
        )
        await bot.poller.poll_once()
        row = bot.db._conn.execute(
            "SELECT last_error FROM subscriptions WHERE id = ?", (bad_id,)
        ).fetchone()
        check("503(缺配置) 被记录为错误", row["last_error"] is not None, str(row["last_error"]))
        check("抓取失败不产生推送", len(pushes) == before, f"{before} → {len(pushes)}")
        print(f"        记录的 last_error = {row['last_error']}")

        await bot.close()
        print(f"\n结果：{ok} passed, {fail} failed")
        sys.exit(1 if fail else 0)


asyncio.run(main())
