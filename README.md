# qq-rss-bot

QQ 群 RSS 订阅机器人：群里 `@机器人 /sub <RSSHub路由>`，之后有新文章自动推到群里。

## 技术栈与选型理由

| 部分 | 选择 | 为什么 |
|---|---|---|
| 机器人框架 | **qq-botpy（官方 Python SDK）** | QQ 官方机器人没有通用协议，只能走官方 API。Python SDK 是官方维护里最省事的：WebSocket 长连接本地就能跑，不需要公网 IP 和内网穿透 |
| RSS 解析 | **feedparser** | 纯 Python，RSS 0.9x/1.0/2.0/Atom 全兼容，且对畸形 XML 极其宽容（真实世界的源大多不干净） |
| HTTP | **httpx** | 异步，方便和 botpy 共用一个事件循环；超时/重定向控制清晰 |
| 存储 | **SQLite（标准库）** | 单文件、零运维、单机机器人够用。去重表增长有上限，见 `prune_seen` |
| 调度 | **自己写的 asyncio 轮询** | 每 30s 查一次「哪些订阅到点了」。不引 APScheduler 是因为间隔状态在数据库里，重启不需要重建任务，比 cron 式调度更好恢复 |
| 依赖管理 | **uv** | `uv sync` 一把梭，锁定版本 |

**为什么不用 Node/Java/Go**：这三个官方也有 SDK，协议完全一样。但这个项目 80% 的复杂度在 RSS 解析的脏数据容错上，feedparser 在这个环节的优势很明显；而 botpy 已经把 WebSocket 重连、鉴权、心跳全处理好了，换语言省不下东西。

## 快速开始

```bash
uv sync
cp .env.example .env      # 填入 AppID / AppSecret
uv run qq-rss-bot
```

### 1. 部署 RSSHub（必做）

**官方公共实例 `rsshub.app` 现在对本项目返回 HTTP 403，直接用不了**（2026-10 实测）。自建：

```bash
docker run -d --name rsshub -p 1200:1200 diygod/rsshub
```

`.env` 里填 `RSSHUB_BASE=http://127.0.0.1:1200`。
想临时验证可以先用镜像 `https://rsshub.rssforever.com`（实测可用，但公共实例随时可能挂）。

### RSSHub 路由可用性（实测）

自建 RSSHub 默认只带「不需要额外配置」的路由。实测结果：

| 路由 | 结果 |
|---|---|
| `/hackernews/best`、`/v2ex/topics/hot`、`/sspai/index`、`/solidot/linux`、`/36kr/newsflashes`、`/ithome/it`、`/readhub/daily`、`/douban/movie/playing`、`/juejin/category/frontend`、`/infoq/recommend` | ✅ 开箱可用 |
| `/github/trending/*` 等 GitHub 路由 | ❌ 503 `ConfigNotFoundError` —— 需要给容器加 `GITHUB_ACCESS_TOKEN` |
| `/weibo/user/*`、`/bilibili/hot-search` 等 | ❌ 503 —— 需要浏览器，换成 `diygod/rsshub:chromium-bundled` 镜像 |
| `/zhihu/hotlist` | ❌ 404 —— 路由已被上游删除，换别的知乎路由 |

加 GitHub token 的做法：

```bash
docker rm -f rsshub
docker run -d --name rsshub --restart unless-stopped -p 1200:1200 \
  -e GITHUB_ACCESS_TOKEN=ghp_xxx diygod/rsshub
```

用 `uv run python scripts/check_sources.py` 可以随时复测路由可用性。

### 2. 开放平台配置（q.qq.com → 你的机器人）

- **开发设置** → 复制 `AppID` / `AppSecret` 到 `.env`
- **事件订阅** → 勾选**群聊@机器人消息**（群聊管理类事件可选，用于记录群）
- **沙箱配置** → 填测试群，未上架的机器人只在这些群生效
- **权限** → 开启**「机器人主动在群聊内发言」**。不开的话只能被动回复，定时推送会全部失败

