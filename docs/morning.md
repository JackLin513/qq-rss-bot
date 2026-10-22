> 本文件是 `CLAUDE.md` 拆出来的一节。2026-10-20 加早安卡片时同步更新过。
> 常驻的精简要点在 [`../CLAUDE.md`](../CLAUDE.md)。

# 小偶像的每日早安语音（2026-10-19 加）

每天早上 **07:30** 随机挑一位《偶像大师 闪耀色彩》的偶像，发到群里。

**2026-10-20 起是「两条消息」**（原先只有一条语音，而**群里没人点开听**）：

    ① markdown：头像 + 「角色名\n日语原文」     ← send_markdown（图走图库）
    ② msg_type=7：语音                          ← send_voice

语音没法塞进 markdown（`file_type=3` 是富媒体），所以必然是两条。
两条之间隔 `MESSAGE_GAP_SEC`（2 秒），挨太近客户端可能把气泡叠在一起。

⚠️ **2026-10-22 之前头像是 base64 上传**（`send_local_image`），理由是「markdown
的图必须公网 URL、QQ 派服务器去拉，本地头发不出去」。现在有了自建图床，
头像先入库、再由图床用自己的地址发 —— 那条推理不再成立。

**失败语义两条故意不一样**（这是最容易写反的地方）：

| 哪一条挂了 | 行为 |
|---|---|
| **① 头像+文字** | 抛出去 → 这一轮**不记账**，窗口内下一拍重试 |
| **② 语音** | 只记 WARNING，**照样记账** —— 头像已经发出去了，再退一次就是重复刷屏 |

⚠️ 是**闪耀色彩**（シャニマス / 283プロ），**不是**「学園アイドルマスター」。
证据：Fish Audio 的模型简介原文是 `THE IDOLM@STER SHINY COLORS | illumination STARS`，
资料里的人设写「283 事务所」，上游仓库叫 **SC**TranslationData。

### 四段流水线，前四段全离线

```
① scripts/fetch_morning_source.py   → data/morning/source/   （抓资料，GitHub 直连）
② data/morning/lines.toml           → 台词表（人工审过；校验在 make_morning.py 里）
③ scripts/make_morning.py           → data/morning/audio/*.silk（TTS，走代理）
                                    + data/morning/.manifest.json（角色名 + 原文）
④ scripts/crop_morning_avatar.py    → data/morning/avatar/<偶像 id>.png（裁立绘大图）
                                     ↓ 四样**一起**同步到 NAS
                  运行时读 audio/ + manifest + avatar/，零 TTS、零新依赖
```

⚠️ **`data/morning/` 里现在有四样东西，同步到 NAS 时一个都不能漏**：
`audio/`、`.manifest.json`、`avatar/`、`lines.toml`（最后这个只是备份用，
运行时**不读**）。漏了 manifest 的症状是「早安的文字静默消失」——
所以代码里拿不到文字会**退回纯语音**并打 WARNING，不会静默。

`name` 字段是 2026-10-20 才加到 manifest 里的，老记录用
`scripts/backfill_morning_names.py` 补，**不用重跑 TTS**（那要重花钱和时间）。

**运行时零新依赖是刻意的取舍**，不是省事：跑在容器里就要把 `pysilk` 装进镜像、
把 Fish Audio 的 key 塞进 `.env`，而且每天 07:30 那一刻多一跳外网 ——
Fish Audio 一抖当天就没问候。离线做完，那一刻只剩「读文件 → base64 → 上传」。

### ⚠️ 语音这条路**上传接口完全不校验格式**（2026-10-19 实测）

`file_type=3`（语音 silk）拿到 `file_info` **一点信息量都没有**：

| 传的什么 | 结果 |
|---|---|
| 正常 silk | ✅ HTTP 200 + `file_info` |
| mp3 / wav / m4a | ✅ HTTP 200 + `file_info` |
| **`\x02#!SILK_V3` 后面接两千个零字节的纯垃圾** | ✅ HTTP 200 + `file_info` |

所以「上传成功 ≠ 能播」在这条路上**尤其致命** —— 视频那条至少还有 10 MiB 的
硬边界（`40093011`），语音这条**连格式都不挡**，唯一的把关是客户端侧的
**silk 魔数闸**，两道：`morning.list_voices` 扫目录时跳过（并打 WARNING 点名），
`RssBot.send_voice` 发之前再挡一次。**别把任何一道去掉。**

### silk 只能在 Mac 上离线编

ffmpeg **没有** silk 编码器，brew 里也没有。用 PyPI 的 `pysilk`（包名
`silk-python`），**必须 `tencent=True`**（QQ / 微信用的是腾讯那套变体），
输入要 **24000 Hz 单声道 WAV**：

