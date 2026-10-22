"""看一眼誕生祭贺图「现在会收什么、为什么」—— **默认一条都不发**。

    uv run python scripts/probe_birthday_art.py                      # 只打印（离线，一次网都不上）
    uv run python scripts/probe_birthday_art.py --now 2026-10-19T22:30
    uv run python scripts/probe_birthday_art.py --search             # 真搜一次，只打印不发
    uv run python scripts/probe_birthday_art.py --search --group <openid> --send
    uv run python scripts/probe_birthday_art.py --group <openid> --reset      # 先清账

## ⚠️ `--send` 跑第二次会「发出去 0 条」—— 那是幂等，不是坏了

`poll_once` 里有**同日同群不重复发**的记账。第一次真发成功之后，第二次跑
**一定**是 0 条（`day + group` 已经在库里，`targets` 为空就直接 `return 0`）。

所以改完挑选规则想拿**同一天**重测，必须先清账，否则永远看到 0：

    ... --group <openid> --reset --send

`--reset` 先打印「原来有没有那行」，删掉，然后照常跑。
⚠️ **生产代码永远不该调 `clear_birthday_art`** —— 记账的意义就是不重复发。

另外，一旦打出「发出去 0 条」，脚本会把**能查的原因挨个查一遍**列出来
（没启用 / 窗口外 / 已经发过 / 这天没人过生日 / 没过下限）—— `poll_once`
有 8 条 `return 0` 的路，光一个 0 看不出是哪条。

打出来的东西：开关 / 时刻 / 窗口 / 张数 / 点赞下限、cookie **长度**（不是内容）、
冷数据统计、**今天是谁的生日**、四个 tag、slot → **被庆祝的那天**（⚠️ 是 slot
**当天**，和生日推送差一天）、在不在窗口里、库里这天已经发给哪些群、
`--search` 时的每个 tag 命中数、排名表、以及**将要发出去的文案原文**。

## ⚠️ `--group` **故意没有默认值**

群 openid 不是群号 —— 拿到手也看不出是不是测试群（CLAUDE.md 里那条硬性约束：
「先查一下是不是测试群」这件事在库里做不到，只能靠事先确认）。所以这里
不提供「默认发到某某群」这种便利写法，必须显式给。

**只发测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
已知：老测试群 openid 尾号 …4C056、新测试群 …36A1、**正式群 …E0629（绝不用于测试）**。

## 为什么 --send 走的是真轮询器

`--send` 不自己拼请求，而是构造一个 `BirthdayArtPoller` + 一个走裸 HTTP 的假
`sender`（不经过 botpy，**不抢线上那个实例的 WebSocket 会话**），跑一遍
`poll_once()`。这样探针验的就是线上真正跑的那段代码，而不是它的复制品。
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import birthday, birthday_art, features, preview, seiyuu  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_RICH = 7


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


def _flag(name: str) -> bool:
    return name in sys.argv[1:]


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now()
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--now 要 ISO 形态，如 2026-10-19T22:30，收到：{raw!r}") from None


class _RawSender:
    """走裸 HTTP 的发送出口 —— 和 `RssBot.send_local_image` 同构（两步上传）。

    **不用 botpy**，所以探针不会和线上实例抢 WebSocket 会话。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._headers: dict[str, str] | None = None

    async def _h(self) -> dict[str, str]:
        if self._headers is not None:
            return self._headers
        self._client = httpx.AsyncClient(timeout=120)
        resp = await self._client.post(
            TOKEN_URL, json={"appId": self.cfg.appid, "clientSecret": self.cfg.secret}
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            raise RuntimeError(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
        self._headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.cfg.appid,
            "Content-Type": "application/json",
        }
        return self._headers

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def send_local_image(self, group: str, content: str, path: Path) -> None:
        raw = Path(path).read_bytes()
        payload = base64.b64encode(raw).decode()
        if len(payload) > preview.MAX_FILE_DATA_BYTES:
            raise RuntimeError(f"图片 base64 后 {len(payload)} 字节，超直传上限")
        headers = await self._h()
        assert self._client is not None
        up = await self._client.post(
            f"{API_BASE}/v2/groups/{group}/files",
            headers=headers,
            json={"file_type": 1, "file_data": payload, "srv_send_msg": False},
        )
        up_data = up.json() if up.content else {}
        file_info = up_data.get("file_info")
        if not file_info:
            raise RuntimeError(f"上传图片没拿到 file_info: HTTP {up.status_code} {up_data}")
        sent = await self._client.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers=headers,
            json={"msg_type": MSG_TYPE_RICH, "content": content,
                  "media": {"file_info": file_info}},
        )
        sent_data = sent.json() if sent.content else {}
        if sent.status_code >= 300 or sent_data.get("code"):
            raise RuntimeError(
                f"HTTP {sent.status_code} code={sent_data.get('code')}: {sent_data}"
            )