### 3. 验证

```bash
uv run python scripts/smoke.py        # 62 项：解析/去重/调度/配图/真实抓取
uv run python scripts/integration.py  # 15 项：端到端（真抓源，假推送，不碰 QQ）
```

## 运行平台

同一套代码支持两种跑法，**差异只在部署形态，代码不用改**（`RSSHUB_BASE` 已是环境变量）。

| | macOS（开发调试） | 群晖 Synology x86_64（长期运行） |
|---|---|---|
| 机器人 | 宿主原生 `uv run qq-rss-bot` | 容器（compose） |
| RSSHub | Docker Desktop | 容器（compose） |
| `RSSHUB_BASE` | `http://127.0.0.1:1200` | `http://rsshub:1200` ← 服务名 |
| 出网代理 | 跟随系统代理 | 需显式配置，见下 |

架构要求 **x86_64**（`diygod/rsshub` 的 amd64 镜像原生运行，无需模拟）。
任何 x86_64 Linux 主机同理，不只群晖。

**为什么地址要改**：容器里的 `127.0.0.1` 是容器自己，不是宿主机。两个服务都进容器后，
bot 必须用 Docker 服务名访问 RSSHub。忘了改的症状是抓取全部超时。

**代理只给 RSSHub，绝不能给 bot**：部分上游（实测 `www.v2ex.com`、`github.com`）
国内直连不通——v2ex 的 DNS 被污染，解析到 Facebook 的 IP 段——需要走代理。
但只有 RSSHub 需要；国内站走代理实测没有延迟代价，所以可以全局走。

```yaml
  rsshub:
    environment:
      - HTTP_PROXY=http://<你的代理地址>:6152
      - HTTPS_PROXY=http://<你的代理地址>:6152
      - NO_PROXY=localhost,127.0.0.1
  bot:
    environment:
      - RSSHUB_BASE=http://rsshub:1200
      - HTTP_PROXY=          # 显式清空，防止容器级代理被继承进来
      - HTTPS_PROXY=
      - NO_PROXY=localhost,127.0.0.1,rsshub,api.sgroup.qq.com,bots.qq.com
```

**bot 一旦走代理，`http://rsshub:1200` 就会失败**——那是容器内部的服务名，代理解析不了。
而且 bot 的命脉 `api.sgroup.qq.com` 是国内站，直连更快更稳，没必要多一层依赖。

详细评估（目标机实机规格、内存账、容器网络实测、四个迁移坑）见
[`docs/synology-migration.md`](docs/synology-migration.md)。

## 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `/sub <路由或URL> [间隔分钟]` | 群主/管理员 | 订阅，默认 15 分钟。RSSHub 路由可省略域名，如 `/36kr/newsflashes` |
| `/unsub <编号>` | 群主/管理员 | 取消订阅 |
| `/list` | 所有人 | 本群订阅列表 |
| `/check <编号>` | 所有人 | 立刻抓一次，验证路由是否还有效 |
| `/ping` | 所有人 | 存活检测 + 统计 |
| `/help` | 所有人 | 帮助 |

**编号是「本群内的位置」，不是数据库 id。** `/list` 永远显示 1、2、3…，
删掉一条后面的自动补位，新群第一条就是 1。
（曾经直接打数据库主键 —— 那是全局自增、跨群共享的，
会让一个从没订阅过的新群把第一条订阅显示成「5.」，删一条还留下空号。）
编号会随订阅增删而变化，所以 `/unsub` 的回复里会带上被删掉那个源的名字，
万一列表在你 `/list` 之后变过，一眼就能看出删错没有。

**订阅是按群共享的。** `subscriptions` 表以 `group_openid` 为作用域：群里任何人订阅，
全群都会收到推送；群与群之间完全隔离。QQ 官方群机器人**不支持**把主动消息定向给
某个成员，所以「全群共同接收」是平台约束，不是实现选择。

