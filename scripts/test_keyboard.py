"""测「群聊里能不能发带按钮的消息」（keyboard）。

为什么只能真发一条试：官方文档「消息交互」那一页抓不到正文（SPA，URL 回落到
总览页），而 botpy 的 `post_group_message` 签名里**确实**有 `keyboard` 参数、
`types/inline.py` 也有完整结构定义。但「SDK 有这个参数」不等于
「群聊里会渲染」—— 只能真发一条看。

和 test_c2c.py 一样走裸 HTTP：不占机器人的 WS 会话，可以和机器人并行跑。

⚠️ 这是往**真实的群**里发消息，群里的人看得到。

跑法：
    uv run python scripts/test_keyboard.py            # 默认发到库里第一个已知群
    uv run python scripts/test_keyboard.py <group_openid>
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from qq_rss_bot.config import load_config  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

ERROR_HINTS = {
    "54001": "参数错误，看 response 里的 message",
    "54002": "消息体格式不对 —— keyboard 结构可能没被接受",
    "34104": "主动消息频次超限",
    "34005": "机器人没有「主动在群聊内发言」权限",
    "22009": "主动消息额度用完",
    "11244": "机器人没连着 gateway（先确认机器人进程在跑）",
    "50054": "该群不存在或机器人不在群里",
}


def build_keyboard() -> dict:
    """最小的 keyboard。

    ⚠️ **必须包 `content` 这一层**。官方 schema 有两种形式：
        "keyboard": {"content": {"rows": [...]}}   ← 长形式
        "keyboard": {"id": "..."}                  ← 短形式，用平台预置模板
    直接把 rows 挂在 keyboard 底下，**接口照样返回 200**，但按钮被静默丢弃 ——
    和指令面板的 children 是同一类坑：**200 不等于生效**。

    `action.type`：0=跳转（http/小程序）1=回调（要 interaction intent）2=指令。
    这里只用 0 和 2 —— 回调按钮要额外的 intent，指令按钮走现有群消息通道。
    注意 `action.type=2` 是**把 `@机器人 data` 插进输入框**，不是直接发送；
    要「点一下自动发」得配 `enter: true`，而官方注明 **enter 仅单聊可用**。

    `render_data.label` 上限 **10 字符**（中文按 2 算），所以标签要短。
    `click_limit` 官方已标废弃，不传。
    """
    tips = "你的 QQ 版本暂不支持按钮，升级后可见"
    return {
        "content": {                      # ← 就是这一层，漏了按钮就没了
            "rows": [
                {
                    "buttons": [
                        {"id": "btn_ping",
                         "render_data": {"label": "发 /ping", "visited_label": "已插入",
                                         "style": 1},
                         "action": {"type": 2, "permission": {"type": 2},
                                    "data": "/ping", "unsupport_tips": tips}},
                        {"id": "btn_list",
                         "render_data": {"label": "发 /list", "visited_label": "已插入",
                                         "style": 0},
                         "action": {"type": 2, "permission": {"type": 2},
                                    "data": "/list", "unsupport_tips": tips}},
                    ]
                },
                {
                    "buttons": [
                        {"id": "btn_link",
                         "render_data": {"label": "打开文档", "visited_label": "已打开",
                                         "style": 0},
                         "action": {"type": 0, "permission": {"type": 2},
                                    "data": "https://bot.q.qq.com/wiki/",
                                    "unsupport_tips": tips}},
                    ]
                },
            ]
        }
    }


CONTENT = ("**按钮渲染测试**\n"
           "这是 qq-rss-bot 的一次性测试消息。点下面任意一个按钮试试。")


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cfg = load_config()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TOKEN_URL,
                              json={"appId": cfg.appid, "clientSecret": cfg.secret})
        token = r.json().get("access_token") if r.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {r.status_code} {r.text[:200]}")
            return 1
        headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": cfg.appid,
                   "Content-Type": "application/json"}

        if args:
            group = args[0]
        else:
            # 群 openid 不是群号，只能从事件里拿，所以从库里已记录的群取
            conn = sqlite3.connect(cfg.db_path)
            rows = [x[0] for x in conn.execute("SELECT group_openid FROM groups")]
            if not rows:
                print("库里没有已知的群，先让机器人在群里被 @ 一次")
                return 1
            group = rows[0]

        print(f"目标群: {group}")
        body = {
            "msg_type": 2,                       # 2 = markdown
            "markdown": {"content": CONTENT},
            "keyboard": build_keyboard(),
        }
        print("\n发送的 keyboard：")
        print(json.dumps(body["keyboard"], ensure_ascii=False, indent=2))
        print(f"\nPOST {API_BASE}/v2/groups/{group}/messages")
        r = await client.post(f"{API_BASE}/v2/groups/{group}/messages",
                              headers=headers, json=body)
        print(f"\n← HTTP {r.status_code}")
        print(f"← {r.text[:600]}")

        if r.status_code == 200:
            print("\n✅ 接口收下了。**去群里看有没有按钮** —— 200 不等于渲染出来了。")
            print("   有按钮 → 群聊能做多级菜单，绕开面板 20 元素的限制。")
            print("   没按钮 → 只剩命名前缀这一条路。")
            return 0

        try:
            j = r.json()
            code = str(j.get("code") or j.get("err_code") or "")
        except Exception:
            code = ""
        print(f"\n❌ 被拒。错误码 {code}："
              f"{ERROR_HINTS.get(code[-5:], ERROR_HINTS.get(code, '未知'))}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