async def main() -> int:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return 0

    now = _parse_now(_arg("--now"))
    group = _arg("--group")
    do_reset = _flag("--reset")
    do_send = _flag("--send")
    do_search = _flag("--search") or do_send or _flag("--download")
    do_download = _flag("--download") or do_send

    if do_send and not group:
        print("❌ --send 必须同时给 --group <openid>（群 openid 不是群号，看不出是不是测试群）")
        return 2
    if do_reset and not group:
        print("❌ --reset 必须同时给 --group <openid> —— 清账要指名道姓，别一不小心全清了")
        return 2

    cfg = load_config()
    print(f"配置：enabled={cfg.birthday_art_enabled} time={cfg.birthday_art_time} "
          f"window={cfg.birthday_art_window_sec}s max={cfg.birthday_art_max} "
          f"min_likes={cfg.birthday_art_min_likes}")
    print(f"  代理 media_proxy={cfg.media_proxy or '（空 = 直连）'}")
    have = (bool(cfg.x_auth_token), bool(cfg.x_ct0))
    print(f"  cookie：auth_token {'✅ %d 字符' % len(cfg.x_auth_token) if have[0] else '❌ 缺'} / "
          f"ct0 {'✅ %d 字符' % len(cfg.x_ct0) if have[1] else '❌ 缺'}"
          f"{'' if all(have) else '  ← 缺一个整个功能就是关的'}")

    cal_path = Path(cfg.birthday_dir) / birthday.CALENDAR_NAME
    try:
        cal = birthday.load_calendar(cal_path)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 冷数据读不了：{cal_path}\n   {exc}")
        print("   先跑 scripts/fetch_birthday_source.py，再把 data/birthday/ 同步到 NAS。")
        return 1
    print(f"\n冷数据：{cal_path}（{len(cal.days)} 天 / 品牌 {len(cal.brands)} 个）")

    # ---- 时刻 → 名单 ----
    slot = birthday_art.slot_for(now, birthday_art.parse_time(cfg.birthday_art_time))
    d = birthday_art.art_date(slot)
    late = (now - slot).total_seconds()
    in_win = 0 <= late <= cfg.birthday_art_window_sec
    print(f"\n现在（本地）：{now:%Y-%m-%d %H:%M:%S}")
    print(f"  这一次的 slot：{slot:%Y-%m-%d %H:%M}（已过去 {late:.0f} 秒）")
    print(f"  ⚠️ 被庆祝的那天：**{d}**（= slot 当天，和生日推送的「+1 天」不是一回事）")
    print(f"  窗口 {cfg.birthday_art_window_sec}s：{'✅ 在里面' if in_win else '❌ 在外面（这一拍不发）'}")
    lo, hi = birthday_art.window_bounds(d)
    print(f"  收图时间窗（JST）：[{lo:%Y-%m-%d %H:%M}, {hi:%Y-%m-%d %H:%M})")

    day = cal.for_date(d)
    print(f"\n{d} 的偶像 {len(day.idols)} 位："
          f"{[i.name for i in day.idols] or '（没有 —— 这天不收贺图）'}")
    if day.idols:
        for idol in day.idols:
            print(f"  {idol.name:<12} {birthday_art.tags_for(idol.name, d.year)}")

    db = Database(cfg.db_path)
    groups = features.groups_for(db, "birthday_art")
    print(f"  目标群 {len(groups)} 个："
          f"{[g[:8] + '…' for g in groups] or '（空 = 功能关闭）'}")
    if do_reset:
        # ⚠️ poll_once 有「同日同群不重复发」的记账。不清账的话，改完规则重测
        #    永远是「发出去 0 条」，看起来像坏了。
        assert group is not None
        had = db.clear_birthday_art(d.isoformat(), group)
        print(f"\n🧹 --reset：{d} / {group[:8]}… 原来"
              f"{'**有一行**，已删' if had else '本来就没有'}")
    done = db.birthday_art_posted(d.isoformat())
    print(f"\n库里 {d} 已发的群：{sorted(g[:8] + '…' for g in done) or '（还没发过）'}")

    if not day.idols:
        print("\n这天没有偶像过生日，到此为止。")
        if not do_send:
            db.close()
            return 0

    sender = _RawSender(cfg) if do_send else None
    run_cfg = cfg
    if do_send:
        # 把窗口临时收成「刚好用完」，好让它立刻发而不是等下一拍。
        run_cfg = dataclasses.replace(
            cfg,
            birthday_art_window_sec=int(max(late, 0)) + birthday_art.CHECK_SEC,
        )
    poller = birthday_art.BirthdayArtPoller(
        run_cfg, db, sender, None,
        groups_override=(group,) if do_send else None)

    if not do_search:
        print("\n（离线模式：没上过网。加 --search 才真去搜一次）")
        db.close()
        return 0

    # ---- 真搜（网络全在这个 with 里 —— 客户端一关就发不出请求了）----
    got: list = []
    arts: list = []
    ranked: list = []
    async with preview.make_client(cfg.media_proxy, timeout=birthday_art.SEARCH_TIMEOUT) as http:
        poller.http = http
        poller.x = birthday_art.XSearch(http, cfg.x_auth_token, cfg.x_ct0)
        arts = await poller._collect([i.name for i in day.idols], d.year, d)

        print(f"\n合计去重后 {len(arts)} 条候选（已过滤时间窗）")
        if arts:
            likes = sorted(a.likes for a in arts)
            print(f"  点赞：min={likes[0]} 中位={likes[len(likes) // 2]} max={likes[-1]}；"
                  f"≥{cfg.birthday_art_min_likes} 的 "
                  f"{sum(1 for x in likes if x >= cfg.birthday_art_min_likes)} 条")
        book = seiyuu.load()
        classify = birthday_art.make_classifier(book, exclude=cfg.birthday_art_exclude)
        print(f"声优库：{'已加载' if book else '读不到（全部按画师）'}"
              + (f"（{len(book.roles)} 角色 / {len(book.handles)} 人 / "
                 f"{len(book.officials)} 官号）" if book else ""))
        ranked = birthday_art.rank_art(
            arts,
            [i.name for i in day.idols],
            min_likes=cfg.birthday_art_min_likes,
            max_n=cfg.birthday_art_max,
            exclude=cfg.birthday_art_exclude,
            skip_official=cfg.birthday_art_skip_official,
            classify=classify,
        )
        print(f"过滤 + 排序后 {len(ranked)} 条，将要发前 {cfg.birthday_art_max} 条：")
        for i, a in enumerate(ranked[: cfg.birthday_art_max]):
            print(f"  [{i + 1}] ♥{a.likes:<6} {classify(a)[1]:<3} @{a.author:<18} "
                  f"{a.posted_at.astimezone(birthday_art.JST):%m-%d %H:%M}JST {a.idol}")
            print(f"      {a.image}")
        if len(ranked) > cfg.birthday_art_max:
            print(f"  ……还有 {len(ranked) - cfg.birthday_art_max} 条没排上")

        if do_download:
            # 下载这一环单独验：图能不能下来、多大、base64 之后会不会超 QQ 的直传上限。
            got = await poller._download(ranked, cfg.birthday_art_max)

        if do_send:
            # ⚠️ **必须在这个 with 里** —— poll_once 会再搜一次、再下一次图，
            # 客户端一关就 `RuntimeError: Cannot send a request, as the client
            # has been closed`。第一版写在 with 外面，--send 是必崩的。
            print(f"\n>>> 真发到 {group[:8]}…"
                  f"（窗口已临时收成 {run_cfg.birthday_art_window_sec}s）")
            # ⚠️ **`poll_once` 有 8 条 `return 0` 的路，「发出去 0 条」本身一点
            #    信息量都没有。** 所以先把能查的条件挨个查一遍打出来 ——
            #    实测最常撞的是「已经发过」（幂等），看着像坏了。
            why: list[str] = []
            if not poller.enabled:
                why.append("功能没启用（开关 / 群 / cookie 缺一样就是关的）")
            if late > run_cfg.birthday_art_window_sec:
                why.append(f"在窗口外（late={late:.0f}s > "
                           f"{run_cfg.birthday_art_window_sec}s）")
            if group in db.birthday_art_posted(d.isoformat()):
                why.append(f"**{d} 这个群已经发过了** —— 幂等挡下（加 --reset 清账）")
            if not day.idols:
                why.append(f"{d} 没有偶像过生日")
            if not ranked:
                why.append(f"没有一条过下限（{cfg.birthday_art_min_likes} 赞）")
            for w in why:
                print(f"    ⚠️ {w}")
            if not why:
                print("    （能查的条件都过了，剩下的看下面的日志）")
            n = await poller.poll_once(now=now)
            print(f">>> 发出去 {n} 条"
                  + ("（原因见上面那几行 ⚠️）" if n == 0 and why else ""))
            print(f">>> 库里现在是："
                  f"{sorted(g[:8] + '…' for g in db.birthday_art_posted(d.isoformat()))}")

    if do_download:
        print(f"\n下载结果：{len(got)}/{min(len(ranked), cfg.birthday_art_max)} 张成功"
              f"（原图上限 {preview.MAX_IMAGE_BYTES // 1000} KB，"
              f"base64 上限 {preview.MAX_FILE_DATA_BYTES // 1024 // 1024} MiB）")
        for a, raw in got:
            b64 = len(raw) * 4 // 3
            flag = "⚠️ 超上限" if b64 > preview.MAX_FILE_DATA_BYTES else "✅"
            print(f"  {flag} {len(raw) / 1024:7.0f} KB（base64 {b64 / 1024:7.0f} KB）  @{a.author}")
        print("\n将要发出去的文案原文：")
        for i, (a, _raw) in enumerate(got):
            print("  ┌" + "─" * 46)
            for line in birthday_art.render(a, i + 1, len(got), d.year,
                                            label=classify(a)[1]).splitlines():
                print(f"  │ {line}")
        print("  └" + "─" * 46)

    if not do_send:
        print("\n（只是预览，一条都没发。加 --send 才真发）")
    if sender is not None:
        await sender.close()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