**别名与容错**：`help`/`帮助`/`?`、`sub`/`subscribe`/`订阅`、`unsub`/`取消`、`list`/`列表`；
斜杠可省略（`@机器人 sub /x` 也认）；只 @ 不写字直接回帮助；未知指令仅在以 `/` 开头时才回
（避免群里闲聊被插嘴）。

**权限**：`/sub` 与 `/unsub` 默认仅群主和管理员可用，角色取自报文里的
`author.member_role`（botpy 会丢弃它，由 `compat.py` 的补丁补回）。
想放开就设 `RESTRICT_ADMIN_COMMANDS=false`。角色取不到时会**放行并记警告**——
宁可漏管，也不能让机器人整个不可用。

### 指令面板（客户端输入框打 `/` 弹出）

官方支持配置「指令面板」，用户在 QQ 里打 `/` 会看到可用指令，点击即填入输入框。

- **手工配**：开放平台 → 发布设置 → 功能配置 → 指令配置面板
  - 配完**必须扫码确认修改**，只填不确认 = 不生效（API 查也是 0 个面板，
    客户端打 `/` 显示「这里还没有指令」）
  - 「使用场景」要勾 **QQ群**，**不能勾「消息列表」**
  - 已提交审核的指令改不动，要先删除再重新配置
- **脚本配**（可复现、可进版本控制）：

```bash
uv run python scripts/setup_panel.py --list      # 只读，先看现有的
uv run python scripts/setup_panel.py --create    # 创建群聊面板
uv run python scripts/setup_panel.py --update <panel_id>
uv run python scripts/setup_panel.py --delete <panel_id>
```

脚本内置官方字数校验（元素名 ≤14、描述 ≤30、单面板 ≤20 元素）。
⚠️ 官方限制的是**显示宽度不是字符数**，中文按 2 算 —— 16 个汉字的描述宽度已 32，
按字符数校验会放行，到接口才回 `30013`（而这个码文档写的是「超出数量限制」，
很容易误判成元素太多）。`validate()` 用 `_width()` 按官方口径算。

**实测的面板数据形态**（服务端的行为，别按直觉假设）：

| | 服务端实际存/返的 |
|---|---|
| 指令名 | **裸名**，`/sub` 会被剥成 `sub`；客户端点击时自己补 `/` |
| `only_admin` | 为 `false` 时**整个字段省略**，回读是 `None`（不是 `false`） |
| 描述 / 顺序 / `target_type` | 原样保留 |

`compare_items()` 对前两项做了归一化。不归一的话会报出
「缺少 /sub、多出 sub」「list 的 only_admin 不一致」这类**假差异**，
把真正的问题淹掉。
并会先发一条只读的 `GET /v2/panels?scope=group` 确认 host 与参数，再动任何东西。

⚠️ 指令面板是较新的能力（约 2026-08 起），**需要较新版 QQ 客户端**才显示，
手机版优先、桌面版可能延后。**「接口成功但客户端看不到」属于灰度期的正常现象**，不是配错了。

选中「每条单独发一条图文」而不是「多条合并成一条摘要」，是因为**一条图文消息只能带一张图**：
官方 `media` 字段只接受单个对象，传数组直接 `400 请求数据异常`（实测确认）。
合并摘要就带不了图，所以改成逐条推送。

三个关键设计：

1. **首抓只建立 baseline，不推送**。否则一订阅就把该源最近 20 篇全倒进群里。
   去重靠 `seen_entries` 表 + 条目 guid（退化到 link，再退化到标题+时间哈希）。
2. **失败退避**。抓取失败的订阅同样更新 `last_polled_at`，等于退避一个间隔再重试，
   错误记在 `last_error`，`/list` 里会带 ⚠ 显示。
3. **配图失败自动退回纯文字**。图片抠不到、或 QQ 拉不到那张图（源站挂了、防盗链、太大），
   都不会让这条新闻丢失，会降级成纯文字再发一次。

