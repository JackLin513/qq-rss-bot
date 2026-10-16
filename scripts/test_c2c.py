"""私聊（C2C）主动消息连通性测试。

**为什么要单独测**：出错通知全部走私聊，而主动 C2C 消息是这条链路上唯一
没法离线验证的环节 —— 群消息里的 `member_openid` 能不能直接用来给
「从没和机器人私聊过」的成员发主动消息，官方文档没写清楚。
这个脚本用裸 HTTP 打官方接口，发的是**不带 msg_id 的主动消息**，
也就是 `notify.C2CNotifier` 真正走的那条路（带 msg_id 的被动回复不算数，
那个只要对方先说话就一定能成）。

它不占机器人的 WebSocket 会话，所以**可以在机器人运行时并行执行**。

跑法：
    uv run python scripts/test_c2c.py                  # 用库里登记的管理员 openid
    uv run python scripts/test_c2c.py <openid>         # 指定 openid
    uv run python scripts/test_c2c.py --listen [--wait 90]  # 自己连网关抓一条消息取 openid
                                                       # （--listen 需要先停掉机器人，
                                                       #   同一个 appid 两个连接会打架）
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_TEXT = 0

# 官方错误码 → 人话。没列到的照样原样打印，别猜。
ERROR_HINTS = {
    "11244": "主动消息频次超限（该用户的 C2C 额度用完了）",
    "22009": "主动消息频次超限",
    "304003": "该用户不存在或不可达",
    "306005": "没有权限给该用户发消息（多半是没建立会话）",
    "50054": "机器人不在线：主动消息要求连着 gateway",
}


def pick_openid_from_db() -> tuple[str | None, str | None]:
    """从 notify_targets 里挑一个（最近登记的），返回 (openid, group_openid)。"""
    db = ROOT / os.environ.get("DB_PATH", "data/bot.db")
    if not db.exists():
        return None, None
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT group_openid, member_openid FROM notify_targets"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None, None
    finally:
        conn.close()
    return (row["member_openid"], row["group_openid"]) if row else (None, None)


async def listen_for_openid(appid: str, secret: str, wait_sec: float) -> tuple[str | None, str]:
    """临时连一次网关，抓第一条消息里的 openid。

    **需要先停掉机器人** —— 同一个 appid 起两个 WebSocket 连接会互相踢。
    """
    import botpy

    # ★ 必须装协议补丁，否则 QQ 2026 推的 GROUP_MESSAGE_CREATE **没有解析器**，
    # 事件会被 botpy 静默丢弃 —— 表现就是「明明 @ 了，监听端什么都没收到」。
    # 主程序在 __main__.py 里装，这个脚本要自己装。
    from qq_rss_bot.compat import install_new_protocol_support

    patched = install_new_protocol_support()
    print(f"  协议补丁: {patched or '已装（幂等）'}")

    found: dict[str, str] = {}
    kind = {"what": ""}

    class Probe(botpy.Client):
        async def _grab(self, openid: str | None, what: str) -> None:
            if not openid or found:
                return
            found["openid"] = openid
            kind["what"] = what
            await self.close()

        async def on_group_at_message_create(self, message) -> None:
            await self._grab(
                getattr(message.author, "member_openid", None), "群里 @ 机器人"
            )

        async def on_group_message_create(self, message) -> None:
            await self._grab(
                getattr(message.author, "member_openid", None), "群消息（新协议）"
            )

        async def on_c2c_message_create(self, message) -> None:
            await self._grab(getattr(message.author, "user_openid", None), "私聊消息")

    probe = Probe(intents=botpy.Intents(public_messages=True), timeout=30)
    print(f"  监听中，最多等 {wait_sec:.0f} 秒 —— 请在群里 @ 一次机器人，或私聊它一句…")
    task = asyncio.create_task(probe.start(appid=appid, secret=secret))
    deadline = time.monotonic() + wait_sec
    while time.monotonic() < deadline and not found:
        await asyncio.sleep(0.3)
    if not task.done():
        try:
            await probe.close()
        except Exception:  # noqa: BLE001
            pass
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    return found.get("openid"), kind["what"]


async def send_active(openid: str, content: str, appid: str, secret: str) -> int:
    """发一条**主动** C2C 消息（不带 msg_id），打印原始响应。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret})
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"  取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        print(f"  access_token 获取成功（{len(token)} 字符）")

        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": appid,
            "Content-Type": "application/json",
        }
        # 不带 msg_id / msg_seq —— 这就是「主动消息」，
        # 和 rss 推送、出错通知走的是同一条路
        payload = {"content": content, "msg_type": MSG_TYPE_TEXT}
        url = f"{API_BASE}/v2/users/{openid}/messages"
        print(f"  POST {url}")
        print(f"  body {payload}")

        r = await client.post(url, headers=headers, json=payload)
        print(f"\n  ← HTTP {r.status_code}")
        body = r.text[:600]
        print(f"  ← {body}")

        if r.status_code < 300:
            print("\n  ✅ 主动 C2C 发送成功 —— 去私聊里看有没有收到那条测试消息。")
            print("     （收到了就说明「群里的 member_openid 可以直接用来发主动私聊」，")
            print("       出错通知这条路是通的。）")
            return 0

        # 失败时把错误码翻成人话
        print("\n  ❌ 发送失败。")
        for code, hint in ERROR_HINTS.items():
            if code in body:
                print(f"     错误码 {code}：{hint}")
        if "50054" in body:
            print("     → 机器人没连着 gateway，先把机器人跑起来再试。")
        elif "11244" in body or "22009" in body:
            print("     → 只是额度问题，通道本身是通的。")
        return 1