```bash
uv venv /tmp/silkvenv && uv pip install --python /tmp/silkvenv/bin/python silk-python
```

`make_morning.py` 默认就用 `/tmp/silkvenv/bin/python`（`--silk-python` 可改）——
**这个包不进项目依赖**，容器里永远不需要它。

试听：
```bash
# ⚠️ decode() 有**三个**必填参数：input / output / sample_rate
/tmp/silkvenv/bin/python -c "from pysilk import decode; \
  decode(open('data/morning/audio/mano_0.silk','rb'), open('/tmp/a.pcm','wb'), 24000)"
ffmpeg -f s16le -ar 24000 -ac 1 -i /tmp/a.pcm /tmp/a.wav && open /tmp/a.wav
```

### ⚠️ pysilk 编码器的第 0 帧是坏的 —— 每句开头会「啪」一声

2026-10-19 用户听出来的。**不是 Fish Audio 的问题**（原始 WAV 干净：前 0.5s 最大
跳变 903），是 `pysilk` 编码器**冷启动那一帧被编成了垃圾数据**：

- 拿 1 秒**纯数字静音**编一遍再解回来，开头照样炸出 **±28871** —— 和信号无关。
- 位置固定在解码输出的第 **131** 个样本（5.5ms），落在本该是静音的地方。
- 输入前面**垫静音也没用**，爆音照样在原位置，还会把垫的静音一起污染。

**做法**（`make_morning.py` 的 `to_silk`，实测表在 `SILK_WARMUP_MS` 上面的注释里）：

1. **编之前垫 200ms 静音** —— 让冷启动那几帧落在静音上；
2. **编之后丢掉前 8 帧**（`_silk_frames` / `_join_silk_frames`）—— 扔掉它们，
   留 2 帧（40ms）静音当前导。

量的是「解码后前 400 个样本的绝对值峰值」（原始 365）：

| 改法 | 峰值 |
|---|---|
| 原样 | 20778 |
| 只丢第 0 帧 | 1736 |
| 输入前 20ms 淡入 | 20757（**没用**，伪影是加性的） |
| 垫 100ms + 丢 5 帧 | 315 |
| **垫 200ms + 丢 8 帧** | **3** ← 采用 |

⚠️ **帧数对得上**（输入 199.5 帧 → silk 199 帧，跳一帧后最佳对齐正好差 480 样本），
说明**第 0 帧是普通音频帧、不是头部帧** —— 删掉它是安全的，代价只是少 200ms 静音。

⚠️ **这是在唯一可用的解码器（pysilk）上测的**，QQ 自己的解码器未必有同样的毛病；
但删冷启动帧**无害且免费**，所以就做了。

⚠️ **`SILK_ENCODER_VERSION` 编在 manifest 键里**（`line_key`）—— 调 `SILK_WARMUP_MS`
这类参数时 `ja` 一个字没变，光靠台词指纹认不出「音频该重做了」。**改参数要 +1，
然后跑 `--force`**，否则已经合成过的文件会被跳过。

### ⚠️ 网络是**分裂**的，两条路的默认值相反

| 打谁 | 直连 | 默认代理 |
|---|---|---|
| `raw.githubusercontent.com` / `api.github.com` | ✅ 通 | **不要**走代理 |
| `api.fish.audio` | ❌ HTTP 000 | **必须** `http://192.168.0.63:6152` |

`fetch_morning_source.py` 直连，`make_morning.py` 默认走 Surge。**别互相抄。**

### ⚠️ Fish Audio 的两个静默坑

1. **`model` 是 HTTP 头，不是 body 字段。**
   `headers={"Authorization": f"Bearer {key}", "model": "s2.1-pro-free"}`。
2. **模型名写错不报错**，会**静默按付费的 `s2.1-pro` 合成**。枚举是
   `s2.1-pro-free`（免费，不消耗额度）/ `s2.1-pro` / `drama-3` / `s2-pro` / `s1`，
   `make_morning.py` 自己校验一遍。

同理 **`pronunciation_dictionary` 形状是 `[{"items": [{"key","value","case_sensitive"}]}]`**
—— 外面还套一层带 `items` 的数组。写成裸列表服务端不认，**而且它不报错，
只是静默跳过这份表**。空的时候整个字段**不要发**。（形状抄自上游
`backend/src/eidolon/readings.py` 的 `pronunciation_dictionary()`。）

### 语气提示（「语音习惯」全靠它）

台词写成 `[softly] おはようございます` 这样，Fish Audio 会照着念出起伏。
正则逐字抄自上游 `backend/src/eidolon/delivery_cues.py`：

