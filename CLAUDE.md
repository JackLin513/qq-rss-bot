# CLAUDE.md

QQ 群 RSS 订阅机器人。单个 Python 进程：botpy 维持 QQ 长连接，同一个事件循环里
跑各个轮询器。

## ⚠️ 这个文件只放「每次都要遵守的东西」

每个功能的完整来龙去脉（踩过的坑、实测数据、为什么这么定）都在 `docs/` 下，
见文末**功能索引** —— 需要时再读。

**不要往这里追加长篇记录。** 它每次对话都会被完整注入，2026-10-20 涨到 142 KB
（≈ 4.5 万 token，占整个上下文预算的 45%）时把窗口挤爆了：压缩后的基线 58k +
重新注入一次 CLAUDE.md 45k ≈ 正好撞上 100k 的压缩线，于是**每 3 轮就压缩一次、
无限空转**。要记新东西就写进对应的 `docs/*.md`，这里只留一行指针。

## 常用命令

```bash
uv sync
scripts/botctl.sh start                        # 启动（后台，日志 /tmp/qq-rss-bot.log）
scripts/botctl.sh restart                      # 重启 —— **改完代码必须重启**，没有热加载
scripts/botctl.sh status                       # 看实例（多于 1 个就是出问题了）★
uv run qq-rss-bot                              # 前台启动（调试用，Ctrl+C 可停）
uv run python scripts/smoke.py                 # 离线+抓取测试（当前 1502 项）
uv run python scripts/integration.py           # 端到端（假推送，不碰 QQ）
uv run python scripts/probe_events.py 20       # 事件探针（先停掉正式实例）
uv run python scripts/probe_panel.py           # 指令面板 schema 探针（自建自删，可复跑）
uv run python scripts/check_sources.py         # 复测 RSSHub 各路由可用性
uv run python scripts/test_push.py             # 测主动消息（不占 WS 会话，可并行）
uv run python scripts/setup_panel.py --list    # 指令面板：查询（只读，先跑这个）
uv run python scripts/setup_panel.py --create  # 指令面板：创建
scripts/wizard-deploy.sh                       # ★ 部署向导：预检 → 选命令 → sudo → 验生效
```

各功能的探针（**一律默认不发消息、`--group` 无默认值**）：

```bash
uv run python scripts/probe_morning.py         # 早安：这拍挑谁 / 哪些文件被跳过及原因
uv run python scripts/probe_birthday.py        # 生日：离线，一次网都不上
uv run python scripts/probe_birthday_art.py    # 贺图：离线；--search 真搜；--send 才真发
uv run python scripts/probe_ticket.py          # 抽票：--now 假装成某时刻验提醒判定
uv run python scripts/probe_monday.py          # 月曜：列出每个文件为什么被过滤
uv run python scripts/probe_imas.py            # 偶像大师 NEWS 标签（--raw 看 CMS 原始字段）
uv run python scripts/probe_bilibili.py        # bilibili 卡片渲染
uv run python scripts/probe_media.py           # YouTube / X 卡片
uv run python scripts/probe_x_search.py        # X 搜索传输诊断（X 改版挂掉时先跑它）
uv run python scripts/probe_mdfmt.py           # markdown 尺寸/语法 A/B（唯一能定图片宽度的方法）
uv run python scripts/probe_textlen.py         # 消息长度上限实测
```

## 硬性约束

### 测试消息只发测试群或私聊

需要往真实 QQ 发消息来验证什么（按钮渲不渲染、推送能不能出去）时，
**只允许发到两个测试群、或者私聊用户本人**。**绝不发正式群。**

**目前有三个群**（openid 是哈希、不可逆，所以「拿群号查一下是不是测试群」做不到）：

| 群 | 能发测试消息吗 |
|---|---|
| **测试群**（群号 575861535） | ✅ |
| **新测试群** | ✅ |
| **正式群**（openid 尾号 `E0629`） | ❌ **绝不** |

⚠️ **三个群的 openid 在记忆库里**（`qq-rss-bot-groups`），**不写进这个仓库** ——
仓库是公开的。别把 openid 写进 `docs/` 或任何会被提交的文件。

### 想确认某个群的 openid：@ 机器人发个标记，再读日志

**群号 → openid 反查不了，但正向可以。** 在群里 @ 机器人发一个标记字符串
（比如 `1`），然后：

```bash
grep "群 .* 指令" data/bot.log | tail -5
# → 群 E521581E… 指令: '1'（角色='owner'）
```

