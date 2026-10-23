"""测试「撤回群消息」到底能不能撤 —— **只删测试群里的指定消息**。

用来回答一个只有线上能回答的问题：**机器人是不是群管理员、能不能撤回
群主（你自己）发的消息**。官方文档只写了「管理员可以撤回**普通群成员**的
消息」，群主不在明确范围内。

    # 1. 在测试群发一条消息（随便什么，最好就是一条链接）
    # 2. 从日志里拿消息 id（分享会被记成「…（消息 <id>）」）
    uv run python scripts/probe_recall.py --group <openid> --msg-id <id>

⚠️ **这是删除操作，不可逆。** 群 openid 没有默认值，正式群尾号 `E0629`
写死拒发。消息超过 2 分钟会失败（`40064004`），机器人不是管理员会失败
（`40062003`）—— 两种都会把接口原始返回打出来。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# 正式群尾号。**写死拒发** —— 撤回不可逆。
FORBIDDEN_TAIL = "E0629"


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return default


async def main() -> int:
    group = _arg("--group")
    msg_id = _arg("--msg-id")
    if not group or not msg_id:
        print("❌ 必须给 --group <openid> 和 --msg-id <消息 id>")
        print("   消息 id 在日志里：`群 …：认出 twitter 分享 …（消息 <id>）`")
        return 2
    if group.upper().endswith(FORBIDDEN_TAIL):
        print("❌ 这是正式群，探针拒绝操作。")
        return 2

    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret})
        tok = r.json().get("access_token") if r.status_code == 200 else None
        if not tok:
            print(f"❌ 取 token 失败：HTTP {r.status_code}")
            return 1
        headers = {
            "Authorization": f"QQBot {tok}",
            "X-Union-Appid": cfg.appid,
        }
        url = f"{API_BASE}/v2/groups/{group}/messages/{msg_id}"
        print(f"DELETE {url}")
        resp = await client.delete(url, headers=headers)
        body = resp.text[:500]
        print(f"→ HTTP {resp.status_code}  {body or '（空）'}")

    if resp.status_code == 200:
        print("✅ 撤回成功 —— 机器人有权限撤这条消息。")
        return 0
    if "40062003" in body or "无操作权限" in body:
        print("❌ 无操作权限：机器人不是群管理员，或这条消息不在可撤范围内。")
    elif "40064004" in body or "撤回时限" in body:
        print("❌ 超过 2 分钟撤回时限 —— 换个刚发的消息再试。")
    else:
        print("❌ 撤回失败，原因看上面的原始返回。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
