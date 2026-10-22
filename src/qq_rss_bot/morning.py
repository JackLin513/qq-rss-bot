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

## 一条早安 = 两条消息（2026-10-20 起）

原本只发一条语音。**群里没人点开听**，所以改成：

    ① msg_type=2：头像 + 「角色名\n日语原文」     ← send_markdown（图走图库）
    ② msg_type=7：语音                            ← send_voice

语音没法塞进 markdown（`file_type=3` 是富媒体），所以必然是两条。
（2026-10-22 之前头像是 base64 上传 + `msg_type=7`，改成图库 + markdown 之后
顺带摆脱了 3.5 MB 的原图上限。）

**不走 markdown**：markdown 的图必须公网 URL，QQ 自己派服务器去拉，
本地文件一个字节都送不进去 —— 而头像就是本地文件。180×180 在 `msg_type=7`
下渲染出来的大小客户端自己定，实测**够用**，所以 `#Wpx #Hpx` 那套用不上。

### 头像

`data/morning/avatar/<偶像 id>.png`，180×180，**按 id 直接取文件，不扫目录**。
用 `scripts/crop_morning_avatar.py` 从官方立绘大图裁（Mac 上离线跑，用 ffmpeg）。

### 文字从哪来 —— ⚠️ manifest 成了运行时输入

角色名和日语原文都在 `data/morning/.manifest.json` 里，**键就是 silk 文件名**
（`line.name`），不做任何索引换算。`name` 是 2026-10-20 才加的字段，
老记录用 `scripts/backfill_morning_names.py` 补，**不用重跑 TTS**。

⚠️ 这意味着 manifest **必须跟 `audio/` 一起同步到 NAS**。缺了的症状是
「文字静默消失」，所以拿不到文字时**退回纯语音**（= 加这个功能之前的行为，
不是发一条光秃秃的图），并且打一条 WARNING。

`ja` 带 TTS 语气提示（`[softly] ` 之类），**发到群里要剥掉** —— 那是喂给
TTS 的，不是给人看的。

### 失败语义

| 哪一条挂了 | 行为 |
|---|---|
| **① 头像+文字** | 抛出去 → 这一轮**不记账**，窗口内下一拍重试 |
| **② 语音** | 只记 WARNING，**照样记账** —— 头像已经发出去了，再退一次就是重复刷屏 |

逐群隔离：一个群失败不连累别的群。和 `birthday_art` / `monday` 同一条规矩。

## 时间用本地时间（`Asia/Shanghai`）

不涉及日本时间。别照抄`ticket`那套 JST 换算。

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

## 版权

`data/morning/` 整个是**不提交**的：`source/` 是从 [Sallyn0225/eidolon-sc]
（AGPL-3.0）抓的第三方资料，音色是社区上传到 Fish Audio 的公开模型，
角色版权属于原作方。个人粉丝群自用，和那个仓库是同一个姿态。

[Sallyn0225/eidolon-sc]: https://github.com/Sallyn0225/eidolon-sc
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Sequence

from . import features, imagelib, multiimage

_log = logging.getLogger("qq_rss_bot.morning")

# silk 的文件头。QQ 语音要的就是这个格式，**这条路唯一的格式校验点**。
SILK_MAGIC = b"\x02#!SILK_V3"

# 角色名 + 日语原文的索引。**运行时要读**（见模块说明），键是 silk 文件名。
MANIFEST_NAME = ".manifest.json"

# 头像+文字 和 语音 两条之间的间隔。挨太近客户端可能把气泡叠在一起
# （2026-10-20 手工实测用的就是 2 秒）。**没有反例证明非它不可**，
# 但留着的成本只有每群两秒，比顺序乱了强。
MESSAGE_GAP_SEC = 2.0

# TTS 语气提示的形态：`[softly] ` / `[cheerfully] `。**只给 TTS 用**，
# 发到群里得剥掉。上游只保证英文、字母/空格/逗号/撇号/连字符。
_CUE = re.compile(r"\[[A-Za-z][A-Za-z ,'-]*\]\s*")

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


