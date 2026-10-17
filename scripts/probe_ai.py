"""AI 摘要的端到端探针：抓正文 → 调模型 → 打印结果。

**不碰数据库、不发 QQ 消息。** 用来回答「为什么这个源开了 AI 却没摘要」——
把那条源里的文章链接直接喂进来，一眼就能看出是抓正文失败还是模型那边的问题。

跑法：

    uv run python scripts/probe_ai.py <文章URL>
    uv run python scripts/probe_ai.py <文章URL> --call      # 连模型一起测
    uv run python scripts/probe_ai.py <URL1> <URL2> ...

不加 `--call` 就**一个 API 调用都不发**，只看正文能抓多少字 ——
绝大多数失败都卡在这一步（付费墙 / JS 渲染 / 反爬）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_call = "--call" in sys.argv
    if not args:
        print(__doc__)
        return 1

    from qq_rss_bot import ai
    from qq_rss_bot.config import load_config

    cfg = load_config()
    print(f"正文提取器：{ai.extractor_name()}")
    print(f"模型：{cfg.ai_model} @ {cfg.ai_base_url}")
    print(
        "正文来源："
        + ("抓文章页（AI_FETCH_ARTICLE=true）" if cfg.ai_fetch_article else "RSS 导语")
    )
    print(f"API key：{'已配' if cfg.ai_api_key else '**没配**（--call 会直接跳过）'}")
    print()

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as direct:
        for url in args:
            print(f"=== {url}")
            text = await ai.fetch_article(direct, url, timeout=cfg.ai_fetch_timeout_sec)
            if not text:
                print("  ❌ 正文抓不到（付费墙 / JS 渲染 / 反爬？）—— 会退回 RSS 导语")
                continue
            print(f"  ✅ 正文 {len(text)} 字，前 120 字：{text[:120]!r}")

            if not do_call or not cfg.ai_api_key:
                continue
            got = await ai.digest(
                direct,
                api_key=cfg.ai_api_key,
                title="（探针，没有原标题）",
                text=text,
                base_url=cfg.ai_base_url,
                model=cfg.ai_model,
                timeout=cfg.ai_timeout_sec,
                disable_thinking=cfg.ai_disable_thinking,
            )
            if got is None:
                print("  ❌ 模型没返回可用结果（看上面的 WARNING）")
            else:
                print(f"  ✅ 标题：{got.title}")
                print(f"     摘要：{got.summary}")
                print(f"     translated={got.translated}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