比按 `groups` 表的 `first_seen_at` 推时间戳**可靠得多** —— 那是旁证，会猜错。

⚠️ **测试群同时也在 `MORNING_GROUPS` / `BIRTHDAY_GROUPS` 里** —— 它是测试群，
但也是正式推送目标，所以「发到测试群」时它会真的收到正式内容。
要最干净的环境用**新测试群**（只在 `PREVIEW_GROUP` 里）。

批量探针（`probe_morning_all.py` 之类一次发上百条）里，正式群尾号 **`E0629`
写死拒发**，并要求交互确认手打后 8 位。

### 凭据

- **别用 grep/cat 打印 `.env`**：它含真实凭证，输出会进入会话记录。要检查只列键名。
- 凭据（`X_AUTH_TOKEN` / `X_CT0` / Fish Audio key / DeepSeek key）**绝不提交**
  （仓库是公开的）、**绝不打印** —— 日志里只出现**长度**，smoke 有钉子守着。
- X 的 cookie **用小号**：拿账号 cookie 做自动化访问违反 X 的条款，有被封的风险。

## 部署（本地开发 → NAS 运行）

开发在 macOS（宿主原生跑 + Docker Desktop 跑 RSSHub），长期运行在**群晖 DS220+**
（两者都容器化，x86_64）。跨平台只改部署形态，代码不动。

### 三条命令各管各的，别混

```bash
# 只改了 src/ 或 scripts/ 下的代码（最常见）
sudo docker compose restart bot              # 不重建，重新起进程 → 重新 import

# 只改了 .env（加变量 / 改值）
sudo docker compose up -d bot                # Compose 会 hash env_file，能检测到

# 两个都改了
sudo docker compose up -d --force-recreate bot
```

- ⚠️ **`up -d` 不能替代 `restart`。** 对「已经在跑、配置没变」的容器它是**空操作**，
  输出 `✔ Container qq-rss-bot-bot-1  Running  0.0s`。
  **`0.0s` 不是「重启用时 0 秒」，是「检测到无需变更，直接跳过」。**
  成因：代码在 bind mount 里（`./src:/app/src:ro`），挂载点在配置里只是**路径字符串**，
  Docker 从不哈希它的**内容** —— 改 `src/` 下的文件**永远不可能**触发重建。
- ⚠️ **改 `.env` 时 `restart` 不够**：环境变量在**容器创建时**烘进去，
  `restart` 只是把现有容器再启动一次，**不会再读一遍 `.env`**。
  症状**完全静默**：代码是新的、日志正常、新变量全是代码里的默认值。
- **判据永远是启动日志那一行** `运行代码 /app/src/qq_rss_bot（最新改动 <时间>）`
  （`__main__.code_identity`）。**时间不对就是没重启成功**，别看命令有没有报错
  —— 它不会报错。**这条日志是热更新的前提，别删。**
- 改 `docker-compose.yml` 本身才需要 `up -d` 一次。改 `src/` 下的代码**永远不用重建**。

### 网络：代理只给 RSSHub，绝不能给 bot

- **容器里 `RSSHUB_BASE` 要用服务名 `http://rsshub:1200`**，不是 `127.0.0.1`
  （容器里的 loopback 是容器自己）。
- bot 要访问 `http://rsshub:1200` 这个内部服务名，走代理必然失败；
  且 `api.sgroup.qq.com` 是国内站直连更好。
- v2ex / github 这类上游被 DNS 污染，只有 RSSHub 需要代理。
- **两个 HTTP 客户端别合并**：`rss_http`（RSSHub、bilibili、CMS、DeepSeek，**直连**）
  和 `media_http`（YouTube / X / imas_ch，**走 `MEDIA_PROXY`**）。合并会两头翻车。

### 部署这步只能用户本人做

**`JackLin` 跑不了 docker，这是硬阻塞**：`/var/run/docker.sock` 是 `root:root 660`，
而 **`administrators` 组不给 docker 权限**（DSM 上容器只认 root）。CLI 在
`/usr/local/bin/docker`，但 PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，
非 root 连命令都找不到；`sudo` 要密码。**别反复试。**

- 传文件用 **tar-over-ssh**：群晖的 `rsync` 是 openrsync（协议 29），
  不认 `--info=stats2`，且 fork 出来的 ssh 拿不到密钥。
- 容器日志必须同时落一份到挂载卷（`LOG_FILE=/app/data/bot.log`），
  因为 `docker logs` 要 root、`/volume1/@docker` 非 root 进不去。
