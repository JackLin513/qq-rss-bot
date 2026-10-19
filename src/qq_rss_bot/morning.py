"""小偶像的每日早安语音 —— 每天早上 07:30（**北京时间**）一条。

随机挑一位偶像，用她的声线说一句带她口癖的日语早上好。语音**离线预生成**
（`scripts/make_morning.py`），运行时只做「扫目录 → 读字节 → base64 → 上传」，
**零 LLM、零 TTS、零新依赖**。详见实现计划 `woolly-exploring-lighthouse.md`。

## 素材目录

放到 `data/morning/audio/`（容器里 `/app/data/morning/audio/`），文件名
**`<偶像 id>_<序号>.silk`**：

    data/morning/audio/mano_0.silk      ← 櫻木真乃的第 0 条
    data/morning/audio/mano_1.silk
    data/morning/audio/hiori_0.silk

**运行时只认文件名，不读 `lines.toml`、不读 manifest、不读读音表。**
这样少一个「索引说有、文件没同步过去」的脏状态；代价是日志里只有 `idol_id`
和文件名，没有台词原文 —— 够排查了。

## ⚠️ silk 魔数闸是这条路上**唯一的把关**

2026-10-19 实测：QQ 的 `file_type=3`（语音）上传接口**完全不校验格式** ——
mp3 / wav / m4a、甚至 `\\x02#!SILK_V3` 后面接两千个零字节的**纯垃圾**，
全都 HTTP 200 拿到 `file_info`。

所以「上传成功」在这条路上**一点信息量都没有**，唯一的防线就是这里：
`list_voices` 跳过魔数不对的文件，`bot.send_voice` 再挡一道。不挡的话，
群里会出现一个点不开的语音气泡，而且**从日志上完全看不出来**。

（视频那条路至少还有个 10 MiB 的硬边界，语音这条连格式都不挡。）

## 为什么**不补发**

窗口 180 秒只覆盖调度抖动。07:33 之后起来的就当今天没发 ——
九点钟补一条「早上好」比不补更怪。这和 `monday.py` 同一个取舍。

## 时间用本地时间（`Asia/Shanghai`）

不涉及日本时间。别照抄`ticket`那套 JST 换算。

## 版权

`data/morning/` 整个是**不提交**的：`source/` 是从 [Sallyn0225/eidolon-sc]
（AGPL-3.0）抓的第三方资料，音色是社区上传到 Fish Audio 的公开模型，
角色版权属于原作方。个人粉丝群自用，和那个仓库是同一个姿态。

[Sallyn0225/eidolon-sc]: https://github.com/Sallyn0225/eidolon-sc
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Sequence

_log = logging.getLogger("qq_rss_bot.morning")

# silk 的文件头。QQ 语音要的就是这个格式，**这条路唯一的格式校验点**。
SILK_MAGIC = b"\x02#!SILK_V3"

DEFAULT_TIME = "07:30"
# 只容忍**调度抖动**（tick 60 秒，窗口 180 秒保证至少落到一次）。
# **不是补发** —— 用户明确不要「停机起来补一条」。
DEFAULT_WINDOW_SEC = 180
# 轮询器的苏醒间隔。比窗口小就行。
CHECK_SEC = 60
# 多久之内不重复同一位偶像。28 位轮一圈约一个月，7 天足够躲开「眼花」。
DEFAULT_RECENT_DAYS = 7


def parse_time(raw: str) -> time:
    """`"07:30"` → `time(7, 30)`。写错就退出 —— 和 `monday.parse_time` 同款。"""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise SystemExit(f"MORNING_TIME 必须是 HH:MM，当前值：{raw!r}") from exc


def scan_voices(directory: Path) -> tuple[dict[str, list[Path]], list[tuple[str, str]]]:
    """扫素材目录 → `({偶像 id: [路径…]}, [(被跳过的文件名, 原因)…])`。

    文件名必须是 **`<偶像 id>_<序号>.silk`**。跳过的原因一并返回，
    好让 `probe_morning.py` 直接打给人看 —— 静默跳过是排查不了问题的。

    四条过滤规则 + 一条报告义务，每条都对应一个真踩到或实测到的东西：

    1. **跳过 `._` 开头的。** macOS 的 tar / Finder 会带 AppleDouble 元数据文件，
       它们的名字是 `._mano_0.silk` —— **同样以 `.silk` 结尾**，不排除的话会被
       当成语音上传（几十字节的「语音」）。`monday.list_videos` 吃过同一个亏。
    2. 跳过 0 字节的（拷一半断了）。
    3. 跳过**魔数不对**的 —— 见模块说明，这是这条路上唯一的把关。
    4. 跳过文件名不合 `<id>_<序号>` 的（序号必须是数字；`rpartition` 而不是
       `split`，因为偶像 id 里可能有下划线）。
    5. 上面跳掉的都**打 WARNING 说是哪几个** —— 静默跳过的话，用户只会看到
       「今天没发」而不知道为什么。
    """
    out: dict[str, list[Path]] = {}
    skipped: list[tuple[str, str]] = []
    if not directory.is_dir():
        return out, skipped
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("._"):
            continue
        if path.suffix.lower() != ".silk":
            continue
        idol, sep, num = path.stem.rpartition("_")
        if not sep or not idol or not num.isdigit():
            skipped.append((path.name, "文件名不是 <偶像id>_<序号>.silk"))
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            skipped.append((path.name, f"读不了：{exc}"))
            continue
        if not raw:
            skipped.append((path.name, "0 字节"))
            continue
        if raw[:10] != SILK_MAGIC:
            # ⚠️ 这条最重要：上传接口对格式**完全不校验**，只有这里能挡。
            skipped.append((path.name, f"不是 silk（头 {raw[:10]!r}）"))
            continue
        out.setdefault(idol, []).append(path)
    return out, skipped


def list_voices(directory: Path) -> dict[str, list[Path]]:
    """`scan_voices` 的常用形态：只要能发的，跳过的一律打 WARNING。

    跳过的**一定要打出来** —— 静默跳过的话，用户只会看到「今天没发」
    而不知道是文件名写错了、还是文件根本没同步过去。
    """
    out, skipped = scan_voices(directory)
    if skipped:
        _log.warning(
            "早安：%s 里跳过了 %d 个文件：%s",
            directory,
            len(skipped),
            "；".join(f"{name}（{why}）" for name, why in skipped),
        )
    return out


def pick_idol(voices: dict[str, list[Path]], recent: set[str]) -> str:
    """随机挑一位，**尽量避开最近发过的**。

    **全都在 `recent` 里时照挑** —— 不能因为「会重复」就一条都不发
    （和 `monday.pick_video` 同一个取舍）。
    """
    if not voices:
        raise ValueError("没有可发的偶像")
    pool = [i for i in sorted(voices) if i not in recent] or sorted(voices)
    return random.choice(pool)


def pick_line(lines: Sequence[Path], last: str | None) -> Path:
    """随机挑一条台词，**尽量不和上一次重复**。

    同一位偶像的两天撞上同一条台词看起来像「随机根本没生效」。
    **只有一条时照发。**
    """
    if not lines:
        raise ValueError("这位偶像没有可发的语音")
    pool = list(lines)
    if len(pool) > 1 and last:
        candidates = [p for p in pool if p.name != last]
        if candidates:
            pool = candidates
    return random.choice(pool)


def slot_for(now: datetime, at: time | None = None) -> datetime:
    """`now` 之前（含）最近的那个计划时刻。**每天一次，不是每周。**

    含 —— 正好 07:30 时返回**今天** 07:30，否则 tick 恰好落在整点那一下
    会算出昨天、然后被窗口判掉，白白错过一天。
    （`monday.slot_for` 是同一套逻辑，只是那里按星期回退 7 天。）
    """
    at = at or parse_time(DEFAULT_TIME)
    slot = datetime.combine(now.date(), at)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


class MorningPoller:
    """每天一次的早安语音。和 `MondayPoller` / `TicketPoller` 同形。

    单独一个 task，不塞进 `FeedPoller` —— 数据源、节奏、失败语义都不一样。
    """

    def __init__(self, cfg, db, sender) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        self._stopped = asyncio.Event()
        # 同一个 slot 只警告一次。窗口内会醒好几次，不记的话
        # 「目录空 / 全是坏文件」会每分钟刷一条。
        self._warned_slot: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.morning_enabled and self.cfg.morning_groups)

    @property
    def audio_dir(self) -> Path:
        return Path(self.cfg.morning_dir) / "audio"

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        if not self.enabled:
            _log.info("早安：未配置 MORNING_GROUPS，功能关闭")
            return
        _log.info(
            "早安：已启用，每天 %s（本地时间）推送，目标 %d 个群，素材目录 %s",
            self.cfg.morning_time,
            len(self.cfg.morning_groups),
            self.audio_dir,
        )
        while not self._stopped.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 和月曜一样：出问题只记日志，**不往群里报错**。
                _log.exception("早安：本轮检查异常，跳过")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=CHECK_SEC)
            except asyncio.TimeoutError:
                pass
        _log.info("早安：已停止")

    async def poll_once(self, *, now: datetime | None = None) -> int:
        """跑一轮，返回这轮发出去的条数。"""
        if not self.enabled:
            return 0

        now = now or datetime.now()
        slot = slot_for(now, parse_time(self.cfg.morning_time))
        # **不补发**：窗口只覆盖调度抖动。晚于窗口就今天没发，直接返回。
        if (now - slot).total_seconds() > self.cfg.morning_window_sec:
            return 0

        slot_key = slot.strftime("%Y-%m-%dT%H:%M")
        done = self.db.morning_posted(slot_key)
        targets = [g for g in self.cfg.morning_groups if g not in done]
        if not targets:
            return 0

        voices = list_voices(self.audio_dir)
        if not voices:
            if self._warned_slot != slot_key:
                self._warned_slot = slot_key
                _log.warning(
                    "早安：%s 里没有能发的 silk，今天跳过。"
                    "先跑 scripts/make_morning.py 合成，再同步到 NAS",
                    self.audio_dir,
                )
            return 0

        # ⚠️ **选人在循环外** —— 一个 slot 所有群发同一位偶像、同一句台词。
        # 放进循环的话，多群用户会在两个群看到不同的人，像精神分裂。
        recent = self.db.morning_recent_idols(self.cfg.morning_recent_days)
        idol = pick_idol(voices, recent)
        line = pick_line(voices[idol], self.db.morning_last_line())

        sent = 0
        for group in targets:
            # 逐群隔离：一个群发不出去（配额、权限、接口抖动）不能连累别的群。
            # 失败**不记账**，窗口内下一次 tick 会重试；窗口过了就当今天没发。
            try:
                await self.sender.send_voice(group, line)
            except Exception as exc:  # noqa: BLE001
                _log.warning("早安：发送失败 → %s: %s", group[:8], exc)
                continue
            self.db.mark_morning_posted(slot_key, group, idol, line.name)
            sent += 1
            _log.info("早安：已推送 %s（%s）→ %s", idol, line.name, group[:8])
        return sent
