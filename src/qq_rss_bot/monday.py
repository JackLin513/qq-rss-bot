"""月曜が近いよ —— 每周定时的视频播报。

原型是 X 上的 `@ChocodateMonday`（8.4 万关注的シャニマス戏仿号）。**那个号
2026-09-27 已经停止活动**，所以这里不做「监控转发」，而是让群里的机器人自己
每周播一次：周日 22:00（**北京时间**）发一条「月曜が近いよ」+ 一个本地视频。

素材放在 `data/monday/`（容器里 `/app/data/monday/`），从里面随机挑。
默认由 `MONDAY_VIDEO_DIR` 指定，留空就跟着 `DB_PATH` 的目录走。

## ⚠️ 视频上限 10 MiB，而且是**体积**不是时长（2026-10-18 实测）

`file_type=2`（视频 mp4）这条路项目里从没跑过，这次用 ffmpeg 造片、
`srv_send_msg=False` **只上传不发消息**（群里全程看不见）测出来的：

| 原始体积 | 结果 |
|---|---|
| 5.8 KB ~ 9.86 MiB | ✅ 拿到 `file_info` |
| **10.11 MiB** | ❌ `400 40093011` |
| 12.96 / 14.35 MiB | ❌ `400 40093011` |
| 19.5 / 26.0 MiB | ❌ `413`（另一个坎：HTTP body 太大） |

* **边界正好在 10 MiB**（9.86 过 / 10.11 不过）。
* **和时长无关**：20 秒 / 5.57 MiB ✅，2.8 秒 / 14.35 MiB ❌ ——
  要压得压码率，压时长没用。
* ⚠️ **官方那句「`file_data` 5M」在视频这条路上不成立**：5.85 MiB、13.8 MiB
  的 base64 都传上去了。5M 那个上限只约束**图片**（`preview.MAX_IMAGE_BYTES`）。
  别把这两个数混成一个。
* `40093011` 和 `413` 是**两个不同的坎**：前者是视频本身被拒（业务错误），
  后者是 HTTP body 太大。视频这条路会先撞 10 MiB 那个。

## ⚠️ 一条播报发**两条消息**，因为视频那条挂不住文案（2026-10-18 实测）

`msg_type=7`（富媒体）的 `content` 字段：

| 带什么 | `content` 渲染吗 |
|---|---|
| 图片（`file_type=1`） | ✅ 渲染 —— 所以「文案 + 表情包」能挤进一条 |
| **视频（`file_type=2`）** | ❌ **客户端整个丢掉** |

视频那条实测：接口回 HTTP 200、`file_info` 也拿到了，群里就是**只有视频、
一个字都没有**。`MediaInfo` 里也只有 `file_info`，没有 caption/title 可挂。

所以拆成两条：**文案（+表情包）一条、视频一条**。顺序是文案在前。

表情包：`data/monday/` 里任何一张图片（按后缀认，`find_sticker`），没有就
只发纯文字。⚠️ 机器人**发不了真·表情包** —— QQ 的表情包是客户端的东西，
bot API 里没有对应的 `msg_type`，发出去的是普通图片。

代价：**一次播报吃两条主动消息配额**。

## 为什么**不需要**预热 / 预览

markdown 多图和抽票封面要预热，是因为那些图是 **QQ 的服务器去下载 URL**，
冷链接第一次渲染必炸。这里的视频是我们自己 base64 传上去的**字节**，
根本不经过「QQ 下载」那一环，所以没有缓存冷启动问题。
（代价是每次都要把整段视频传上去 —— 一周一条、几个群，可以忽略。）

## 时间为什么用**本地时间**，不是 JST

抽票那套用 JST 是因为**截止时刻本身是日本时间**，差一小时就错过。
这里的「周日 22:00」是给人看的播出时间，观众在国内，所以一律用容器本地时间
（`Asia/Shanghai`）。**别把这两套混在一起。**
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Sequence

from . import features

_log = logging.getLogger("qq_rss_bot.monday")

# 实测边界，见模块 docstring 的表。**别按官方文档改成 5 MiB** ——
# 那个 5M 限的是图片那条路。
MAX_VIDEO_BYTES = 10 * 1024 * 1024

# `datetime.weekday()` 的口径：0=周一 … 6=周日。默认周日。
DEFAULT_WEEKDAY = 6
DEFAULT_TIME = "23:30"
# 只容忍**调度抖动**（tick 60 秒，窗口 180 秒保证至少落到一次）。
# **这不是补发** —— 用户明确选了「不补发」：22:05 之后起来的就当这周没播。
DEFAULT_WINDOW_SEC = 180

TEXT = "月曜が近いよ"

# 轮询器的苏醒间隔。比窗口小就行；60 秒既够细又不费什么。
CHECK_SEC = 60

_WD = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def parse_time(raw: str) -> time:
    """`"22:00"` → `time(22, 0)`。写错就退出 —— 和 `_env_int` 同一个风格。"""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise SystemExit(f"MONDAY_TIME 必须是 HH:MM，当前值：{raw!r}") from exc


def list_videos(directory: Path) -> list[Path]:
    """目录里**能发的**视频。

    三条过滤规则，每条都来自实际踩到或实测到的东西：

    1. **跳过 `._` 开头的。** macOS 的 tar / Finder 会带 AppleDouble 元数据
       文件，它们的名字是 `._月曜が近いよ_1.mp4` —— **同样以 `.mp4` 结尾**，
       不排除的话会被当成视频传上去（793 字节的「视频」）。
    2. 跳过 0 字节的（拷一半断了）。
    3. **跳过超过 10 MiB 的**，并且**打 WARNING 说出是哪几个** ——
       静默跳过的话，用户只会看到「这周没播」而不知道原因。
    """
    if not directory.is_dir():
        return []
    out: list[Path] = []
    too_big: list[tuple[str, int]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("._"):
            continue
        if path.suffix.lower() != ".mp4":
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        if size > MAX_VIDEO_BYTES:
            too_big.append((path.name, size))
            continue
        out.append(path)
    if too_big:
        _log.warning(
            "月曜：这些视频超过 %.0f MiB（QQ 会回 400 40093011），发不了：%s",
            MAX_VIDEO_BYTES / 1048576,
            "、".join(f"{name}（{size / 1048576:.2f} MiB）" for name, size in too_big),
        )
    return out


_STICKER_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def find_sticker(directory: Path) -> Path | None:
    """素材目录里的表情包图片，没有就返回 `None`。

    **按后缀认，不要求文件名** —— 和视频放同一个目录（`data/monday/`），
    `.mp4` 归 `list_videos`，图片归这里，互不打扰。所以素材直接叫
    `月曜が近いよ.jpg` 就行，不用另立一套命名规矩。

    **不是必须的**：没有就只发纯文字文案，其余完全不变。

    ⚠️ 机器人**没有「发表情包」的接口** —— QQ 的表情包是客户端的东西，
    bot API 里没有对应的 `msg_type`。这里的图只能当**普通图片**发
    （`msg_type=7` + `file_type=1`），观感上是一条带图的消息，不是表情气泡。
    """
    if not directory.is_dir():
        return None
    found = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file()
        and not path.name.startswith("._")          # AppleDouble，同 list_videos
        and path.suffix.lower() in _STICKER_SUFFIXES
        and path.stat().st_size > 0
    ]
    if not found:
        return None
    if len(found) > 1:
        # 不静默挑一张：多图是配置问题，得让人看见。只会在这周真要发的时候
        # 才走到这里，所以不至于刷屏。
        _log.warning(
            "月曜：%s 里有 %d 张图，只发按名字排第一的 %s（其余：%s）",
            directory,
            len(found),
            found[0].name,
            ", ".join(x.name for x in found[1:]),
        )
    return found[0]


def pick_video(videos: Sequence[Path], last: str | None) -> Path:
    """随机挑一个，**尽量不和上一次重复**。

    只有两个素材时纯随机有一半概率连着两周发同一个，看起来像「随机根本没生效」。
    所以有得选的时候先排除上次那个。**只有一个时照发** —— 不能因为「会重复」
    就一条都不发。
    """
    if not videos:
        raise ValueError("没有可发的视频")
    pool = list(videos)
    if len(pool) > 1 and last:
        candidates = [p for p in pool if p.name != last]
        if candidates:
            pool = candidates
    return random.choice(pool)


def slot_for(
    now: datetime,
    weekday: int = DEFAULT_WEEKDAY,
    at: time | None = None,
) -> datetime:
    """`now` 之前（含）最近的那个计划时刻。

    含 —— 正好 22:00 时返回**今天** 22:00，否则 tick 恰好落在整点那一下
    会算出上周、然后被窗口判掉，白白错过一周。
    """
    at = at or parse_time(DEFAULT_TIME)
    delta = (now.weekday() - weekday) % 7
    slot = datetime.combine(now.date() - timedelta(days=delta), at)
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def render_text(now: datetime | None = None) -> str:
    """播报文案。

    **就是「月曜が近いよ」这一句，不带任何后缀**（2026-10-18 用户改的）。

    原先拼过一个「（あと N 時間）」的尾巴，去掉了 —— 一是用户不要，
    二是那个数**只在 22:00 那一刻才对**：窗口内的重试、或者哪天把
    `MONDAY_TIME` 调到别的点，尾巴就会开始胡说（周一早上发的话是负数）。
    留 `now` 参数只是为了不破坏调用方签名。

    ⚠️ **别在这里加 markdown 语法**（`**` `[]` `#` 之类）—— 这条走的可能是
    `msg_type=0` 纯文本，符号会原样打出来。
    """
    return TEXT


class MondayPoller:
    """每周一次的定时播报。和 `TicketPoller` 同形（`run` / `stop` / `poll_once`）。

    单独一个 task，不塞进 `FeedPoller` —— 数据源、节奏、失败语义都不一样。
    """

    def __init__(self, cfg, db, sender) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        self._stopped = asyncio.Event()
        # 同一个 slot 只警告一次。窗口内会醒好几次，不记的话
        # 「目录空 / 全超限」会每分钟刷一条。
        self._warned_slot: str | None = None
        # (slot, group) → 文案已经发出去了。只为了「视频失败重试时别把文案再发一遍」，
        # 不参与记账（那件事只认 db.monday_posted）。
        self._caption_sent: set[tuple[str, str]] = set()

    @property
    def groups(self) -> tuple[str, ...]:
        """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。

        群开关存在库里（见 `features.py`），`.env` 里**一个群参数都没有**。
        """
        return features.groups_for(self.db, "monday")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.monday_enabled and self.groups)

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        # ⚠️ **这里不能 return。** 群开关在库里（`features.py`），`/set` 的语义是
        # 「改完下一拍就生效」—— 启动时库里一个群都没开的话，一 return 这个 task
        # 就**永久结束**了：之后再怎么 `/set` 开群都不会跑，只能重启容器。
        # 留着轮询不花什么钱 —— `poll_once` 每拍开头 `if not self.enabled: return 0`。
        if not self.enabled:
            _log.info("月曜：库里没有开着的群，功能关闭（**仍然轮询**，/set 开了下一拍生效）")
        else:
            _log.info(
                "月曜：已启用，%s %s（本地时间）播报，推送到 %d 个群，素材目录 %s",
                _WD[self.cfg.monday_weekday % 7],
                self.cfg.monday_time,
                len(self.groups),
                self.cfg.monday_video_dir,
            )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 和抽票一样：出问题只记日志，**不往群里报错**。
                _log.exception("月曜：本轮检查异常，跳过")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=CHECK_SEC)
            except asyncio.TimeoutError:
                pass
        _log.info("月曜：已停止")

    async def poll_once(self, *, now: datetime | None = None) -> int:
        """跑一轮，返回这轮发出去的条数。"""
        if not self.enabled:
            return 0

        now = now or datetime.now()
        slot = slot_for(now, self.cfg.monday_weekday, parse_time(self.cfg.monday_time))
        # **不补发**：窗口只覆盖调度抖动。晚于窗口就当天没播，直接返回 ——
        # 用户明确不要「停机起来补一条」的行为。
        if (now - slot).total_seconds() > self.cfg.monday_window_sec:
            return 0

        slot_key = slot.strftime("%Y-%m-%dT%H:%M")
        done = self.db.monday_posted(slot_key)
        targets = [g for g in self.groups if g not in done]
        if not targets:
            return 0

        videos = list_videos(self.cfg.monday_video_dir)
        if not videos:
            if self._warned_slot != slot_key:
                self._warned_slot = slot_key
                _log.warning(
                    "月曜：%s 里没有能发的视频（≤ %.0f MiB 的 mp4），这一周跳过",
                    self.cfg.monday_video_dir,
                    MAX_VIDEO_BYTES / 1048576,
                )
            return 0

        video = pick_video(videos, self.db.monday_last_video())
        sticker = find_sticker(self.cfg.monday_video_dir)
        text = render_text(now)
        sent = 0
        for group in targets:
            # 逐群隔离：一个群发不出去（配额、权限、接口抖动）不能连累别的群。
            # 失败**不记账**，窗口内下一次 tick 会重试；窗口过了就当这周没发。

            # **两条消息**：文案（+表情包）一条、视频一条。不是想拆 ——
            # `msg_type=7` 带视频时 `content` 客户端整个丢掉（实测），
            # `MediaInfo` 里也没有 caption 字段可挂。详见模块说明。
            #
            # 顺序是文案在前、视频在后。代价是「文案发成了、视频挂了」时，
            # 重试会把文案再发一遍 —— `_caption_sent` 挡掉这种情况（只挡本进程内
            # 的重试；重启后窗口只剩几十秒，真撞上也就是多一条一样的文案）。
            if (slot_key, group) not in self._caption_sent:
                try:
                    if sticker:
                        await self.sender.send_local_image(group, text, sticker)
                    else:
                        await self.sender.send_text(group, text)
                    self._caption_sent.add((slot_key, group))
                except Exception as exc:  # noqa: BLE001
                    # 文案发不出去不连累视频 —— 视频才是这条播报的主体。
                    _log.warning("月曜：文案发送失败 → %s: %s", group[:8], exc)

            try:
                await self.sender.send_video(group, video)
            except Exception as exc:  # noqa: BLE001
                _log.warning("月曜：视频发送失败 → %s: %s", group[:8], exc)
                continue
            self.db.mark_monday_posted(slot_key, group, video.name)
            sent += 1
            _log.info(
                "月曜：已推送「%s」+%s → %s（%s）",
                text,
                sticker.name if sticker else "纯文字",
                group[:8],
                video.name,
            )
        return sent
