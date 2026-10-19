"""「月曜が近いよ」每周播报的探针。

回答四件事，**默认一条消息都不发**：

1. 素材目录里有什么 —— 每个文件多大、会不会被过滤掉（`._` / 超 10 MiB / 空文件）；
2. 现在的 slot、下一个 slot、现在在不在播报窗口里；
3. 上一次发的是哪个文件（避免连着两周重复）；
4. 真发的话会发什么（文案 + 表情包 + 选中的视频）。

真发是**两条消息**：文案（+表情包）一条、视频一条 —— 因为 `msg_type=7` 带
视频时 `content` 客户端不渲染，理由见 `monday.py` 的模块说明。

跑法：

    uv run python scripts/probe_monday.py
    uv run python scripts/probe_monday.py --now 2026-10-18T22:00   # 假装成某个时刻
    uv run python scripts/probe_monday.py --group <openid> --send   # 才真发

⚠️ **只允许发到机器人测试群（群号 575861535）或私聊用户本人。绝不发正式群。**
所以 `--group` **没有默认值** —— 群号查不出 openid（库里 `groups` 表存的是
openid），只能你自己事先确认好是哪个群。
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

from qq_rss_bot import monday  # noqa: E402
from qq_rss_bot.config import load_config  # noqa: E402
from qq_rss_bot.db import Database  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_RICH = 7
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2


def _arg(name: str, default: str | None = None) -> str | None:
    args = sys.argv[1:]
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now()
    return datetime.fromisoformat(raw)


def _list_all(directory: Path) -> None:
    """把目录里的**每个**文件都列出来，包括会被过滤掉的 —— 这样「为什么没发」
    一眼就能看出来。只看 `list_videos()` 的返回值会让过滤变成黑盒。"""
    if not directory.is_dir():
        print(f"  ⚠️ 目录不存在：{directory}")
        return
    kept = {p.name for p in monday.list_videos(directory)}
    sticker = monday.find_sticker(directory)
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            print(f"  ? {path.name}（读不到大小）")
            continue
        if path.name in kept:
            mark = "✅ 会发（视频）"
        elif sticker is not None and path.name == sticker.name:
            mark = "✅ 会发（表情包）"
        elif path.suffix.lower() in monday._STICKER_SUFFIXES:
            mark = "⏭  跳过：图（本目录只发排第一张）"
        elif path.name.startswith("._"):
            mark = "⏭  跳过：`._` 元数据（AppleDouble）"
        elif path.suffix.lower() != ".mp4":
            mark = "⏭  跳过：不是 .mp4"
        elif size <= 0:
            mark = "⏭  跳过：0 字节"
        elif size > monday.MAX_VIDEO_BYTES:
            mark = f"❌ 跳过：超过 {monday.MAX_VIDEO_BYTES / 1048576:.0f} MiB"
        else:
            mark = "❓ 未知原因"
        print(f"  {mark:<44} {size / 1048576:8.2f} MiB  {path.name}")


async def _send(
    group: str, text: str, video: Path, sticker: Path | None, cfg
) -> int:
    """发「文案（+表情包）」一条、「视频」一条。

    ⚠️ **为什么必须拆两条**（2026-10-18 实测）：`msg_type=7` 带**图片**时
    `content` 是渲染的，带**视频**时**客户端整个丢掉** —— 接口回 HTTP 200、
    `file_info` 也拿到了，群里就是只有视频、一个字都没有。`MediaInfo` 里也
    只有 `file_info`，没有 caption/title 可挂。

    所以：文案跟**图**走（`msg_type=7` + `file_type=1`），视频单独发
    （`msg_type=7` + `file_type=2` + `content=""`）。没有表情包时文案退回
    `msg_type=0` 纯文本。
    """
    raw = video.read_bytes()
    if len(raw) > monday.MAX_VIDEO_BYTES:
        print(f"  ❌ {video.name} 超过 10 MiB，不发")
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

        # ---- 第一条：文案（有表情包就跟着一起发）----
        if sticker is not None:
            img = base64.b64encode(sticker.read_bytes()).decode()
            iu = await client.post(
                f"{API_BASE}/v2/groups/{group}/files",
                headers=headers,
                json={"file_type": 1, "file_data": img, "srv_send_msg": False},
            )
            try:
                iu_data = iu.json() or {}
            except ValueError:
                iu_data = {}
            img_info = iu_data.get("file_info")
            print(f"  传图 ← HTTP {iu.status_code} file_info={'有' if img_info else '无'}"
                  f"（{sticker.name}）")
            if not img_info:
                print(f"     {str(iu_data)[:300]}")
                return 1
            t = await client.post(
                f"{API_BASE}/v2/groups/{group}/messages",
                headers=headers,
                json={
                    "msg_type": MSG_TYPE_RICH,
                    "content": text,          # 图片这条路 content 是渲染的
                    "media": {"file_info": img_info},
                },
            )
        else:
            t = await client.post(
                f"{API_BASE}/v2/groups/{group}/messages",
                headers=headers,
                json={"msg_type": MSG_TYPE_TEXT, "content": text},
            )
        try:
            t_body = t.json() or {}
        except ValueError:
            t_body = {}
        print(f"  文案 ← HTTP {t.status_code}  {str(t_body)[:200]}")
        if t.status_code >= 300 or t_body.get("code"):
            print("  ❌ 文案没发出去，视频也不发了")
            return 1

        # ---- 第二条：视频 ----
        up = await client.post(
            f"{API_BASE}/v2/groups/{group}/files",
            headers=headers,
            json={
                "file_type": 2,  # 视频 mp4
                "file_data": base64.b64encode(raw).decode(),
                "srv_send_msg": False,
            },
        )
        try:
            up_data = up.json() or {}
        except ValueError:
            up_data = {}
        file_info = up_data.get("file_info")
        print(f"  上传 ← HTTP {up.status_code} file_info={'有' if file_info else '无'}")
        if not file_info:
            print(f"     {str(up_data)[:300]}")
            return 1

        r = await client.post(
            f"{API_BASE}/v2/groups/{group}/messages",
            headers=headers,
            json={
                "msg_type": MSG_TYPE_RICH,
                # 视频这条正文留空：客户端不渲染，塞了也只是自欺欺人。
                "content": "",
                "media": {"file_info": file_info},
            },
        )
        try:
            body = r.json() or {}
        except ValueError:
            body = {}
        print(f"  视频 ← HTTP {r.status_code}  {str(body)[:300]}")
        ok = r.status_code < 300 and not body.get("code")
        print("  ✅ 发出去了，去群里看：文案+表情包一条、视频一条" if ok else "  ❌ 失败")
        return 0 if ok else 1


async def main() -> int:
    args = sys.argv[1:]
    cfg = load_config()
    now = _parse_now(_arg("--now"))
    group = _arg("--group")
    do_send = "--send" in args
    directory = Path(_arg("--dir", str(cfg.monday_video_dir)) or cfg.monday_video_dir)

    if do_send and not group:
        print("--send 必须配 --group <group_openid>（没有默认值，见文件头的警告）")
        return 2

    print(f"配置 : {'开' if cfg.monday_enabled else '关'}"
          f"（MONDAY_ENABLED） · 群 {len(cfg.monday_groups)} 个")
    if not cfg.monday_groups:
        print("       ⚠️ MONDAY_GROUPS 为空 = 线上功能是关的；下面的探针照常能跑")
    print(f"时段 : {monday._WD[cfg.monday_weekday % 7]} {cfg.monday_time}"
          "（本地时间）· 窗口 "
          f"{cfg.monday_window_sec}s")
    print(f"素材 : {directory}")
    print(f"时刻 : {now.isoformat()}（{monday._WD[now.weekday()]}）")

    print("\n目录内容：")
    _list_all(directory)

    videos = monday.list_videos(directory)
    print(f"\n可发 {len(videos)} 个：{[p.name for p in videos]}")

    at = monday.parse_time(cfg.monday_time)
    slot = monday.slot_for(now, cfg.monday_weekday, at)
    in_window = (now - slot).total_seconds() <= cfg.monday_window_sec
    print(f"\n最近 slot : {slot.isoformat(timespec='minutes')}"
          f"（{int((now - slot).total_seconds())} 秒前）")
    print(f"在窗口内  : {'是 ✅ 此刻会发' if in_window else '否 ⏭  不会发（不补发）'}")
    nxt = slot + timedelta(days=7)
    print(f"下一个 slot: {nxt.isoformat(timespec='minutes')}"
          f"（还有 {timedelta(seconds=int((nxt - now).total_seconds()))}）")

    # 上次发的是哪个 + 下次会挑哪个
    db = Database(cfg.db_path)
    try:
        last = db.monday_last_video()
        print(f"上次发的  : {last or '（没有记录）'}")
        posted = db.monday_posted(slot.strftime("%Y-%m-%dT%H:%M"))
        print(f"这个 slot 已发的群: {len(posted)} 个")
        if videos:
            # 真实运行时是随机的，这里把**每个候选**都打出来，好判断去重有没有生效
            print("\n候选（按去重规则）：")
            for path in videos:
                mark = "（上周发过，有别的可选时会避开）" if path.name == last and len(videos) > 1 else ""
                print(f"  · {path.name}{mark}")
            sticker = monday.find_sticker(directory)
            print(f"\n表情包：{sticker.name if sticker else '（没有，文案退化成纯文字）'}")
            print(f"文案：{monday.render_text(now)}")
    finally:
        db.close()

    if do_send:
        if not videos:
            print("\n没有可发的视频，不发。")
            return 1
        video = monday.pick_video(videos, last)
        text = monday.render_text(now)
        sticker = monday.find_sticker(directory)
        print("\n" + "=" * 70)
        print(f"⚠️  真发到群 {group[:8]}…，两条消息：")
        print(f"   1) 文案「{text}」"
              + (f" + 表情包 {sticker.name}" if sticker else "（无表情包）"))
        print(f"   2) 视频 {video.name}（{video.stat().st_size:,} 字节）")
        print("-" * 70)
        return await _send(group, text, video, sticker, cfg)

    print("\n（默认只打印。要真发：--group <openid> --send）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
