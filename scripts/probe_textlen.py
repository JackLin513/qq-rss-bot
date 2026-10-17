"""实测 QQ 单条消息的**字数上限**。

**为什么要实测**：官方文档里**根本没有这个数字**。翻遍 v1.36.0 的全部 JS
chunk，只有一条错误码 `40054007 消息长度超限 / 请缩短消息内容`，没有任何
「最多 N 字」的说明；`qq-botpy` 包里也没有相关常量。而 `scheduler._MAX_CONTENT
= 1500` 从写下那天起就是**猜的**（注释写的是「留足余量」），从没验证过。

这个数字决定了两件事：推送文案能塞多长、以及「要不要为了塞下全文去调 LLM
做摘要」。猜错两个方向都难受 —— 猜小了白白摘要，猜大了消息直接被平台拒。

跑法：

    uv run python scripts/probe_textlen.py                  # 干跑，只打印计划
    uv run python scripts/probe_textlen.py --send           # 真发（收件人从库里取）
    uv run python scripts/probe_textlen.py --send <openid>  # 指定收件人
    uv run python scripts/probe_textlen.py --send --sizes 500,1500,3000

⚠️ **默认一个字节都不发。** 走的是 C2C 私聊，收件人默认取 `notify_targets`
里最近登记的那个 —— **只会发给用户本人**，不碰任何群（见 CLAUDE.md 的硬性约束）。

⚠️ **撞频控就立刻停。** 私信主动消息也有配额（`11244 主动消息频次超限`），
把它当成「长度超限」会把结论完全带偏。只有明确的长度相关错误才算「太长」。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# 默认采点：从小到大，撞到第一个失败就停。四个点足够定位一个「几百 vs 几千」
# 的问题，又不会把私信配额打光。
DEFAULT_SIZES = (500, 1500, 3000, 6000)

# 明确的「太长」判据。**收窄到这里是有意的** —— 把 11244（频控）当成
# 「太长」会让整个测量得出反向结论。
TOO_LONG_CODES = ("40054007", "消息长度超限")

# 这些一出现就中止整轮：结论已经不可信了，再发也是浪费配额。
ABORT_CODES = {
    "11244": "主动消息频次超限（私信配额用完了，等一会儿再跑）",
    "22009": "主动消息频次超限",
    "50054": "机器人不在线 —— 主动消息要求连着 gateway，先把机器人跑起来",
    "304049": "私信主动消息数限频",
    "304050": "私信主动消息总量限频",
    "40054013": "用户拒收消息",
    "306005": "没有权限给该用户发消息（多半是没建立会话）",
}


def make_content(n: int) -> str:
    """造一条**长度恰好为 n** 的中文文本。

    用中文而不是 `"x" * n`：如果平台按**字节**或**显示宽度**算，中文和英文
    的结果会差一大截，而我们的真实文案就是中文 —— 拿英文测出来的数字不能用。
    """
    head = f"【字数探针 {n}】"
    unit = "这是一条用于实测 QQ 消息长度上限的文本，收到说明这个长度是通的。"
    if n <= len(head):
        return head[:n]
    body = (unit * (n // len(unit) + 2))[: n - len(head)]
    return head + body


def pick_openid_from_db() -> str | None:
    """从 notify_targets 里取最近登记的那个 openid。**和通知器同一个来源**。"""
    db = ROOT / os.environ.get("DB_PATH", "data/bot.db")
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT member_openid FROM notify_targets ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row["member_openid"] if row else None


async def probe_once(client, headers, openid: str, n: int) -> tuple[str, str]:
    """发一条长度为 n 的文本。返回 ("ok" | "too_long" | "abort", 说明)。"""
    content = make_content(n)
    payload = {"content": content, "msg_type": 0}  # 文本消息，不带 msg_id = 主动消息
    url = f"{API_BASE}/v2/users/{openid}/messages"
    try:
        r = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return "abort", f"网络错误 {type(exc).__name__}: {exc}"

    body = r.text[:300]
    if r.status_code < 300:
        return "ok", f"HTTP {r.status_code}"

    if any(code in body for code in TOO_LONG_CODES):
        return "too_long", f"HTTP {r.status_code} {body}"
    for code, hint in ABORT_CODES.items():
        if code in body:
            return "abort", f"{code} {hint} —— {body}"
    return "abort", f"HTTP {r.status_code} 未归类的错误 —— {body}"


async def main() -> int:
    args = sys.argv[1:]
    do_send = "--send" in args
    args = [a for a in args if a != "--send"]

    sizes: tuple[int, ...] = DEFAULT_SIZES
    for i, a in enumerate(args):
        if a == "--sizes" and i + 1 < len(args):
            sizes = tuple(int(x) for x in args[i + 1].split(","))
    args = [a for a in args if a != "--sizes" and not all(c.isdigit() or c == "," for c in a)]

    from qq_rss_bot.config import load_config

    cfg = load_config()
    openid = args[0] if args else None
    if openid is None:
        openid = pick_openid_from_db()

    print(f"采点（字符数）：{', '.join(map(str, sizes))}")
    for n in sizes:
        c = make_content(n)
        print(f"  {n:>5} 字符 → 实际 {len(c)} 字符，开头是 {c[:14]!r}")
    print(f"收件人 openid：{openid or '（没找到）'}")

    if not do_send:
        print("\n干跑结束 —— **一个字节都没发**。加 --send 才真发。")
        return 0
    if not openid:
        print("\n❌ 库里没有可用的 openid。先跑 scripts/test_c2c.py --listen 抓一个，"
              "或者把 openid 当参数传进来。")
        return 1

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }

        print()
        biggest_ok = 0
        for n in sizes:
            verdict, detail = await probe_once(client, headers, openid, n)
            mark = {"ok": "✅ 通了", "too_long": "❌ 太长", "abort": "⛔ 中止"}[verdict]
            print(f"  {n:>5} 字符  {mark}  {detail}")
            if verdict == "ok":
                biggest_ok = n
            elif verdict == "too_long":
                print(f"\n结论：上限在 {biggest_ok} ~ {n} 之间（这些都是字符数，中文）。")
                return 0
            else:
                print("\n⛔ 中止 —— 上面那条不是长度问题，结论不可信。")
                return 1
            await asyncio.sleep(2.0)  # 别把私信频控撞上

        print(f"\n结论：测到的最大通过长度是 {biggest_ok} 字符，还没摸到上限。")
        print("       想继续就加更大的采点，例如 --sizes 8000,12000")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
