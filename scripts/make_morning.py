"""把 `data/morning/lines.toml` 的台词合成为 QQ 能发的 silk 语音。

**这是离线工具，容器里永远不跑它。** 理由见实现计划：跑在容器里就得把 `pysilk`
装进镜像、把 Fish Audio 的 key 塞进 `.env`，而且每天 7:30 那一刻还要多一跳外网 ——
Fish Audio 一抖当天就没问候。离线做完，运行时只剩「读文件 → base64 → 上传」。

跑法：
    uv run python scripts/make_morning.py --dry-run          # 只列要合成什么，不联网
    uv run python scripts/make_morning.py --only mano        # 只做一位，先试听
    uv run python scripts/make_morning.py                    # 全部，缺什么补什么
    uv run python scripts/make_morning.py --force            # 全部重做（换了模型才需要）

    # 试听
    uv run python scripts/make_morning.py --only mano
    # ⚠️ `decode()` 有**三个**必填参数：input / output / sample_rate
    /tmp/silkvenv/bin/python -c "from pysilk import decode; \
        decode(open('data/morning/audio/mano_0.silk','rb'), open('/tmp/a.pcm','wb'), 24000)"
    ffmpeg -f s16le -ar 24000 -ac 1 -i /tmp/a.pcm /tmp/a.wav && open /tmp/a.wav

## key 从哪来

`FISH_AUDIO_API_KEY` 环境变量，或者 `data/morning/.fish_key`（文件里就一行 key）。
**两者都不进 git、都不打印。** `data/` 整个在 `.gitignore` 里。

    export FISH_AUDIO_API_KEY=...
    # 或
    printf '%s' '你的key' > data/morning/.fish_key && chmod 600 data/morning/.fish_key

## ⚠️ 网络：和抓资料那一步相反

* GitHub（`fetch_morning_source.py`）**直连是通的**，不要走代理。
* `api.fish.audio` **直连不通**（实测 HTTP 000），**必须走 Surge 代理**
  `http://192.168.0.63:6152` —— 所以 `--proxy` 的默认值就是它。
  换网络环境时用 `--proxy ''` 强制直连试试。

## ⚠️ 模型名写错不会报错

Fish Audio 的模型枚举是 `s2.1-pro-free`（**免费，不消耗额度**）/ `s2.1-pro` /
`drama-3` / `s2-pro` / `s1`。**名字写错不会返回错误，会静默按付费的 `s2.1-pro` 合成**
（上游仓库文档里明确警告过）。所以这里自己校验，不在枚举里直接退出。

## ⚠️ `model` 是 **HTTP 头**，不是 body 字段

    headers = {"Authorization": "Bearer <key>", "model": "s2.1-pro-free"}
    json    = {"text": ..., "reference_id": <voice_id>, "format": "wav",
               "sample_rate": 24000, "pronunciation_dictionary": [...]}

这三行是从上游 `backend/src/eidolon/call.py` 和 `readings.py` 里抄准的，**别凭印象改**：

* `pronunciation_dictionary` 的形状是 **`[{"items": [{"key","value","case_sensitive"}]}]`**
  —— 外面还套一层带 `items` 的数组，不是裸列表。空的时候整个字段**不要发**（上游
  发的是 `None`，`if self._dictionary is not None` 才塞进去）。
* `key` 是 `written`（原样写法），Fish Audio 按**子串、区分大小写**匹配。
* 上游只把**这段文本里真出现过的**读音条目发过去（`used_in`，最长优先）。
  我们的台词是纯招呼语，正常一条都不会命中 —— 留着是为了以后台词里提到人名时能念对。

## ⚠️ 为什么用 `wav` 而不是上游用的 `mp3`

silk 编码要 PCM，mp3 还得再解一道。上游用 mp3 是因为它走的是 WebSocket 流式通话，
mp3 分片方便。我们是离线一次性拿整段，`wav` 少一次有损转码。
**万一服务端不认 `wav`**（返回的不是 RIFF），这里会退回 ffmpeg 转一道，不会静默写坏文件。

## 幂等

`data/morning/.manifest.json` 记每条台词的 `sha256(voice_id + model + ja)[:16]`
—— **`ja` 含语气提示**，因为提示也进 TTS 输入，改提示等于改了音频。
manifest 只在离线用，**运行时不读**。改一条台词只会重合成那一条。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import httpx  # noqa: E402

TTS_URL = "https://api.fish.audio/v1/tts"

# 实测：Mac 直连 api.fish.audio 是 HTTP 000，必须走这个代理。
# ⚠️ 和 fetch_morning_source.py 相反 —— 那边直连就行，不要照抄这个默认值。
DEFAULT_PROXY = "http://192.168.0.63:6152"

# 上游 call.py 里用的枚举。**写错不会报错，会静默按付费的 s2.1-pro 合成**，
# 所以这里自己挡一道。
MODELS = ("s2.1-pro-free", "s2.1-pro", "drama-3", "s2-pro", "s1")
DEFAULT_MODEL = "s2.1-pro-free"

SAMPLE_RATE = 24000          # silk 要 24k
CONCURRENCY = 5              # 免费档别太贪
RETRIES = 4                  # 429 / 5xx 退避重试次数

# pysilk 不在项目依赖里（**故意的**：容器不该有这个包）。走一个单独的
# python 解释器把它编出来。装法见 README / CLAUDE.md。
DEFAULT_SILK_PYTHON = "/tmp/silkvenv/bin/python"
SILK_MAGIC = b"\x02#!SILK_V3"

SILK_SNIPPET = (
    "import sys;from pysilk import encode;"
    "encode(open(sys.argv[1],'rb'),open(sys.argv[2],'wb'),24000,24000,tencent=True)"
)

# ---- 消掉 pysilk 开头那声「啪」---------------------------------------------
#
# 2026-10-19 用户反馈「每一句开头有个啪的声音」，查出来的根因：
# **pysilk 编码器的第 0 帧是坏的。** 拿 1 秒**纯数字静音**编一遍再解回来，
# 开头照样炸出 ±28871 —— 和信号无关，是编码器冷启动那一帧被编成了垃圾数据。
# 位置固定在解码输出的第 131 个样本（5.5ms）。Fish Audio 的原始 WAV 是干净的
# （前 0.5s 最大跳变 903），所以不是 TTS 的问题。
#
# 试过的改法，量的是「解码后前 400 个样本的绝对值峰值」（原始是 365）：
#
#   | 改法 | 峰值 | 结论 |
#   |---|---|---|
#   | 原样 | 20778 | ← 就是用户听到的「啪」 |
#   | 只丢掉第 0 帧 | 1736 | 好很多，但还不够 |
#   | 输入前 20ms 做淡入 | 20757 | **没用**（伪影是加性的，不是包络问题） |
#   | 垫 100ms 静音再丢前 5 帧 | 315 | ✅ |
#   | **垫 200ms 静音再丢前 8 帧** | **3** | ✅ 采用这个 |
#
# 原理：先垫静音，冷启动那几帧就落在静音上；编完**把它们整帧删掉**，
# 剩下的帧是编码器「热」了之后编的，解出来干净。留 2 帧（40ms）静音当前导。
#
# ⚠️ 帧数对得上（实测：输入 199.5 帧 → silk 199 帧，跳掉一帧后最佳对齐正好
#    差 480 样本），说明**第 0 帧是普通音频帧，不是头部帧** —— 删掉它是安全的，
#    代价只是开头少 200ms 静音。
#
# ⚠️ **这是在唯一可用的解码器（pysilk）上测出来的。** QQ 自己的解码器未必有
#    同样的毛病，但删掉冷启动帧**无害且免费**（只少 200ms 静音），所以就做了。
SILK_FRAME_MS = 20
SILK_WARMUP_MS = 200
SILK_WARMUP_DROP = 8          # 丢掉前 8 帧（160ms），留 2 帧静音

# ⚠️ 改上面任何一个数都要 +1 —— 否则 `ja` 一个字没变，manifest 认不出
#    「音频该重做了」而跳过。**已经合成过的文件要跑 --force 才会更新。**
SILK_ENCODER_VERSION = 2


class Fatal(Exception):
    """跑不下去的错误（配置、key、模型名、台词不合格）。"""


# 语气提示的正则**逐字抄自**上游 `backend/src/eidolon/delivery_cues.py`：
#     _CUE = re.compile(r"\[[A-Za-z][A-Za-z ,'\-]*\]")
# 只有字母 / 空格 / 逗号 / 撇号 / 连字符。`(happy)` / `【嬉しそうに】` / `[高兴]`
# 这种**不会被当成提示**，会被原样念出来 —— 所以这里自己挡一道。
CUE_RE = re.compile(r"^\[([A-Za-z][A-Za-z ,'\-]*)\] (.+)$", re.S)

# 上游用过、确认有效的提示词表。**只从这里挑** —— Fish Audio 对面生词的
# 反应没法预期，而这段代码的全部指望就是提示能念出起伏。
CUES = frozenset(
    "softly shyly happily excitedly teasing sweetly proudly nervously lazily "
    "gently flatly confidently cheerfully".split()
)

# 正文长度（**不含提示**）。下限 5 是有意的：透 / 美琴 / ルカ 那种寡黙角色
# 的招牌就是短句（「……おはよ」），硬撑长反而假。
MIN_BODY, MAX_BODY = 5, 40

# 拉丁字母会被念成字母名；表情符号和颜文字会被念出来或吞掉。
_LATIN = re.compile(r"[A-Za-z]")
_SYMBOL = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2764]")


def validate_lines(idols: list[dict]) -> list[str]:
    """逐条挑毛病，返回问题清单（**不静默丢弃**）。

    上游那条最容易被忽略的规则在这里钉死：**一行末尾的语气提示不会被念出来**
    （`delivery_cues.py` 的 `CueFilter` 注释原文：「a cue with no words after it
    in the line is never voiced」）。所以提示必须在**句首、后面还有词**。
    写在句尾的话音频里一点效果都没有，而且从任何日志上都看不出来。
    """
    bad: list[str] = []
    for idol in idols:
        iid = idol.get("id", "?")
        if not idol.get("voice_id"):
            bad.append(f"{iid}: 缺 voice_id")
        lines = idol.get("lines") or []
        if not lines:
            bad.append(f"{iid}: 没有 lines")
        for n, line in enumerate(lines):
            tag = f"{iid}[{n}]"
            ja, zh = line.get("ja") or "", line.get("zh") or ""
            if not zh:
                bad.append(f"{tag} 缺 zh（审稿用的中文对照）")
            if not ja:
                bad.append(f"{tag} 缺 ja")
                continue
            if "[" not in ja:
                # 单独报这一条 —— 「方括号不是恰好一对」对「压根没写提示」是句
                # 莫名其妙的话，而没写提示的后果（整句平着念）又必须说清楚。
                bad.append(f"{tag} 没有语气提示 —— 整句只会平着念出来：{ja}")
                continue
            if ja.count("[") != 1 or ja.count("]") != 1:
                bad.append(f"{tag} 方括号不是恰好一对：{ja}")
                continue
            m = CUE_RE.match(ja)
            if not m:
                bad.append(
                    f"{tag} 开头不是「[提示] 正文」—— 提示只能放句首、后面还得有词，"
                    f"否则不会被念出来：{ja}"
                )
                continue
            cue, body = m.group(1), m.group(2)
            if cue not in CUES:
                bad.append(f"{tag} 提示词 {cue!r} 不在词表里：{'、'.join(sorted(CUES))}")
            if _LATIN.search(body):
                bad.append(f"{tag} 正文里有拉丁字母（会被念成字母名）：{body}")
            if _SYMBOL.search(body):
                bad.append(f"{tag} 正文里有表情/颜文字（会被念出来或吞掉）：{body}")
            if "[" in body or "]" in body:
                bad.append(f"{tag} 正文里还有方括号：{body}")
            if not MIN_BODY <= len(body) <= MAX_BODY:
                bad.append(
                    f"{tag} 正文 {len(body)} 字符，超出 {MIN_BODY}~{MAX_BODY}：{body}"
                )
    return bad


# ---------------------------------------------------------------- 读音表

def load_readings(path: Path) -> list[dict]:
    """`readings.toml` → `[{"written","kana","phonemes"}]`。文件不在就当空的。

    这份表是上游 `characters/readings.toml`，**别人的 AGPL 资料，不提交**。
    """
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        r for r in (data.get("readings") or [])
        if isinstance(r, dict) and r.get("written") and r.get("phonemes")
    ]


def used_in(text: str, readings: list[dict]) -> list[dict]:
    """`text` 里真出现过的读音条目，按首次出现的顺序。

    逐字扫，每个位置**先试最长的键** —— 上游 `readings.py` 的 `used_in` 就是这个
    口径（「樱木真乃」要盖过「真乃」）。不这么做的话「真乃」会先命中，念出来的调
    和整名的调对不上。

    我们的台词不提任何人名，所以正常返回空表；留着是为了以后能直接用。
    """
    by_written = {r["written"]: r for r in readings}
    lengths = sorted({len(w) for w in by_written}, reverse=True)
    used: dict[str, dict] = {}
    start = 0
    while start < len(text):
        for length in lengths:
            hit = by_written.get(text[start:start + length])
            if hit is not None:
                used.setdefault(hit["written"], hit)
                start += length
                break
        else:
            start += 1
    return list(used.values())


def dictionary_for(text: str, readings: list[dict]) -> list[dict] | None:
    """请求里的 `pronunciation_dictionary`，没有条目就返回 `None`（**整个字段别发**）。

    ⚠️ 形状是 `[{"items": [...]}]` —— 外面还有一层。上游 `readings.py` 就是这么写的，
    写成裸列表服务端不认，而且**它不报错，只是静默跳过这份表**。
    """
    items = [
        {"key": r["written"], "value": r["phonemes"], "case_sensitive": True}
        for r in used_in(text, readings)
    ]
    return [{"items": items}] if items else None


# ---------------------------------------------------------------- 台词表

def load_lines(path: Path) -> list[dict]:
    if not path.is_file():
        raise Fatal(f"找不到台词表 {path} —— 先跑 fetch_morning_source.py，再写台词")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    idols = data.get("idols") or []
    if not idols:
        raise Fatal(f"{path} 里没有 [[idols]]")
    problems = validate_lines(idols)
    if problems:
        # **拒绝合成，而不是跳过坏条目** —— 坏台词的产物是一个能播但内容不对的
        # 语音文件，上线后从日志上完全看不出来。宁可现在拦住。
        raise Fatal(
            f"{path} 里有 {len(problems)} 处不合格，先改台词再合成（不静默丢弃）：\n  "
            + "\n  ".join(problems)
        )
    return idols


def line_key(voice_id: str, model: str, ja: str) -> str:
    """一条台词的指纹。**含语气提示** —— 提示也进 TTS 输入，改提示等于改了音频。

    **也含 `SILK_ENCODER_VERSION`** —— 改 silk 的编码方式（比如调
    `SILK_WARMUP_MS`）时 `ja` 一个字都没变，光靠它认不出「音频该重做了」。
    """
    return hashlib.sha256(
        f"{voice_id}\x00{model}\x00{ja}\x00silk{SILK_ENCODER_VERSION}".encode()
    ).hexdigest()[:16]


# ---------------------------------------------------------------- key

def _from_dotenv(path: Path, name: str) -> str:
    """从 `.env` 里抠出 `NAME=值`。**只认这一行，不解析整个文件。**

    `.env` 是给**容器**用的，里面还有 QQ 的 appid/secret、DeepSeek 的 key ——
    所以这里刻意只用最笨的办法取一行，不把它整个解析进环境或日志。
    值两边的引号会剥掉（`.env` 里写成 `KEY="xxx"` 也认）。
    """
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def load_key() -> str:
    """按这个顺序找 `FISH_AUDIO_API_KEY`，**不打印、不提交**：

    1. **环境变量** —— 临时试一下最方便：`FISH_AUDIO_API_KEY=... uv run ...`
    2. **`.env`** —— 用户实际放的地方。⚠️ `.env` 是**给容器用的**，所以 key 会跟着
       它到 NAS 上（容器里没有任何代码读它，只是文件在那儿）。
       想让 key **只留在 Mac 上**就放到 `data/morning/.fish_key`（`data/` 不进 git、
       也不在部署同步的范围里），这个选项优先级最低。
    3. `data/morning/.fish_key`
    """
    key = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()
    if key:
        return key
    for path in (ROOT / ".env", ROOT / "data" / "morning" / ".fish_key"):
        key = _from_dotenv(path, "FISH_AUDIO_API_KEY") if path.name == ".env" \
            else (path.read_text(encoding="utf-8").strip() if path.is_file() else "")
        if key:
            print(f"key 来源：{path.relative_to(ROOT)}")
            return key
    raise Fatal(
        "没有 Fish Audio 的 key。注册 https://fish.audio → 个人设置里建一个 API key，然后\n"
        "  · 写进 .env：FISH_AUDIO_API_KEY=...\n"
        "  · 或临时：export FISH_AUDIO_API_KEY=...\n"
        "  · 或只留在本机（不跟着 .env 上 NAS）：\n"
        f"      printf '%s' '你的key' > {ROOT / 'data' / 'morning' / '.fish_key'}\n"
        "默认模型 s2.1-pro-free 是免费的，不消耗额度。"
    )


# ---------------------------------------------------------------- silk

def _pad_wav(wav: bytes, ms: int) -> bytes:
    """在 WAV 前面垫 `ms` 毫秒数字静音（重写头）。"""
    with wave.open(io.BytesIO(wav)) as w:
        params = w.getparams()
        pcm = w.readframes(w.getnframes())
    silence = b"\x00" * (SAMPLE_RATE * ms // 1000 * params.sampwidth * params.nchannels)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(silence + pcm)
    return out.getvalue()


def _silk_frames(silk: bytes) -> list[bytes]:
    """拆 silk 的帧。格式：`\x02#!SILK_V3` 后面是重复的 `[u16 LE 长度][负载]`。"""
    body, frames, i = silk[10:], [], 0
    while i + 2 <= len(body):
        n = struct.unpack("<H", body[i:i + 2])[0]
        frames.append(body[i + 2:i + 2 + n])
        i += 2 + n
    return frames


def _join_silk_frames(frames: list[bytes]) -> bytes:
    return SILK_MAGIC + b"".join(struct.pack("<H", len(f)) + f for f in frames)


def to_silk(wav: bytes, silk_python: str, label: str) -> bytes:
    """WAV 字节 → silk 字节。**必须 `tencent=True`**（QQ/微信用的是腾讯那套变体）。

    `pysilk` 走单独的解释器（见模块说明），所以这里落临时文件、起子进程。

    前后各有一道加工，都是为了消掉 pysilk 编码器第 0 帧那声「啪」——
    **完整实测表在 `SILK_WARMUP_MS` 上面的注释里**：

    1. **编之前垫 200ms 静音**（`_pad_wav`）—— 让冷启动那几帧落在静音上；
    2. **编之后丢掉前 8 帧**（`_silk_frames` / `_join_silk_frames`）—— 扔掉它们。

    输出不以 `\x02#!SILK_V3` 开头就报错 —— 宁可在这里炸，也别把坏文件同步到 NAS。
    """
    if not Path(silk_python).is_file():
        raise Fatal(
            f"找不到 {silk_python}。pysilk 不在项目依赖里（容器不该有），要单独装：\n"
            "  uv venv /tmp/silkvenv && uv pip install --python /tmp/silkvenv/bin/python silk-python\n"
            "或用 --silk-python 指到别的解释器。"
        )
    padded = _pad_wav(wav, SILK_WARMUP_MS)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.silk"
        src.write_bytes(padded)
        proc = subprocess.run(
            [silk_python, "-c", SILK_SNIPPET, str(src), str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pysilk 失败：{proc.stderr.strip()[:300]}")
        out = dst.read_bytes()
    if out[:10] != SILK_MAGIC:
        raise RuntimeError(f"{label}: silk 文件头不对（{out[:10]!r}）—— 不写盘")

    frames = _silk_frames(out)
    if len(frames) <= SILK_WARMUP_DROP:
        raise RuntimeError(
            f"{label}: silk 只有 {len(frames)} 帧，不够丢 {SILK_WARMUP_DROP} 帧 —— "
            "台词是不是太短了？"
        )
    trimmed = _join_silk_frames(frames[SILK_WARMUP_DROP:])
    if trimmed[:10] != SILK_MAGIC:
        raise RuntimeError(f"{label}: 去掉冷启动帧后文件头坏了（{trimmed[:10]!r}）")
    return trimmed


def as_wav(raw: bytes, label: str) -> bytes:
    """拿到的字节保证是 24k 单声道 PCM WAV。

    正常服务端会直接给 WAV（`format="wav"`）。万一给了别的（mp3 之类），
    用 ffmpeg 转一道 —— **不要假设**，也不要把非 WAV 直接喂给 pysilk。
    """
    if raw[:4] == b"RIFF":
        return raw
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            f"{label}: 服务端返回的不是 WAV（头 {raw[:8]!r}），而本机没有 ffmpeg 转不了。"
            "装一个 ffmpeg，或改用 --format mp3 并确保 ffmpeg 可用。"
        )
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.bin", Path(tmp) / "out.wav"
        src.write_bytes(raw)
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
             "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{label}: ffmpeg 转换失败：{proc.stderr.strip()[:300]}")
        return dst.read_bytes()


# ---------------------------------------------------------------- manifest

def load_manifest(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(path: Path, manifest: dict) -> None:
    """原子写 —— 半截的 manifest 会让下一次跑重做一批已经好的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