async def main() -> int:
    args = sys.argv[1:]
    do_listen = "--listen" in args
    args = [a for a in args if a != "--listen"]
    wait_sec = 90.0
    for i, a in enumerate(args):
        if a == "--wait" and i + 1 < len(args):
            wait_sec = float(args[i + 1])
    args = [a for a in args if a not in ("--wait",) and not a.replace(".", "").isdigit()]

    from qq_rss_bot.config import load_config

    cfg = load_config()
    appid = cfg.appid
    secret = cfg.secret

    openid = args[0] if args else None
    source = "命令行参数"
    group = None
    if not openid and not do_listen:
        # 和通知器用**同一个**收件人来源，否则配了白名单之后这里会去读那张空表
        if cfg.notify_openids:
            openid = cfg.notify_openids[0]
            source = "NOTIFY_OPENIDS 白名单"
            if len(cfg.notify_openids) > 1:
                print(f"  白名单有 {len(cfg.notify_openids)} 人，只测第一个")
        else:
            openid, group = pick_openid_from_db()
            source = "数据库 notify_targets"

    if do_listen and not openid:
        print("[1] 连网关抓 openid")
        openid, what = await listen_for_openid(appid, secret, wait_sec=wait_sec)
        source = f"网关监听（{what}）"
        if not openid:
            print(f"  ✗ {wait_sec:.0f} 秒内没收到任何消息。")
            return 1
        print(f"  ✓ 抓到 openid: {openid}")

    if not openid:
        print("找不到 openid。四个办法：")
        print("  1) 在 .env 里配 NOTIFY_OPENIDS=<你的openid>（推荐，通知也只发给你）")
        print("  2) 让机器人跑起来，在群里发一次 /ping（会登记你的 openid），再跑本脚本")
        print("  3) uv run python scripts/test_c2c.py <openid>")
        print("  4) uv run python scripts/test_c2c.py --listen （需先停掉机器人）")
        return 1

    print(f"\n目标 openid: {openid}")
    print(f"来源: {source}" + (f"（群 {group[:12]}…）" if group else ""))
    print()
    print("[2] 发送主动私聊消息")
    return await send_active(
        openid,
        "【测试】这是 qq-rss-bot 的主动私聊消息。收到就说明出错通知这条路通了。",
        appid,
        secret,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