def strip_cue(ja: str) -> str:
    """`[softly] あ……おはよう` → `あ……おはよう`。

    语气提示是喂给 TTS 的，不是给人看的 —— 直接发到群里只会显得莫名其妙。
    """
    return _CUE.sub("", ja).strip()


def load_lines(morning_dir: Path) -> dict[str, dict]:
    """读 `.manifest.json` → `{silk 文件名: {"name": 角色名, "ja": 日语原文}}`。

    **键就是文件名** —— 运行时手里正好有 `line.name`，不用做任何索引换算，
    也就没有「第几条对第几条」这种会错位的东西。

    读不到就返回空 dict，**不抛**：早安这条的主体是语音，文字没了不该连语音
    一起不发。调用方（`MorningPoller`）会打 WARNING，不静默。
    """
    path = morning_dir / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        _log.warning("早安：%s 读不了，这轮只发语音", path)
        return {}
    if not isinstance(data, dict):
        _log.warning("早安：%s 不是一个对象，这轮只发语音", path)
        return {}
    out: dict[str, dict] = {}
    for fname, rec in data.items():
        if not isinstance(rec, dict):
            continue
        out[fname] = {
            "name": str(rec.get("name") or ""),
            "ja": strip_cue(str(rec.get("ja") or "")),
        }
    return out