- **判断「连上网关没有」只能看日志，别信 netstat**（宿主 netstat 看不到容器命名空间
  的套接字；主动消息也不能当探针，本机实例停掉时发 C2C 照样 HTTP 200）。

📖 **完整版（本地开发 → GitHub → NAS → 换机迁移，一份看完）：**
[`docs/dev-workflow.md`](docs/dev-workflow.md)
—— 含 GUI 幽灵容器、镜像不重建、tar-over-ssh、实机规格、故障判据速查表。

## 平台硬事实（别再查了）

### botpy 1.2.1（读 `.venv` 源码确认过，直接信）

- `Intents(public_messages=True)` → bit 25，覆盖 `on_group_at_message_create` /
  `on_c2c_message_create` / `on_group_add_robot` / `on_group_del_robot` /
  `on_group_msg_reject`。频道的 `public_guild_messages` 是 bit 30，两套互不相干。
- 派发规则是 `method = "on_" + event`（`client.py:256`），**没有显式注册表**。
- **发群消息必须用 `api.post_group_message(group_openid=...)`**；
  `post_message(channel_id=...)` 是频道用的，发群里会失败。
- `GroupMessage.reply()` 内部就是 `post_group_message(group_openid=…, msg_id=self.id)`。
- **同一条 `msg_id` 回复多次，`msg_seq` 必须递增**，否则后续被静默丢弃。
- 不传 `msg_id` 就是主动消息，需要群里开启「机器人主动在群聊内发言」。
- `group_openid` **不是群号**，只能从事件里捕获（`on_group_add_robot` 或有人 @）。
- `Client.run()` 阻塞，`Client.start()` 是协程。轮询器在 `on_ready` 里
  `self.loop.create_task()` 挂到同一循环。**`on_ready` 断线重连会再跑一次**，
  所以里面的补订阅之类必须幂等。
- `GroupMessage` 用了 `__slots__`，**不能事后挂属性** —— 捡字段要子类 + 加槽位
  （`compat.GroupMessageWithRole` 就是这么干的）。

### QQ 群消息 API

- **群消息 `msg_type` 只有三种：0 文本 / 2 markdown / 7 富媒体。**
  没有合并转发 —— 那是 QQ 客户端协议的特性，官方 bot API 做不到，别再查了。
- **`msg_type=7` 一条消息只能带一个媒体**（`MediaInfo` 只有 `file_info` 一个字段）。
  多图只能连发多条，或在 markdown 里放多个 `![](url)`。
- **`content` 字段渲染吗？** 图片 ✅ / **视频 ❌**（客户端整个丢掉，实测）。
- `file_type`：1 图片 / 2 视频 mp4 / 3 语音 silk / 4 文件（未开放）。
- **发图是两步**：先 `post_group_file(..., srv_send_msg=False)` 拿 `file_info`，
  再 `post_group_message(msg_type=7, media={"file_info": …}, content=…)`。
  `srv_send_msg=True` 会只发图不带文字，且占用主动消息频次。
- **`file_data` 的 5 MiB 上限限的是 base64 之后** → 原图实际只有 3.5 MB 余量
  （`preview.MAX_IMAGE_BYTES`）。**视频那条路不适用**：实测 13.8 MiB 的 base64
  也传上去了，视频的坎是 **10 MiB 原始体积**（`40093011`）和 HTTP `413`。
  **别把这两个数合成一个。**
- ⚠️ **语音（`file_type=3`）上传接口完全不校验格式**：mp3 / wav / 纯垃圾都 HTTP 200
  拿到 `file_info`。**唯一的把关是 silk 魔数闸**（`morning.list_voices` +
  `send_voice` 两道，别去掉任何一道）。
- **markdown 的图必须公网 URL，由 QQ 自己下载转存** → 所以
  `i.ytimg.com` / `pbs.twimg.com` 这类够不到的图床**只能我们自己下载再 base64 上传**。
  「图能不能显示」取决于 **QQ 机房到图床的连通性**，我们控制不了。
- markdown 图片语法 `![alt #Wpx #Hpx](url)`：**尺寸提示必填**（手机端不带 →
  整条都不渲染），**比例必须对**（错了图被拉伸）。`#Wpx` 是**显示尺寸**，
  **QQ 不会放大小图** —— 定多少渲染多少。
- ⚠️ **绝不给图片 URL 加 `_cb=` 之类的 query 参数** —— 加了 QQ 的图片代理就抓不到，
  一张都不渲染。