# ---------------------------------------------------------------- 合成

async def synth_one(
    http: httpx.AsyncClient, key: str, model: str, voice_id: str, text: str,
    readings: list[dict], fmt: str,
) -> bytes:
    """一条台词 → WAV 字节。429 / 5xx 退避重试。"""
    headers = {"Authorization": f"Bearer {key}", "model": model}
    body: dict = {
        "text": text,
        "reference_id": voice_id,
        "format": fmt,
        "sample_rate": SAMPLE_RATE,
    }
    dictionary = dictionary_for(text, readings)
    if dictionary is not None:          # 上游也是「有才发」，空表整个不发
        body["pronunciation_dictionary"] = dictionary

    last = ""
    for attempt in range(RETRIES + 1):
        resp = await http.post(TTS_URL, headers=headers, json=body)
        if resp.status_code == 200 and resp.content:
            return resp.content
        last = f"HTTP {resp.status_code} {resp.text[:200]}"
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES:
            await asyncio.sleep(2 ** attempt)
            continue
        break
    raise RuntimeError(last or "空响应")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=ROOT / "data" / "morning")
    ap.add_argument("--only", action="append", default=[],
                    help="只做这几位（可重复），如 --only mano")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"默认 {DEFAULT_MODEL}，可选 {', '.join(MODELS)}")
    ap.add_argument("--format", default="wav", choices=("wav", "mp3"), help="默认 wav")
    ap.add_argument("--proxy", default=DEFAULT_PROXY,
                    help=f"默认 {DEFAULT_PROXY}；传空串强制直连")
    ap.add_argument("--silk-python", default=DEFAULT_SILK_PYTHON)
    ap.add_argument("--force", action="store_true", help="忽略 manifest，全部重做")
    ap.add_argument("--dry-run", action="store_true", help="只列要合成什么，不联网")
    ap.add_argument("--limit", type=int, default=0, help="最多合成几条（试水用）")
    args = ap.parse_args()

    # 模型名先挡一道 —— 写错会静默按付费的 s2.1-pro 合成，不会报错。
    if args.model not in MODELS:
        print(f"❌ 模型名 {args.model!r} 不在枚举里：{', '.join(MODELS)}\n"
              "   ⚠️ Fish Audio 对写错的模型名**不报错**，会静默按付费的 s2.1-pro 合成。")
        return 2

    idols = load_lines(args.dir / "lines.toml")
    readings = load_readings(args.dir / "source" / "readings.toml")
    audio_dir = args.dir / "audio"
    manifest_path = args.dir / ".manifest.json"
    manifest = {} if args.force else load_manifest(manifest_path)

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {i["id"] for i in idols}
        if unknown:
            print(f"❌ --only 里有台词表里没有的 id：{', '.join(sorted(unknown))}")
            return 2
        idols = [i for i in idols if i["id"] in wanted]

    # 先算出这轮要做哪些，再决定要不要 key / 联网 —— --dry-run 完全不碰网络。
    todo: list[tuple[dict, int, str, str, Path]] = []
    skip = 0
    for idol in idols:
        for n, line in enumerate(idol["lines"]):
            ja = line["ja"]
            out = audio_dir / f"{idol['id']}_{n}.silk"
            key = line_key(idol["voice_id"], args.model, ja)
            rec = manifest.get(out.name)
            if not args.force and out.is_file() and rec and rec.get("key") == key:
                skip += 1
                continue
            todo.append((idol, n, ja, key, out))
    if args.limit:
        todo = todo[:args.limit]

    hit = sum(1 for _, _, ja, _, _ in todo if dictionary_for(ja, readings))
    print(f"台词表：{len(idols)} 位 / {sum(len(i['lines']) for i in idols)} 条")
    print(f"模型 {args.model} · 格式 {args.format} · {SAMPLE_RATE} Hz · 并发 {CONCURRENCY}")
    print(f"读音表 {len(readings)} 条，其中 {hit} 条台词会命中（正常是 0，台词不提人名）")
    print(f"要做 {len(todo)} 条，已有跳过 {skip} 条 → {audio_dir}")

    if args.dry_run:
        for idol, n, ja, _, out in todo[:40]:
            print(f"  {out.name:20s} {ja}")
        if len(todo) > 40:
            print(f"  …… 还有 {len(todo) - 40} 条")
        print("\n（--dry-run：没联网、没写盘）")
        return 0

    if not todo:
        print("没有要做的，全部已是最新。")
        return 0

    key = load_key()
    proxy = args.proxy or None
    print(f"代理：{proxy or '（直连）'}")

    audio_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    failed: list[tuple[str, str]] = []
    done = 0

    async with httpx.AsyncClient(timeout=120, proxy=proxy) as http:
        async def worker(idol: dict, n: int, ja: str, lkey: str, out: Path) -> None:
            nonlocal done
            async with sem:
                label = out.name
                try:
                    raw = await synth_one(
                        http, key, args.model, idol["voice_id"], ja, readings, args.format
                    )
                    silk = to_silk(as_wav(raw, label), args.silk_python, label)
                except Exception as exc:  # noqa: BLE001
                    async with lock:
                        failed.append((label, f"{type(exc).__name__}: {exc}"))
                        print(f"  ❌ {label}  {type(exc).__name__}: {str(exc)[:160]}")
                    return
                async with lock:
                    # 先落音频再记 manifest —— 反过来的话，写盘失败会留下
                    # 「manifest 说好了、文件是旧的」这种脏状态。
                    out.write_bytes(silk)
                    manifest[out.name] = {
                        "key": lkey, "idol": idol["id"], "line": n,
                        "voice_id": idol["voice_id"], "model": args.model,
                        "ja": ja, "bytes": len(silk),
                    }
                    done += 1
                    print(f"  ✅ {label}  {len(silk):,} 字节  {ja}")

        await asyncio.gather(*(worker(*t) for t in todo))

    if done:
        save_manifest(manifest_path, manifest)

    print(f"\n完成 {done} 条，失败 {len(failed)} 条。")
    if failed:
        print("失败的（改完重跑，已成功的会跳过）：")
        for label, err in failed:
            print(f"  {label}: {err[:200]}")
        return 1
    print(f"下一步：把 {audio_dir} 同步到 NAS 的 /volume1/docker/qq-rss-bot/data/morning/audio/")
    print("（**只要 audio/**，别把 source/ 和 lines.toml 传上去 —— 运行时用不到）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Fatal as exc:
        print(f"❌ {exc}")
        raise SystemExit(2) from None