def render_content(rec: dict | None) -> str:
    """`{"name": …, "ja": …}` → `"角色名\\n日语原文"`。缺哪行就少哪行。

    两行**都是空**时返回 `""` —— 调用方据此退回纯语音（见 `poll_once`）。
    """
    if not rec:
        return ""
    return "\n".join(x for x in (rec.get("name", ""), rec.get("ja", "")) if x)


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
    """每天一次的早安：一条「头像 + 角色名 + 日语原文」，一条语音。

    和 `MondayPoller` / `TicketPoller` 同形，单独一个 task，不塞进 `FeedPoller`
    —— 数据源、节奏、失败语义都不一样。
    """

    def __init__(self, cfg, db, sender, library=None) -> None:
        self.cfg = cfg
        self.db = db
        self.sender = sender
        # 头像也走图库（内容寻址，同一张只存一份），消息用 markdown 发。
        self.library = library
        self._stopped = asyncio.Event()
        # 同一个 slot 只警告一次。窗口内会醒好几次，不记的话会每分钟刷一条。
        # 三条各自记 —— 用同一个 key 的话，「目录空」会把「缺头像」盖掉。
        self._warned_slot: str | None = None      # 目录空 / 全是坏文件
        self._warned_text: str | None = None      # 读不到角色名 / 原文
        self._warned_avatar: str | None = None    # 缺这位偶像的头像

    @property
    def groups(self) -> tuple[str, ...]:
        """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。

        群开关存在库里（见 `features.py`），`.env` 里**一个群参数都没有**。
        """
        return features.groups_for(self.db, "morning")

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.morning_enabled and self.groups)

    @property
    def morning_dir(self) -> Path:
        return Path(self.cfg.morning_dir)

    @property
    def audio_dir(self) -> Path:
        return self.morning_dir / "audio"

    @property
    def avatar_dir(self) -> Path:
        return self.morning_dir / "avatar"

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        # ⚠️ **这里不能 return。** 群开关在库里（`features.py`），`/set` 的语义是
        # 「改完下一拍就生效」—— 启动时库里一个群都没开的话，一 return 这个 task
        # 就**永久结束**了：之后再怎么 `/set` 开群都不会跑，只能重启容器。
        # 留着轮询不花什么钱 —— `poll_once` 每拍开头 `if not self.enabled: return 0`。
        if not self.enabled:
            _log.info("早安：库里没有开着的群，功能关闭（**仍然轮询**，/set 开了下一拍生效）")
        else:
            _log.info(
                "早安：已启用，每天 %s（本地时间）推送，目标 %d 个群，素材目录 %s",
                self.cfg.morning_time,
                len(self.groups),
                self.audio_dir,
            )
        # 头像和 manifest 都是**可选**的（缺了退回纯语音），但缺了要让人知道，
        # 所以在启动时就把状态打出来，不用等出问题才发现。
        lines = load_lines(self.morning_dir)
        avatars = (
            len(list(self.avatar_dir.glob("*.png"))) if self.avatar_dir.is_dir() else 0
        )
        _log.info(
            "早安：台词表 %s（%d 条），头像 %d 张 —— %s",
            "读到了" if lines else f"没读到（{self.morning_dir / MANIFEST_NAME}）",
            len(lines),
            avatars,
            "发「头像+文字+语音」"
            if lines and avatars
            else "**只有语音**（缺台词表或头像，跑 scripts/backfill_morning_names.py "
                 "和 scripts/crop_morning_avatar.py）",
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
        targets = [g for g in self.groups if g not in done]
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

        # 文字：角色名 + 日语原文，从 manifest 按**文件名**取（不用索引换算）。
        content = render_content(load_lines(self.morning_dir).get(line.name))

        # 头像：按 id 直接取文件，**不扫目录**。缺了退回纯语音，不静默。
        avatar = self.avatar_dir / f"{idol}.png"
        if not avatar.is_file():
            if self._warned_avatar != slot_key:
                self._warned_avatar = slot_key
                _log.warning(
                    "早安：没有 %s 的头像，今天只发语音。"
                    "跑 scripts/crop_morning_avatar.py 裁一批，再同步到 NAS",
                    avatar,
                )
            avatar = None

        # 文字拿不到就**整条退回纯语音**（= 加这个功能之前的行为），
        # 而不是发一条光秃秃的图 —— 没文字的头像不如不发。
        if not content:
            if self._warned_text != slot_key:
                self._warned_text = slot_key
                _log.warning(
                    "早安：%s 里没有 %s 的角色名/原文，今天只发语音。"
                    "跑 scripts/backfill_morning_names.py 补，再同步到 NAS",
                    self.morning_dir / MANIFEST_NAME,
                    line.name,
                )
            avatar = None

        # 头像入库**只做一次**（所有群共用同一个地址）。
        ref = None
        if avatar is not None and self.library is not None and self.library.enabled:
            ref = self.library.store(
                avatar.read_bytes(),
                kind=imagelib.KIND_FEED,
                event_date=slot.date().isoformat(),
                source_id=f"{idol}.png",
            )
        if ref is not None:
            content = (
                f"{content}\n"
                f"{multiimage.image_line(1, ref.url, (ref.width, ref.height), multiimage.SINGLE_W)}"
            )

        sent = 0
        for group in targets:
            # 逐群隔离：一个群发不出去（配额、权限、接口抖动）不能连累别的群。
            try:
                if avatar is None:
                    await self.sender.send_voice(group, line)
                else:
                    # ① 头像 + 文字（一条 markdown）。这一条挂了整轮不算数 ——
                    # 不记账，窗口内下一拍会重试。图库不可用时退回纯文字，
                    # 别把文字也一起丢了。
                    if ref is not None:
                        await self.sender.send_markdown(group, content)
                    else:
                        await self.sender.send_text(group, content)
                    await asyncio.sleep(MESSAGE_GAP_SEC)
                    # ② 语音。这一条挂了**只记 WARNING、照样记账** ——
                    # 头像已经发出去了，再退一次就是重复刷屏。
                    try:
                        await self.sender.send_voice(group, line)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "早安：语音没发出去（头像已发，不重试）→ %s: %s",
                            group[:8], exc,
                        )
            except Exception as exc:  # noqa: BLE001
                _log.warning("早安：发送失败 → %s: %s", group[:8], exc)
                continue
            self.db.mark_morning_posted(slot_key, group, idol, line.name)
            sent += 1
            _log.info(
                "早安：已推送 %s（%s，%s）→ %s",
                idol, line.name,
                "头像+文字+语音" if ref is not None else "纯语音",
                group[:8],
            )
        return sent