- ⚠️ **QQ 的图片缓存是服务端、跨群共享的，冷链接第一次必炸**，且和体积强相关
  （1.6 MB 不炸 / 3.4 MB 全炸）。预热必须是**真的发一条 markdown** ——
  `post_group_file` 那条路预热不了渲染缓存。
- `keyboard` **必须包一层 `content`**（`{"keyboard": {"content": {"rows": […]}}}`），
  漏了就是「HTTP 200 但按钮没了」。
- 主动消息频控：发送侧 60 qpm（认证）/ 30 qpm，**群维度 20 qpm**、每群每天 1000 条。
  被动回复 5 分钟内最多 5 次。`50035006` 是通用瞬时错误，不是「QQ 抓不到图」。

### 指令面板 / 自定义菜单（两个不同的东西）

| | 指令面板 `/v2/panels` | 自定义菜单 `/v2/menu` |
|---|---|---|
| 在哪 | 输入 `/` 或 @机器人拉起 | **只在 C2C 单聊底部**，群里不显示 |
| 能嵌套吗 | **不能，是平的** | **能，一层**（`sub_menu_items`，只有一层） |

- **面板不支持嵌套**：塞 `children` / `items` 会 HTTP 200 但被**静默丢弃**，
  必须回读。想给指令分组只能自建按钮菜单或用命名前缀。
- `type` 只有 `command` / `link` 两个值。**`30015` = 这个类型不存在；
  `30016` = 类型存在、只是还差字段** —— 分不清就会把「存在但缺参数」误判成「不支持」。
- 上限：单面板 ≤20 元素、单机器人 ≤20 面板。`name` ≤14 / `desc` ≤30
  （**显示宽度**，中文按 2 —— 用 `len()` 校验会放行超宽的纯中文，到接口才回 30013）。
  `render_data.label` ≤**10 显示宽度**，比面板的 14 还紧。
- **面板的指令名存成裸名**（POST 给 `/sub`，服务端剥成 `sub`）。
  `only_admin=false` 时服务端**省略该字段**，回读成 `None`，比对要 `bool()` 归一。
- `GET /v2/panels` 的 **`scope` 是必填查询参数**（官方文档放在参数表里容易漏，
  漏了返回 30011）；错误码**文档写 `40030011`、运行时返回 `30011`**，要归一化。
- **平台 UI 配的面板和我们 API 建的不是同一份数据**，别把 API 查到的 0 当成「UI 也没配」。

### 这个平台：**「HTTP 200」从来不等于「生效」**

新字段一律**回读或肉眼确认**。已经栽过四次（面板 `children`、`keyboard` 少了
`content` 那层、`only_admin` 回读成 `None`、UI 面板与 API 面板不同源）。

### 日志：失败路径一律不许写 DEBUG

判据是「这行日志只在某件罕见的事发生时出现」—— 那种日志写 DEBUG 等于没写。
踩过的代价：线上群里发了链接毫无反应，日志里**也毫无痕迹**，排查只能靠猜。

| 位置 | 级别 |
|---|---|
| 认出分享 / 成功处理 | INFO（频率极低，是正常路径的痕迹） |
| **认出了但没处理成** | **WARNING**（「有人分享了我们没回」——必须看得见） |
| 内部各步放弃的原因 | INFO |
| 异常栈 | WARNING（带 `exc_info`） |
| 「忽略非 @ 消息」 | DEBUG（量大且无信息量） |

**群里看不见的失败必须私聊告警**（`notify.C2CNotifier`，同类按 key 冷却 30 分钟）。
典型：配图失败、cookie 失效 —— 症状都是「今天什么都没有」，和「今天真的没有」
长得一模一样。

## 约定

- 注释和用户可见文案用中文，代码标识符用英文。
- **推送顺序不能反：先推送，成功了才 `mark_seen`**（at-least-once）。
  `db.seen_keys()` 只查不写，`db.mark_seen()` 才写；`filter_new_entries`
  只留给「不推送、只标记」的场景（首抓 baseline、`/check`）。
- **`asyncio.gather(..., return_exceptions=True)` 的返回值必须检查** ——
  不检查的话异常被静默吞掉，推送失败连一行日志都没有。
- **推送是逐条发的，不是合并摘要**（一条图文只能带一张图，合并就带不了图）。
  文案在 `scheduler.format_entry`，`_MAX_CONTENT = 1500`；间隔 `_PUSH_GAP_SEC`。
