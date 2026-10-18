"""偶像大师 NEWS 的「企划 / 分类」标签探针。

**默认只打印，不发 QQ 消息、不碰数据库。** 用来回答两件事：

1. 标签翻译对不对（`【灰姑娘】【演出/谷子】` 这行到底长什么样）；
2. 某条为什么**没标签** —— 是 CMS 里没有这篇文章，还是翻译表缺 code。

跑法：

    uv run python scripts/probe_imas.py                    # 抓真 feed 打印
    uv run python scripts/probe_imas.py --raw              # 连 CMS 原始字段一起看
    uv run python scripts/probe_imas.py --rsshub http://127.0.0.1:1200

`--rsshub` 默认打 NAS（`http://100.91.151.20:1200`），在容器里跑时不用传。
**本脚本没有任何发送路径**，不提供 `--send`。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import idolmaster  # noqa: E402
from qq_rss_bot.rss import fetch_feed, resolve_url  # noqa: E402
from qq_rss_bot.scheduler import format_entry  # noqa: E402

DEFAULT_RSSHUB = "http://100.91.151.20:1200"
ROUTE = "/idolmaster/news"


def _arg(name: str, default: str) -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


async def main() -> int:
    base = _arg("--rsshub", DEFAULT_RSSHUB)
    show_raw = "--raw" in sys.argv
    url = resolve_url(ROUTE, base)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        result = await fetch_feed(http, url, 30)
        if result.error:
            print(f"抓取失败：{result.error}\n{url}")
            return 1

        meta = await idolmaster.fetch_meta(http)
        tags = await idolmaster.tags_for(http, url, result.entries)

        print(f"feed    : {result.title}  ({len(result.entries)} 条)")
        print(f"CMS     : {len(meta)} 篇")
        print(f"拿到标签: {len(tags)}/{len(result.entries)}")
        if not meta:
            print("\n!! CMS 一篇都没拿到 —— 网络或接口变了，此时推送会静默不带标签")
        print()

        for entry in result.entries:
            line = tags.get(entry.key, "")
            if not line:
                print(f"[无标签] {entry.link}")
                if show_raw:
                    print(f"         CMS 里{(entry.link in meta) and '有' or '没有'}这篇")
                print()
                continue
            print(format_entry(result.title, url, entry, tags=line))
            if show_raw:
                brands, types = meta.get(entry.link, ((), ()))
                print(f"    raw: brand={brands} subcategory={types}")
            print("-" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