## 推送配图

条目正文里的第一张图会被抠出来当配图（`rss.first_image()`），支持 RSS 的
`description` 和 Atom 的 `content[]`，也能处理扩展名藏在 query 里的 URL。

- 发图是**两步**：先 `post_group_file(url=图片地址)` 拿 `file_info`，再
  `post_group_message(msg_type=7, media=...)` 发出去。
- **QQ 服务器自己去下载图片**（我们只给 URL），所以图片必须公网可达。
- 各源带图情况差别很大，实测：`/idolmaster/news` 12/12 条有图、
  `/v2ex/topics/hot` 2/9、`/36kr/newsflashes` 0/20。没图的源自动走纯文字，不用管。
- 关掉配图：`.env` 里 `ATTACH_IMAGES=false`，全部退回纯文字推送。

## 工作方式

```
QQ 群 @机器人 ──▶ GROUP_MESSAGE_CREATE ──▶ @ 过滤 ──▶ CommandHandler ──▶ SQLite
                                                                        │
FeedPoller（每 30s 扫一次到期的订阅）──▶ fetch_feed ──▶ 增量去重 ──▶ 逐条主动推送
                                                                    （有图走图文）
```

## QQ 2026 新协议（重要）

QQ 开放平台 2026 年起对群聊改用 **`GROUP_MESSAGE_CREATE`** 事件，`botpy 1.2.1` 不认识它，
网关会打一行 `ERROR botpy | _parser unknown event group_message_create.` 然后**把事件丢掉**。
症状是「机器人连上了、日志显示启动成功，但永远收不到任何消息」。
`src/qq_rss_bot/compat.py` 在建立连接前给 botpy 补上了解析器。

这个协议有三个后果：

1. **平台推的是全量群消息**，不是只推 @ 消息。群里任何人闲聊都会推过来（实测发「111」也推），
   所以 `bot.py` 必须自己筛 @，否则机器人会对着所有聊天乱回。
2. **报文里没有 `mentions` 字段**。实测 payload 的 `d` 里只有
   `author / content / group_id / group_openid / id / message_scene / message_type / timestamp`，
   @ 是以明文 `@显示名` 混在 `content` 里的。因此 `is_bot_mentioned()` 只能退化到字符串匹配。
3. 被动回复所需的 `msg_id`（`d.id`）仍在，`message.reply()` 正常工作。

**@ 有三种形态，都得认**：

| 形态 | 出现在 | 例子 |
|---|---|---|
| 显示名明文 | 手工 @ 机器人 | `@猪排爱好者 /ping` |
| `openid` 占位符 | **指令面板按钮** | `<@C371C51BA906A22E4...> /help` |
| 老数字占位符 | 老协议 | `<@!123456> /ping` |

中间那种最容易踩：占位符里是 **32 位十六进制 openid**，不是 QQ 号。
正则若写成 `<@!?\d+>`（只认数字）就一条都匹配不上，
症状恰恰是「**面板完美显示，点了却毫无反应**」——而且丢在 DEBUG 级日志里，
从外面完全看不出原因。排查这类问题先把 `LOG_LEVEL=DEBUG` 打开，
`compat.py` 会把原始报文打出来。

**已知局限**：两种误判，都源于拿不到机器人自己的 openid 做精确比对——
1. 显示名前缀：机器人叫 `bot` 时，`@bot2 hi` 会被误触发；
2. 占位符：`<@某队友openid> ping` 这种「@ 别人 + 恰好跟一个裸指令名」会被误触发。

代价可接受（比漏掉面板消息好），且 `@ 别人 + 闲聊` 不会被误触发。
要彻底规避就在群设置里把「机器人可获取的群聊消息范围」改回「仅接收 @ 机器人消息」。要规避就在群设置里把
「机器人可获取的群聊消息范围」改回「仅接收 @ 机器人消息」，这样平台只推 @ 消息，
机器人就不需要自己筛了。

