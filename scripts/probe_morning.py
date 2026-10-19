"""看一眼早安语音「现在会发什么、为什么」—— **默认一条都不发**。

    uv run python scripts/probe_morning.py                          # 只打印
    uv run python scripts/probe_morning.py --now 2026-10-20T07:30   # 假装现在是那个点
    uv run python scripts/probe_morning.py --group <openid> --send  # 才真发一条

打出来的东西：素材目录扫到了什么、**哪几个文件被跳过（以及原因）**、
这个时刻算出来的计划时刻、在不在窗口里、库里上次发的是谁、最近几天发过哪些偶像、
这一拍会挑谁和哪一条。--send 时按真实链路发一条。

## ⚠️ `--group` **故意没有默认值**

群 openid 不是群号 —— 拿到手也看不出是不是测试群（CLAUDE.md 里那条硬性约束：
「先查一下是不是测试群」这件事在库里做不到，只能靠事先确认）。所以这里
不提供「默认发到某某群」这种便利写法，必须显式给。

**只发测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
已知：老测试群 openid 尾号 …4C056、新测试群 …36A1、**正式群 …E0629（绝不用于测试）**。

## 和 `make_morning.py` 的分工

那个在 **Mac 上离线**跑（需要 Fish Audio key + `pysilk` + 代理）。
这个在**任何地方**都能跑，**只需要素材目录**，因为运行时本来就不碰 TTS。
所以排查线上问题（「今天怎么没发」「发的是谁」）直接跑这个就行。
"""

from __future__ import annotations

import asyncio
import base64
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot import morning  # noqa: E402
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


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now()
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"--now 要 ISO 形态，如 2026-10-20T07:30，收到：{raw!r}") from None


async def _send_voice(group: str, path: Path, cfg) -> int:
    """按真实链路发一条语音（`file_type=3` + `msg_type=7` + `content=""`）。

    走的是和 `RssBot.send_voice` 一样的两个请求，只是不经过 botpy ——
    这样探针不需要构造 Client，也就不会和线上那个实例抢 WebSocket 会话。
    """
    raw = path.read_bytes()
    if raw[:10] != morning.SILK_MAGIC:
        print(f"  ❌ {path.name} 不是 silk，不发（上传接口不校验格式，只能自己挡）")
        return 1
    async with httpx.AsyncClient(timeout=120) as client:
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
        up = await client.post(
            f"{API_BASE}/v2/groups/{group}/files",
            headers=headers,
            json={
                "file_type": 3,  # 3=语音 silk
                "file_data": base64.b64encode(raw).decode(),
                "srv_send_msg": False,
            },
        )
        try:
            up_data = up.json() or {}
        except ValueError:
            up_data = {}
        file_info = up_data.get("file_info")
        print(f"  上传 ← HTTP {up.status_code} "
              f"file_info={'有' if file_info else '无'}（{len(raw):,} 字节）")
        if not file_info:
            print(f"     {str(up_data)[:400]}")
            return 1

        sent = await client.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers=headers,
            json={
                "msg_type": MSG_TYPE_RICH,
                "content": "",              # 只发语音气泡，不要字幕
                "media": {"file_info": file_info},
            },
        )
        try:
            sent_data = sent.json() or {}
        except ValueError:
            sent_data = {}
        code = str(sent_data.get("code") or "")
        if sent.status_code < 300 and not code:
            print(f"  ✅ 发出去了（HTTP {sent.status_code}）—— "
                  "去客户端确认：① 出现语音气泡 ② 点开能播 ③ 是女声日语 ④ 像那位角色")
            return 0
        print(f"  ❌ 发送失败 HTTP {sent.status_code} code={code or '-'}")
        print(f"     {str(sent_data)[:400]}")
        return 1


