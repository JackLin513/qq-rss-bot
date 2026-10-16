"""测几个真实 RSS 源，确认解析链路本身没问题（排查 RSSHub 403 用）。"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx
from qq_rss_bot.rss import fetch_feed

SOURCES = [
    ("HN 官方 RSS", "https://hnrss.org/frontpage"),
    ("阮一峰 Atom", "https://www.ruanyifeng.com/blog/atom.xml"),
    ("RSSHub 公共实例", "https://rsshub.app/github/trending/daily/python"),
    ("RSSHub 镜像 rsshub.rssforever.com", "https://rsshub.rssforever.com/github/trending/daily/python"),
]

async def main():
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 qq-rss-bot/0.1"}, follow_redirects=True) as c:
        for name, url in SOURCES:
            r = await fetch_feed(c, url, 25)
            if r.ok:
                print(f"OK    {name}: {len(r.entries)} 条 | {r.title} | 首条={r.entries[0].title[:50]}")
            else:
                print(f"FAIL  {name}: {r.error}")

asyncio.run(main())
