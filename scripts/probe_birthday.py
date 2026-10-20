"""看一眼生日推送「现在会发什么、为什么」—— **默认一条都不发、一次网都不上**。

    uv run python scripts/probe_birthday.py                          # 只打印（离线）
    uv run python scripts/probe_birthday.py --now 2026-10-18T23:05   # 假装现在是那个点
    uv run python scripts/probe_birthday.py --fetch                  # 真去抓一次推文
    uv run python scripts/probe_birthday.py --fetch --proxy http://192.168.0.63:6152
    uv run python scripts/probe_birthday.py --group <openid> --send  # 才真发一条

打出来的东西：冷数据在哪、有多少天/多少位、这个时刻算出来的推送时刻、
**被祝福的那天**（⚠️ 是推送时刻的**第二天**）、当天的名单、渲染出来的原文、
在不在窗口里、库里这天已经发给哪些群、推文抓不抓得到。

## ⚠️ `--group` **故意没有默认值**

群 openid 不是群号 —— 拿到手也看不出是不是测试群（CLAUDE.md 里那条硬性约束：
「先查一下是不是测试群」这件事在库里做不到，只能靠事先确认）。所以这里
不提供「默认发到某某群」这种便利写法，必须显式给。

**只发测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
已知：老测试群 openid 尾号 …4C056、新测试群 …36A1、**正式群 …E0629（绝不用于测试）**。

## 为什么 --send 走的是真轮询器

`--send` 不自己拼请求，而是构造一个 `BirthdayPoller` + 一个走裸 HTTP 的假
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

from qq_rss_bot import birthday, preview  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_RICH = 7


def _arg(name: str, default: str | None = None) -> str | None:
    """`--name value` 或 `--name=value`。"""
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
        raise SystemExit(f"--now 要 ISO 形态，如 2026-10-18T23:05，收到：{raw!r}") from None


class _RawSender:
    """走裸 HTTP 的发送出口 —— 和 `RssBot.send_text` / `send_local_image` 同构。

    两个请求：`POST /v2/groups/{g}/files`（`srv_send_msg=False` 拿 `file_info`）
    → `POST /v2/groups/{g}/messages`（`msg_type=7`）。**不用 botpy**，
    所以探针不会和线上实例抢 WebSocket 会话。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._headers: dict[str, str] | None = None

    async def _h(self) -> dict[str, str]:
        # token 取一次就复用 —— 官方对取 token 本身也有频控。
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

    async def send_text(self, group: str, content: str) -> None:
        headers = await self._h()
        assert self._client is not None
        resp = await self._client.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers=headers,
            json={"msg_type": 0, "content": content},
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 300 or data.get("code"):
            raise RuntimeError(f"HTTP {resp.status_code} code={data.get('code')}: {data}")

    async def send_local_image(self, group: str, content: str, path: Path) -> None:
        raw = Path(path).read_bytes()
        payload = base64.b64encode(raw).decode()
        if len(payload) > preview.MAX_FILE_DATA_BYTES:
            raise RuntimeError(
                f"图片 base64 后 {len(payload)} 字节，超 QQ 的 "
                f"{preview.MAX_FILE_DATA_BYTES} 直传上限"
            )
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


def _print_calendar(cal: birthday.Calendar, path: Path) -> None:
    filled = [d for d in cal.days.values() if not d.empty]
    idols = sum(len(d.idols) for d in cal.days.values())
    people = sum(len(d.people) for d in cal.days.values())
    print(f"冷数据：{path}")
    print(
        f"  {len(cal.days)} 天（有人的 {len(filled)} 天，空 {len(cal.days) - len(filled)} 天）"
        f" / 偶像 {idols} 条 / 声优 {people} 条 / 品牌 {len(cal.brands)} 个"
    )


