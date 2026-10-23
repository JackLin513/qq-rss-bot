"""ASOBI TICKET 抽票提醒的探针。

用来回答三件事，**全程默认不发任何消息**：

1. 接口现在返回什么 —— 多少条受付、哪些被品牌过滤剔掉了；
2. 会推什么 —— 给定时刻下哪几条提醒会响（`--now` / `--scan-days`）；
3. 封面图长什么样 —— 哪个尺寸、多大、能不能读到宽高。

跑法：

    uv run python scripts/probe_ticket.py                      # 拉真接口，打印现状
    uv run python scripts/probe_ticket.py --raw                # 连原始字段一起看
    uv run python scripts/probe_ticket.py --now 2026-10-18T22:30+09:00   # 假装成某个时刻
    uv run python scripts/probe_ticket.py --scan-days 14       # 扫描未来两周，列出所有会响的提醒
    uv run python scripts/probe_ticket.py --group <openid> --send        # 才真发

⚠️ **只允许发到机器人测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
所以 `--group` 必须显式给 openid，**没有默认值** —— 群号查不出 openid
（库里 `groups` 表存的是 openid），只能你自己事先确认好是哪个群。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import tickets  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_MARKDOWN = 2


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return tickets.now()
    value = datetime.fromisoformat(raw)
    return value if value.tzinfo else value.replace(tzinfo=tickets.JST)


def _print_rec(
    rec: tickets.Reception,
    now: datetime,
    booths: dict[str, dict[str, str]] | None = None,
) -> None:
    booth_cover = (booths or {}).get(rec.booth_slug) or {}
    print(f"  [{rec.sale_label}] {rec.booth_name}")
    print(f"        {rec.name}   状态={rec.status}")
    print(
        f"        申込 {tickets._at(rec.entry_start)} ～ {tickets._at(rec.entry_end)}"
        f"  当落 {tickets._at(rec.result_at, weekday=False)}"
        f"  入金 {tickets._at(rec.deposit_start)} ～ {tickets._at(rec.deposit_end)}"
    )
    # 两个来源分开打：booth 级是每场 live 的主视觉，受付级是单条受付的横幅。
    # 列表用 booth 级；这里两个都看得到，方便判断「这场为什么没图」。
    print(f"        封面 booth={'有' if booth_cover else '无'}"
          f"  受付={'有' if rec.cover else '无'}   {rec.url}")


async def _send(group: str, markdown: str, cfg) -> int:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret})
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"  取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }
        url = f"{API_BASE}/v2/groups/{group}/messages"
        r = await client.post(
            url, headers=headers, json={"msg_type": MSG_TYPE_MARKDOWN, "markdown": {"content": markdown}}
        )
        print(f"  ← HTTP {r.status_code}  {r.text[:300]}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = r.status_code < 300 and not body.get("code")
        print("  ✅ 发出去了，去群里看图渲染出来没有" if ok else "  ❌ 失败")
        return 0 if ok else 1


async def main() -> int:
    args = sys.argv[1:]
    cfg = load_config()
    show_raw = "--raw" in args
    now = _parse_now(_arg("--now"))
    scan_days = int(_arg("--scan-days", "0") or 0)
    group = _arg("--group")
    do_send = "--send" in args
    size = _arg("--size", cfg.ticket_image_size) or cfg.ticket_image_size

    if do_send and not group:
        print("--send 必须配 --group <group_openid>（没有默认值，见文件头的警告）")
        return 2

    print(f"接口 : {cfg.ticket_url}")
    print(f"品牌 : {list(cfg.ticket_brands)}")
    print(f"时刻 : {now.isoformat()}（JST {now.astimezone(tickets.JST):%Y-%m-%d %H:%M}）")
    print(f"封面 : {'开' if cfg.ticket_images else '关'} · {size}")
    print()

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        all_recs, etag, not_modified = await tickets.fetch(http, cfg.ticket_url)
        print(f"拉取 : {len(all_recs)} 条受付，ETag {etag!r}，304={not_modified}")

        # 再拉一次验 ETag —— 这是轮询成本的关键（814 KB → 0 字节）
        _, _, again = await tickets.fetch(http, cfg.ticket_url, etag=etag)
        print(f"复验 : 第二次 304={again}（True 才对，说明轮询很便宜）")

        # 每场 live 的封面在这条接口里（receptions 里的 booth.cover_image 是空的）
        booth_covers, booth_etag, _ = await tickets.fetch_booths(
            http, tickets.booths_url(cfg.ticket_url)
        )
        _, _, booth_304 = await tickets.fetch_booths(
            http, tickets.booths_url(cfg.ticket_url), etag=booth_etag
        )
        print(f"封面 : {len(booth_covers)} 场有主视觉，"
              f"ETag {booth_etag!r}，复验 304={booth_304}")
        print()

        imas = tickets.filter_imas(all_recs, cfg.ticket_brands)
        print(f"过滤 : 留 {len(imas)} 条，剔 {len(all_recs) - len(imas)} 条")
        kept = sorted({tickets.brand_of(r.booth_slug) for r in imas})
        drop = sorted({tickets.brand_of(r.booth_slug) for r in all_recs if not tickets.is_imas(r.booth_slug, cfg.ticket_brands)})
        print(f"       留下品牌 {kept}")
        print(f"       剔掉品牌 {drop}")

        open_now = [r for r in imas if r.status == "within_entry_period"]
        print(f"\n受付中 {len(open_now)} 条：")
        for rec in sorted(open_now, key=lambda r: r.entry_end or datetime.max.replace(tzinfo=tickets.JST)):
            _print_rec(rec, now, booth_covers)
        # 有受付、但封面表里没有的场 —— 列出来才看得出是不是站点新加的
        missing = sorted({r.booth_name for r in imas if r.booth_slug not in booth_covers})
        if missing:
            print(f"\n⚠️ 封面表里没有的场（{len(missing)}）：")
            for name in missing:
                print(f"    {name[:60]}")

        # ---- 会推什么 ----
        print("\n" + "=" * 70)
        if scan_days:
            print(f"扫描未来 {scan_days} 天，列出所有会响的提醒（每条只响一次）：")
            seen: dict[str, set[str]] = {}
            fired: list[tuple[datetime, str, str]] = []
            step = timedelta(minutes=10)
            t = now
            end = now + timedelta(days=scan_days)
            while t < end:
                for rec in imas:
                    for kind in tickets.due_alerts(rec, t, seen.get(rec.id, set())):
                        seen.setdefault(rec.id, set()).add(kind)
                        fired.append((t, kind, rec.title))
                t += step
            for when, kind, title in sorted(fired):
                print(f"  {when.astimezone(tickets.JST):%m/%d %H:%M}  {kind:10} {title}")
            print(f"\n共 {len(fired)} 条。首次运行时 kind='new' 的会被 baseline 吃掉，不会推。")
        else:
            print(f"此刻会推的提醒（seen 为空，即全新状态）：")
            # **和线上一样走 `alert_groups`**：多席位 / 多日期会合并成一条。
            # 这里如果逐条渲染，看到的条数会比群里实际收到的多得多。
            any_fired = False
            for kind, group in tickets.alert_groups(imas, now, {}):
                any_fired = True
                print(f"\n--- {kind} ---")
                print(tickets.render_alert(group, kind, now))
            if not any_fired:
                print("  （没有）")

            print("\n" + "=" * 70)
            print("=== /抽选 ===")
            print(await tickets.render_digest(
                http, imas, now, wanted=("open",),
                size=cfg.ticket_image_size, images=cfg.ticket_images,
                width=cfg.ticket_image_w, booths=booth_covers))
            print("\n=== /当落 ===")
            print(await tickets.render_digest(
                http, imas, now, wanted=("pending", "deposit"),
                size=cfg.ticket_image_size, images=cfg.ticket_images,
                width=cfg.ticket_image_w, booths=booth_covers))

        if show_raw:
            print("\n" + "=" * 70)
            print("原始 data[0].attributes：")
            async with httpx.AsyncClient(timeout=30) as raw_http:
                resp = await raw_http.get(cfg.ticket_url, headers={"Accept": "application/json"})
                for k, v in sorted((resp.json()["data"][0].get("attributes") or {}).items()):
                    print(f"  {k}: {str(v)[:100]}")

        # ---- 真发 ----
        if do_send:
            print("\n" + "=" * 70)
            print(f"⚠️  真发到群 {group[:8]}…，只发**一条**样例（用受付中的第一条）")
            if not open_now:
                print("没有受付中的条目，改发 304 之前拿到的一条")
            rec = open_now[0] if open_now else imas[0]
            text = tickets.render_new(rec)
            if cfg.ticket_images:
                text = await tickets.with_cover(http, rec, text, size, {}, booths=booth_covers)
            print("\n--- 要发的内容 ---")
            print(text)
            print("------------------")
            return await _send(group, text, cfg)

    print("\n（默认只打印。要真发：--group <openid> --send）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