- `FeedPoller` 的推送出口是 `Pusher` 协议（`send_text` / `send_rich`），
  `RssBot` 自己实现它。测试里换成假实现就不碰 QQ。
- **`QuotaExceeded` 必须穿透 `except Exception`**（它是 RuntimeError，
  会被当成「配图失败」吞掉，然后每条再试一遍纯文字，什么都没解决）。
- 数据库 schema 改动写在 `db.SCHEMA` 里（`executescript` 建表，加新表老库自动补）。
  **加列**走 `db._MIGRATIONS`（`PRAGMA table_info` 查一下、缺了才 `ALTER TABLE ADD COLUMN`）。
  改类型 / 删列 / 改主键仍然要手工处理已有 `data/bot.db`。
- **每天一次的功能用「本地时间」，涉及日本截止时刻的用 JST。**
  项目里 JST 只有一份（`tickets.JST`），别再造一个。
- **多群推送：选人 / 抓图 / 挑选都在循环外**，一个 slot 所有群发同一份内容。
- **逐群隔离**：一个群发不出去不连累别的群，**失败的群不记账**（窗口内下一拍重试）。

## 功能索引 → `docs/`

需要动某个功能时**先读对应那份**，别只看着代码猜。

| 功能 | 代码 | 文档 |
|---|---|---|
| **开发 · 仓库 · 部署 · 迁移（一份看完）** | `docker-compose.yml` | [`docs/dev-workflow.md`](docs/dev-workflow.md) |
| 平台踩坑总集（botpy / 面板 / 分享卡片） | —— | [`docs/pitfalls.md`](docs/pitfalls.md) |
| 早期现场验证结论 | —— | [`docs/platform-notes.md`](docs/platform-notes.md) |
| `/rss` 按钮菜单与进群自动订阅 | `menu.py` `autosub.py` | [`docs/menu-panel.md`](docs/menu-panel.md) |
| 群里分享 bilibili 链接回卡片 | `bilibili.py` | [`docs/preview-bilibili.md`](docs/preview-bilibili.md) |
| 群里分享 YouTube / X 链接 | `preview.py` | [`docs/preview-yt-x.md`](docs/preview-yt-x.md) |
| 多图合并 markdown | `multiimage.py` | [`docs/preview-yt-x.md`](docs/preview-yt-x.md) |
| AI 摘要（抓正文 + 翻译/精简） | `ai.py` | [`docs/ai-digest.md`](docs/ai-digest.md) |
| 偶像大师 NEWS 企划 / 分类标签 | `idolmaster.py` | [`docs/imas-tags.md`](docs/imas-tags.md) |
| ASOBI TICKET 抽票提醒 | `tickets.py` | [`docs/tickets.md`](docs/tickets.md) |
| 月曜が近いよ（周播视频） | `monday.py` | [`docs/monday.md`](docs/monday.md) |
| 小偶像的每日早安语音 | `morning.py` | [`docs/morning.md`](docs/morning.md) |
| 偶像生日推送（23:05） | `birthday.py` | [`docs/birthday.md`](docs/birthday.md) |
| 誕生祭 贺图聚合（22:30） | `birthday_art.py` | [`docs/birthday-art.md`](docs/birthday-art.md) |
| 限流、配额与出错通知 | `quota.py` `notify.py` | [`docs/quota-notify.md`](docs/quota-notify.md) |
| 群复读机（连着两个人发同样的话就跟一句） | `repeat.py` | [`docs/repeat.md`](docs/repeat.md) |
| **按群的功能开关**（群列表存库，`.env` 里没有群参数；`/set` 面板） | `features.py` `db.group_features` `commands._set` | [`docs/group-features.md`](docs/group-features.md) |

## Agent skills

工程技能（`/to-tickets`、`/triage`、`/to-spec`、`/domain-modeling` 等）的逐仓库配置。

### Issue tracker

issue 和 spec 都是 **GitHub issue**（`JackLin513/qq-rss-bot`，用 `gh` CLI 读写）。
见 `docs/agents/issue-tracker.md`。

### Triage labels

五个分类角色用**默认标签名**：`needs-triage` / `needs-info` / `ready-for-agent` /
`ready-for-human` / `wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

**单上下文**：仓库根目录 `GLOSSARY.md` + `docs/adr/`。两者**都还没建** ——
需要时才由 `/domain-modeling` 创建，别提前造。
⚠️ 和本文件「功能索引」里那 15 份 `docs/*.md` 是两回事（那些是各功能的
来龙去脉）。见 `docs/agents/domain.md`。