## 主动消息（RSS 推送的前提）

RSS 机器人的核心是「没人说话时主动推一条」，这依赖两件事：

1. **群内开关**：手机 QQ → 群设置 → 群机器人 → 你的机器人 → 打开
   「**机器人主动在群聊内发言**」。不开的话被动回复一切正常，但定时推送**全部静默失败**。
2. **机器人必须在线连着 gateway**。官方文档明确要求
   「发送消息接口要求机器人需要链接到 websocket gateway 上保持在线状态」，
   所以机器人进程不能停。

验证方式（不占用机器人的 WebSocket 会话，可并行跑）：

```bash
uv run python scripts/test_push.py                          # 推一条文字
uv run python scripts/test_push.py --route /idolmaster/news  # 从路由取首图推图文
uv run python scripts/test_push.py --image <图片URL>
```

**图片推送**同样验证过（HTTP 200）。QQ 支持直接给图片 URL，由 QQ 服务器自己去下载，
不需要本地中转，代价是多一次 `/files` 上传调用。

实测这个群是通的，返回 HTTP 200。官方频控：群聊认证后 60 qpm（机器人维度）、
20 qpm（单群维度）、**每群每天 1000 条**，对 RSS 推送完全够用。

## 可靠性

**推送语义是 at-least-once：先推送，成功了才把条目标记为已见。**

这样才有「网络一断也不丢新闻」的性质——推送失败的条目留到下个周期重试
（代价是延迟，不是丢失）。曾经的反例是在推送**之前**就标记已见，
结果网络中断时那批新闻已算见过、永远不再推，且没有任何日志。
`scripts/smoke.py` 里的 `[4.56]` 就是这条的回归测试。

只有**超出 `MAX_ITEMS_PER_PUSH` 上限**的条目是「有意丢弃」——那批直接标记已见，
不会反复重捞（否则一批来 50 条会永远卡在前 5 条上）。

**但这个机器人的可靠性等于它所在机器的可靠性。** 实测一小时内的运行环境：

| 现象 | 次数 | 影响 |
|---|---|---|
| DNS 解析失败（`api.sgroup.qq.com`） | 8 | 推送失败，下轮重试 |
| WebSocket 异常断开（1006） | 4 | botpy 自动重连，全部成功 |

网络断的同时事件循环还可能整个停摆：实测有个订阅 15:25 到期，直到 15:29 才被抓
（日志里伴随一次 1006 断开）。**笔记本合盖睡眠会冻结事件循环**，
这期间不抓取也不推送，醒来后才补。

所以：**要 7×24 稳定推送，别放在会睡眠的笔记本上**，换一台常开的机器
（VPS / 家里的服务器 / 树莓派）。放笔记本上就只能接受「醒着且网络好时才推」。

## 已知限制

- **主动消息配额**：群聊主动消息需要权限；私聊（C2C）按官方文档是
  **每用户每天 1000 条**（另有第三方文档说每月 4 条，与官方不符）。
  本项目只做群聊，不涉及 C2C。
- **`group_openid` 不是群号**，且只能从事件里拿。所以：
  - 机器人被拉群时通过 `on_group_add_robot` 立刻记录；
  - 如果该事件没收到，等群里有人 `@` 一次即可补上。
- **Python 版本**：锁在 3.11–3.12。3.14 上 botpy 1.2.1 未经验证（其依赖链较老）。
- 文本消息有长度上限，推送内容截断在 1500 字符。

## 目录

```
src/qq_rss_bot/
  config.py      环境变量 → Config
  db.py          SQLite：订阅表 + 去重表
  rss.py         RSSHub 抓取与解析
  scheduler.py   轮询、增量判定、推送格式化
  commands.py    群指令解析与执行
  bot.py         botpy 客户端，事件 → 指令/轮询
scripts/
  smoke.py       离线逻辑 + 真实抓取
  integration.py 端到端（假推送，不碰 QQ）
```