async def main() -> int:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    group = _arg("--group")
    do_send = "--send" in args
    all_files = "--all" in args
    now = _parse_now(_arg("--now"))
    cfg = load_config()
    directory = Path(_arg("--dir") or cfg.morning_dir) / "audio"

    if do_send and not group:
        print("❌ --send 必须配 --group <group_openid>。**这里故意没有默认值** ——\n"
              "   群 openid 不是群号，拿到手也看不出是不是测试群。\n"
              "   只发老测试群（…4C056）或私聊你自己。绝不发正式群。")
        return 2

    print(f"配置：MORNING_TIME={cfg.morning_time}  窗口 {cfg.morning_window_sec}s  "
          f"不重复 {cfg.morning_recent_days} 天")
    print(f"      MORNING_GROUPS={len(cfg.morning_groups)} 个"
          f"{'' if cfg.morning_groups else '（**空 = 功能关闭**）'}   "
          f"enabled={cfg.morning_enabled}")
    print(f"素材目录：{directory}")
    if not directory.is_dir():
        print("  ❌ 目录不存在。先跑 scripts/make_morning.py，再把 audio/ 同步上去。")
        return 1

    print("\n" + "=" * 72)
    voices, skipped = morning.scan_voices(directory)
    total = sum(len(v) for v in voices.values())
    print(f"扫到 {len(voices)} 位偶像 / {total} 条语音：")
    for idol in sorted(voices):
        files = sorted(p.name for p in voices[idol])
        print(f"  {idol:10s} {len(files)} 条  {'、'.join(files)}")
    if skipped:
        print(f"\n⚠️  跳过 {len(skipped)} 个文件（**不会**被发出去）：")
        for name, why in skipped:
            print(f"  · {name} —— {why}")
        print("  （魔数不对的最要命：上传接口不校验格式，传上去就是个点不开的气泡）")
    if not voices:
        print("\n❌ 一条能发的都没有，线上这一刻会直接跳过。")
        return 1

    # ---- 时刻 / 窗口 ----
    at = morning.parse_time(cfg.morning_time)
    slot = morning.slot_for(now, at)
    slot_key = slot.strftime("%Y-%m-%dT%H:%M")
    delta = (now - slot).total_seconds()
    in_window = delta <= cfg.morning_window_sec
    print(f"\n现在      : {now:%Y-%m-%d %H:%M:%S}（{delta / 60:.1f} 分钟前过了 {slot_key}）")
    print(f"计划时刻  : {slot_key}")
    print(f"在窗口内  : {'是' if in_window else '否'} —— "
          + ("这一拍会发" if in_window else
             f"**不补发**（晚了 {delta:.0f} 秒 > {cfg.morning_window_sec} 秒），"
             "要等明天 " + cfg.morning_time))

    # ---- 库 ----
    db = Database(cfg.db_path)
    try:
        last_line = db.morning_last_line()
        recent = db.morning_recent_idols(cfg.morning_recent_days)
        posted = db.morning_posted(slot_key)
        print(f"\n上次发的台词: {last_line or '（没有记录）'}")
        print(f"最近 {cfg.morning_recent_days} 天发过的偶像（{len(recent)} 位）: "
              + ("、".join(sorted(recent)) if recent else "（没有）"))
        print(f"这个 slot 已发的群: {len(posted)} 个"
              + ("（已发的群这一拍会跳过）" if posted else ""))

        # ---- 这一拍会挑谁、哪一条 ----
        idol = morning.pick_idol(voices, recent)
        line = morning.pick_line(voices[idol], last_line)
        print("\n" + "-" * 72)
        print(f"这一拍会发: {idol} / {line.name}（{line.stat().st_size:,} 字节）")
        if all_files:
            print(f"  {idol} 的候选：")
            for path in sorted(voices[idol]):
                mark = "  ← 上次发过（有别的可选时会避开）" if path.name == last_line else ""
                print(f"    · {path.name}{mark}")
        # 随机的东西打一次不算数，把候选池说清楚
        print(f"  候选池：{len(voices) - len([i for i in voices if i in recent])} 位"
              f"（避开 recent 后；全被避开时会在全部 {len(voices)} 位里挑）")
    finally:
        db.close()

    if do_send:
        print("\n" + "=" * 72)
        print(f"⚠️  真发到群 {group[:8]}…：一条 msg_type=7 语音，**没有文字**")
        print("-" * 72)
        rc = await _send_voice(group, line, cfg)
        if rc == 0:
            print("\n真发之后要**用耳朵**确认的：")
            print("  1. 群里出现**语音气泡**，点开**能播**、是**女声日语**、音质正常")
            print("  2. **听起来像那位角色**（音色只能靠耳朵，看日志看不出来）")
            print("  3. **语气提示有没有生效**（起伏、软硬）——「语音习惯」全靠它")
            print("  ⚠️ 探针**不写库**，所以这次发送不会让线上那一拍认为「今天发过了」")
        return rc

    print("\n（默认只打印。要真发：--group <openid> --send）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
