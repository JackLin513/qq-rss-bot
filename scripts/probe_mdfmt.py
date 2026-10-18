"""markdown 排版探针：**图片多宽刚好占满气泡** + **哪些 markdown 语法真的生效**。

2026-10-18 加。起因是用户反馈两条：

1. 「图片没有占满整个文本框宽度」—— 列表里的封面定的是 208px（从 bilibili 封面
   抄来的），QQ **不会**把小于气泡宽度的图放大，所以看着就是没占满。
   到底多少才占满取决于设备，代码里猜不出来，只能真机上看。
2. 「哪些该变大哪些该变粗」—— `**加粗**` 是实测过的（bilibili 卡片一直在用），
   但 `#` 标题 / `>` 引用 / `-` 列表这些**从没验过**，不能凭文档就写进生产文案。

所以这个脚本一次把两件事都问了：发两条消息，你扫一眼就知道该填什么。

    uv run python scripts/probe_mdfmt.py                      # 只打印，不发
    uv run python scripts/probe_mdfmt.py --group <openid> --send

⚠️ **只允许发到机器人测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
`--group` **没有默认值** —— 群号查不出 openid（库里 `groups` 表存的是 openid），
只能你自己事先确认好是哪个群。

看完把结论告诉我（或者直接改 `.env` 的 `TICKET_IMAGE_W`）：

* 第一条消息里，哪一档**刚好占满**、哪一档**开始溢出/被裁**；
* 第二条消息里，哪些行**真的变大了/变粗了/变斜了**，哪些**原样打出了 `#` `>` `-`**。

结论要写进 CLAUDE.md —— 不然下次又会有人照着文档猜。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qq_rss_bot import multiimage, tickets  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_MARKDOWN = 2

# 想试的宽度。覆盖「肯定不够」到「多半会溢出」，中间那个就是答案。
WIDTHS = (208, 320, 480, 640)


def _arg(name: str) -> str | None:
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def _width_msg(url: str, wh: tuple[int, int]) -> str:
    """同一条消息里把同一张图按 4 个宽度排一遍。

    每张图上面标出宽度 —— 对着气泡右边缘看哪一张刚好贴齐。
    """
    lines = ["**图片宽度 A/B（看哪张刚好占满气泡）**", ""]
    for w in WIDTHS:
        bw, bh = multiimage.box_for(wh, w)
        lines.append(f"**{w}px**")
        lines.append(f"![图 #{bw}px #{bh}px]({url})")
        lines.append("")
    lines.append("↑ 哪一档贴齐了右边缘、哪一档开始溢出，记下来")
    return "\n".join(lines)


def _syntax_msg() -> str:
    """把候选语法一条条列出来，看哪几条真的被渲染。"""
    return "\n".join(
        [
            "**markdown 语法 A/B（看哪些真的生效）**",
            "",
            "# 一级标题 #",
            "## 二级标题 ##",
            "### 三级标题 ###",
            "",
            "**加粗（已知能用）**",
            "*斜体*",
            "~~删除线~~",
            "`行内代码`",
            "",
            "> 引用一行",
            "",
            "- 无序列表 A",
            "- 无序列表 B",
            "",
            "1. 有序列表 A",
            "2. 有序列表 B",
            "",
            "---",
            "",
            "↑ **加粗**是已经在用的，其余哪几行变了样子？原样打出 `#` `>` `-` 的"
            "就是**不支持**，别写进生产文案",
            "",
            "**链接 A/B（抽票列表那一行用哪种）**",
            "",
            "① **1. [加粗套链接的场名](https://asobiticket2.asobistore.jp/booths/va-liv-3rdlive)**",
            "② [在 ASOBI TICKET 打开](https://asobiticket2.asobistore.jp/booths/va-liv-3rdlive)",
            "③ **1. 加粗的场名**",
            "",
            "↑ 现在生产用的是 **②**（场名加粗单独一行，链接再占一行）。"
            "① 能渲染的话可以省掉一行、链接直接挂在场名上；"
            "渲染失败是**静默**的 —— 只会看到裸的 `**1. [名字](url)**`，"
            "所以**先在这里确认**，确认了再改 `tickets._title_line`。",
        ]
    )


async def _send(group: str, markdown: str, cfg, label: str) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"  取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }
        r = await client.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers=headers,
            json={"msg_type": MSG_TYPE_MARKDOWN, "markdown": {"content": markdown}},
        )
        print(f"  {label}: ← HTTP {r.status_code}  {r.text[:200]}")
        body = (
            r.json()
            if r.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        return 0 if (r.status_code < 300 and not body.get("code")) else 1


async def main() -> int:
    args = sys.argv[1:]
    do_send = "--send" in args
    group = _arg("--group")
    cfg = load_config()

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        recs, *_ = await tickets.fetch(http, cfg.ticket_url)
        imas = tickets.filter_imas(recs, cfg.ticket_brands)
        # 封面在 booths 那条接口上（每场 live 一张主视觉）
        booths, *_ = await tickets.fetch_booths(http, tickets.booths_url(cfg.ticket_url))

        # 挑一张**一定有封面**的：booth 级 17/17 全有，受付级只有一半
        url, wh = None, None
        for rec in imas:
            cand = tickets.cover_url(rec, cfg.ticket_image_size, booths)
            if cand and (wh := await multiimage.measure(http, cand)):
                url = cand
                break

    if not url or not wh:
        print("找不到可用的封面图，先跑 probe_ticket.py 看看接口通不通")
        return 1

    print(f"封面: {url}")
    print(f"真实: {wh[0]}×{wh[1]}")
    print(f"当前配置 TICKET_IMAGE_W = {cfg.ticket_image_w}")
    print()
    print("=" * 70)
    print(_width_msg(url, wh))
    print("=" * 70)
    print(_syntax_msg())
    print("=" * 70)

    if not do_send:
        print("\n（默认只打印。要真发：--group <openid> --send）")
        return 0

    if not group:
        print("\n❌ --send 必须同时给 --group <openid>。")
        print("   这个脚本**故意没有默认群** —— 只允许发测试群或私聊你自己。")
        return 2

    print(f"\n⚠️  真发到群 {group[:8]}…，两条消息")
    rc = await _send(group, _width_msg(url, wh), cfg, "宽度")
    rc |= await _send(group, _syntax_msg(), cfg, "语法")
    if rc == 0:
        print("\n发出去了。去群里看一眼，把结论填进 .env 的 TICKET_IMAGE_W，")
        print("顺便告诉我哪些 markdown 语法生效，我写进 CLAUDE.md。")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