async def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    now = _parse_now(_arg("--now"))
    group = _arg("--group")
    do_send = _flag("--send")
    do_fetch = _flag("--fetch") or do_send
    proxy_override = _arg("--proxy")

    if do_send and not group:
        print("❌ --send 必须同时给 --group <openid>（群 openid 不是群号，看不出是不是测试群）")
        return 2

    cfg = load_config()
    print(f"配置：birthday_enabled={cfg.birthday_enabled} "
          f"time={cfg.birthday_time} window={cfg.birthday_window_sec}s")
    print(f"  目标群 {len(cfg.birthday_groups)} 个："
          f"{[g[:8] + '…' for g in cfg.birthday_groups] or '（空 = 功能关闭）'}")
    proxy = proxy_override if proxy_override is not None else cfg.media_proxy
    print(f"  代理 media_proxy={proxy or '（空 = 直连，容器里必然失败）'}"
          f"{'（--proxy 覆盖）' if proxy_override is not None else ''}")

    cal_path = Path(cfg.birthday_dir) / birthday.CALENDAR_NAME
    try:
        cal = birthday.load_calendar(cal_path)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 冷数据读不了：{cal_path}\n   {exc}")
        print("   先跑 scripts/fetch_birthday_source.py，再把 data/birthday/ 同步到 NAS。")
        return 1
    _print_calendar(cal, cal_path)

    # ---- 时刻 → 名单 ----
    at = birthday.parse_time(cfg.birthday_time)
    slot = birthday.slot_for(now, at)
    d = birthday.birthday_date(slot)
    late = (now - slot).total_seconds()
    print(f"\n现在 {now:%Y-%m-%d %H:%M:%S}（本地）")
    print(f"  → 计划时刻 {slot:%Y-%m-%dT%H:%M}")
    print(f"  → 被祝福的那天 **{d}**（⚠️ 是计划时刻的第二天）")
    inside = late <= cfg.birthday_window_sec
    print(f"  → 距计划时刻 {late:.0f} 秒，窗口 {cfg.birthday_window_sec} 秒："
          f"{'✅ 在窗口内' if inside else '❌ 窗口外（今天不发）'}")

    day = cal.for_date(d)
    if day.empty:
        print(f"  → {d} **没人过生日**（366 天里有 46 天是这样），不发。")
    else:
        print(f"  → 名单 {day.total} 位：偶像 {[p.name for p in day.idols]} "
              f"/ 声优 {[(p.name, p.role) for p in day.people]}")
        print("  ---- 文案 ----")
        for line in birthday.render(d, day).splitlines():
            print(f"  | {line}")
        print("  --------------")

    # ---- 库 ----
    db = Database(cfg.db_path)
    try:
        done = db.birthday_posted(d.isoformat())
        print(f"\n库：{d} 已发给 {len(done)} 个群 "
              f"{[g[:8] + '…' for g in sorted(done)] or '（一个都没有）'}")
        pending = [g for g in cfg.birthday_groups if g not in done]
        print(f"  这轮还会发：{len(pending)} 个群 {[g[:8] + '…' for g in pending]}")

        # ---- 抓推文（可选）----
        if do_fetch:
            print(f"\n抓 {birthday.X_PROFILE_URL} …")
            client = preview.make_client(
                proxy,
                ca_bundle=cfg.media_ca_bundle,
                insecure=cfg.media_insecure_tls,
            )
            try:
                tweets = await birthday.fetch_timeline(client)
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ 抓不到：{type(exc).__name__}: {exc}")
                print("     容器里直连必然失败，线上必须是 MEDIA_PROXY 生效。")
                print("     Mac 上本地 .env 没配代理时，用 --proxy 指到 Surge 再试。")
                return 1
            try:
                print(f"  时间线 {len(tweets)} 条：")
                for t in tweets[:8]:
                    mark = "🎂" if t in birthday.pick_birthday_tweets(tweets, d) else "  "
                    print(f"   {mark} {t.id} {t.posted_at} 图{len(t.images)} "
                          f"{t.text[:50]!r}")
                found = await birthday.fetch_day_image(client, tweets, d)
                if found:
                    tid, raw = found
                    print(f"  ✅ 当天推文 {tid}，配图 {len(raw):,} 字节 "
                          f"（base64 后 {len(raw) * 4 // 3:,}）")
                else:
                    print(f"  ⚠️ 没找到 {d.month}/{d.day} 的配图 —— "
                          "窗口末尾会**发纯文字**（名单不依赖推文）")
            finally:
                await client.aclose()

        # ---- 真发 ----
        if do_send:
            print(f"\n真发到 {group[:8]}…（⚠️ 只该是测试群或你本人）")
            sender = _RawSender(cfg)
            client = preview.make_client(
                proxy,
                ca_bundle=cfg.media_ca_bundle,
                insecure=cfg.media_insecure_tls,
            )
            # ⚠️ **把窗口收到刚好用完**，这样它立刻发，而不是「返回 0 等下一拍重试」。
            # 线上的窗口是 300 秒、给抓图留重试；探针是一次性的，等不了。
            # 效果：抓到图就发图文，没抓到就发纯文字 —— 正是窗口末尾的行为。
            forced = dataclasses.replace(
                cfg, birthday_window_sec=int(late) + birthday.CHECK_SEC
            )
            try:
                poller = birthday.BirthdayPoller(forced, db, sender, client)
                # 用**真实轮询器**跑一遍 —— 探针验的就是线上那段代码。
                n = await poller.poll_once(now=now)
                if n:
                    print(f"  ✅ 发出去了 {n} 条 —— 去客户端确认图渲染出来了、"
                          "文案和上面那份一致")
                else:
                    print("  poll_once 返回 0（窗口外 / 今天没人过生日 / 这个群已发过）")
            finally:
                await sender.close()
                await client.aclose()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
