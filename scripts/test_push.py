"""主动推送连通性测试（文字 / 图片）。

RSS 机器人核心能力是「没人说话时主动推一条」，依赖群里的
「机器人主动在群聊内发言」开关。这个脚本用裸 HTTP 打官方接口，
不占用机器人的 WebSocket 会话，可在机器人运行时并行执行。

官方文档要求发送时机器人必须在线连着 gateway，所以先确认机器人在跑。

跑法：
    uv run python scripts/test_push.py                          # 推一条文字
    uv run python scripts/test_push.py --route /idolmaster/news  # 从路由取首图推图片
    uv run python scripts/test_push.py --image <图片URL>         # 推指定图片
    uv run python scripts/test_push.py --route /xx <group_openid>  # 指定群

默认群取数据库里最近活跃的那个。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
FILE_TYPE_IMAGE = 1
MSG_TYPE_TEXT = 0
MSG_TYPE_RICH = 7  # 富媒体（图文）

# 图片 URL 可能把扩展名藏在 query 里（idolmaster 那个源就是），所以连着 query 一起抓
_IMG_RE = re.compile(
    r"""https?://[^\s"'<>()]+?\.(?:jpe?g|png|gif|webp)(?:\?[^\s"'<>()]*)?""", re.I
)


def pick_group_openid() -> str | None:
    db = ROOT / os.environ.get("DB_PATH", "data/bot.db")
    if not db.exists():
        return None
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT group_openid FROM groups ORDER BY last_seen_at DESC"
    ).fetchone()
    return row["group_openid"] if row else None


async def first_image_from_route(client: httpx.AsyncClient, route: str) -> str | None:
    """从 RSSHub 路由的第一条目里抠出第一张图。"""
    from qq_rss_bot.config import load_config

    base = load_config().rsshub_base.rstrip("/")
    url = route if route.startswith("http") else f"{base}/{route.lstrip('/')}"
    resp = await client.get(url, timeout=40, follow_redirects=True)
    if resp.status_code != 200:
        print(f"  取源失败: HTTP {resp.status_code}")
        return None
    # description 里的 HTML 是转义过的，先解一层再找图
    body = (
        resp.text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )
    found = _IMG_RE.findall(body)
    print(f"  源里共找到 {len(found)} 个图片 URL")
    return found[0] if found else None


def parse_args(argv: list[str]) -> tuple[str, str | None, str | None]:
    """返回 (模式, 目标, 群号)。模式为 text / route / url。"""
    mode, target, group = "text", None, None
    i = 0
    while i < len(argv):
        if argv[i] == "--route" and i + 1 < len(argv):
            mode, target, i = "route", argv[i + 1], i + 2
        elif argv[i] == "--image" and i + 1 < len(argv):
            mode, target, i = "url", argv[i + 1], i + 2
        else:
            group, i = argv[i], i + 1
    return mode, target, group


async def main() -> int:
    mode, target, group_arg = parse_args(sys.argv[1:])

    from qq_rss_bot.config import load_config

    cfg = load_config()
    group_openid = group_arg or pick_group_openid()
    if not group_openid:
        print("找不到 group_openid：先在群里 @ 一次机器人。")
        return 1
    print(f"目标群: {group_openid}")

    async with httpx.AsyncClient(timeout=40) as client:
        image = target
        if mode == "route":
            print(f"从路由取图: {target}")
            image = await first_image_from_route(client, target or "")
            if not image:
                print("没抠到图片 URL")
                return 1
        if image:
            print(f"图片: {image[:110]}")

        # 1. 取 access_token
        resp = await client.post(
            TOKEN_URL, json={"appId": cfg.appid, "clientSecret": cfg.secret}
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            print(f"取 token 失败: HTTP {resp.status_code} {resp.text[:200]}")
            return 1
        print(f"access_token 获取成功（{len(token)} 字符）")
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": cfg.appid,
            "Content-Type": "application/json",
        }
        send_url = f"{API_BASE}/v2/groups/{group_openid}/messages"

        if not image:
            resp = await client.post(
                send_url,
                headers=headers,
                json={
                    "content": "【主动推送测试】看到这条说明 RSS 定时推送可用。",
                    "msg_type": MSG_TYPE_TEXT,
                },
            )
            print(f"文字推送 HTTP {resp.status_code}: {resp.text[:300]}")
            return 0 if resp.status_code == 200 else 1

        # 2. 上传图片：srv_send_msg=False 只上传，拿 file_info，不单独发一条
        resp = await client.post(
            f"{API_BASE}/v2/groups/{group_openid}/files",
            headers=headers,
            json={"file_type": FILE_TYPE_IMAGE, "url": image, "srv_send_msg": False},
        )
        print(f"上传图片 HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code != 200:
            print("\n❌ 图片上传失败。可能是 QQ 服务器拉不到该图，或格式/大小不合规。")
            return 1
        media = resp.json()
        file_info = media.get("file_info")
        print(f"  拿到 file_info（ttl={media.get('ttl')}s）")

        # 3. 图文一起发：msg_type=7 + media
        resp = await client.post(
            send_url,
            headers=headers,
            json={
                "msg_type": MSG_TYPE_RICH,
                "content": "【图片推送测试】图文消息",
                "media": {"file_info": file_info},
            },
        )
        print(f"图文推送 HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            print("\n✅ 图文消息发送成功 —— 机器人可以推图片")
            return 0
        print("\n❌ 图文消息失败")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