```python
_CUE = re.compile(r"\[[A-Za-z][A-Za-z ,'\-]*\]")
```

- **只有英文字母 / 空格 / 逗号 / 撇号 / 连字符。** `(happy)` / `【嬉しそうに】` /
  `[高兴]` 都不是提示，会被**原样念出来**。
- ⚠️ **写在句尾的提示永远不会被念出来**（上游原文：「a cue with no words after it
  in the line is never voiced」）。**提示必须在句首、后面还有词** ——
  写在句尾音频里一点效果都没有，而且从任何日志上都看不出来。
- 只从上游用过、验证有效的词表里挑：`softly shyly happily excitedly teasing
  sweetly proudly nervously lazily gently flatly confidently cheerfully`。
- 方括号提示**任何模型都支持**；成对标签（`<whisper>` 之类）**只有 `drama-3`** ——
  所以免费档够用。

### 素材与版权

- **台词是我们自己写的文案**，不复制上游任何一段原文。差别全靠
  「自称 / 对制作人的称呼 / 句尾口癖 / 语气提示 / 语序」拉开，v1 **只有一句早上好**。
- `data/morning/source/` 是从 [Sallyn0225/eidolon-sc]（**AGPL-3.0**）抓的第三方资料，
  那 28 个 `voice_id` 是社区上传到 Fish Audio 的**公开模型**，角色版权属于原作方。
  **`data/morning/` 整个不进 git**（`data/` 已在 `.gitignore` 里）。
  **别把它挪出 `data/`。**
- ⚠️ **`data/morning/lines.toml` 是本次最有价值的产物**（28 位 × 7 条，人工写过一遍），
  丢了要重来。建议自己另做一份备份。

### 运行时的口径

- **扫 `data/morning/audio/`，并按文件名读 `.manifest.json` 和 `avatar/`。**
  仍然**不读 `lines.toml`**（那是离线的台词表，运行时用不上）。
  ⚠️ manifest 是 2026-10-20 加卡片时**新变成运行时输入**的 —— 键就是 silk
  文件名（`line.name`），不做任何索引换算，所以不存在「第几条对第几条」
  这种会错位的东西。头像也是**按 `<偶像 id>.png` 直接取文件，不扫目录**，
  少一个「索引说有、文件没同步过去」的脏状态。
- **缺材料时的退化**：`audio/` 空 → 打 WARNING 跳过（这是纯语音播报，
  发条文字反而莫名其妙）；缺 `avatar/` 或缺 manifest 里的文字 →
  **退回纯语音**并打 WARNING。
- 文件名必须是 **`<偶像 id>_<序号>.silk`**（序号是数字；用 `rpartition` 切，
  因为偶像 id 里可能有下划线）。不合规的跳过并 WARNING 点名。
- ⚠️ **跳过 `._` 开头的**：macOS 的 tar / Finder 带出来的 AppleDouble 文件
  **同样以 `.silk` 结尾**，不排除会被当成语音上传。`monday.list_videos` 吃过同一个亏。
- **每天一次，所有群发同一位偶像、同一句台词**（选人在循环外）。
- **不补发**：`MORNING_WINDOW_SEC`（默认 180 秒）只覆盖 tick 抖动 ——
  九点钟补一条「早上好」比不补更怪。用户明确选的。
- 一位偶像的台词只有 7 条，所以**隔几天会听到同一条**；28 位轮一圈约一个月，
  同一条要七个月才重复。这是刻意的（v1 只做「一句早上好」，没有素材拉开差别）。
- `scripts/probe_morning_all.py` **批量试听**（默认只列清单）：把 `audio/` 里
  196 条全发一遍到一个群。和上面那个的分工是「诊断这一拍会挑谁」对
  「上线前全听一遍」。**自己限速**（默认 15 条/分钟，官方群维度上限 20）——
  它直接 POST HTTP API，**不经过 `quota.py` 的令牌桶**。也**不写库**，
  所以不影响真实的 07:30。正式群尾号 `E0629` **写死拒发**（一次发一百多条，
  填错就是事故），`--group` 无默认值，交互确认要求手打后 8 位。
- `scripts/probe_morning.py` 离线看（**默认不发消息**）：扫到什么、
  **哪些文件被跳过及原因**、slot / 窗口、上次发的是谁、最近发过哪些偶像、
  这一拍会挑谁和哪一条。`--group` **故意没有默认值**（群 openid 不是群号，
  看不出是不是测试群）。

[Sallyn0225/eidolon-sc]: https://github.com/Sallyn0225/eidolon-sc
