# CLAUDE.md

QQ 群 RSS 订阅机器人。单个 Python 进程：botpy 维持 QQ 长连接，同一个事件循环里跑 RSS 轮询。

## 常用命令

```bash
uv sync
uv run qq-rss-bot                              # 启动（需要 .env）
uv run python scripts/smoke.py                 # 62 项离线+抓取测试
uv run python scripts/integration.py           # 15 项端到端（假推送，不碰 QQ）
uv run python scripts/probe_events.py 20       # 事件探针（先停掉正式实例）
uv run python scripts/check_sources.py         # 复测 RSSHub 各路由可用性
uv run python scripts/test_push.py             # 测主动消息（不占 WS 会话，可并行）
```

## 运行平台

开发在 macOS（宿主原生跑 + Docker Desktop 跑 RSSHub），长期运行在**群晖 DS220+**
（两者都容器化，x86_64）。

跨平台只改部署形态，代码不动。两个必须记住的差异：

- **容器里 `RSSHUB_BASE` 要用服务名 `http://rsshub:1200`**，不是 `127.0.0.1`
  （容器里的 loopback 是容器自己）。
- **代理只给 RSSHub，绝不能给 bot**：bot 要访问 `http://rsshub:1200` 这个内部服务名，
  走代理必然失败；且 `api.sgroup.qq.com` 是国内站直连更好。
  v2ex / github 这类上游被 DNS 污染，只有 RSSHub 需要代理。

目标机实机实测（规格、内存账、容器网络可达性、迁移坑）见
`docs/synology-migration.md`——那份是实机数据，别按规格重新推算。

## 已验证的 botpy 1.2.1 事实（不要再凭印象改）

这些是读 `.venv` 里源码确认过的，写代码时直接信：

- `botpy.Intents(public_messages=True)` → bit 25，覆盖 `on_group_at_message_create`、
  `on_c2c_message_create`、`on_group_add_robot`、`on_group_del_robot`、`on_group_msg_reject`。
  频道的 `public_guild_messages` 是 bit 30，两套互不相干。
- 派发规则是 `method = "on_" + event`（`client.py:256`），没有显式注册表。
- **发群消息必须用 `api.post_group_message(group_openid=...)`**，
  `post_message(channel_id=...)` 是频道（guild）用的，发群里会失败。
- `GroupMessage.reply()` 内部就是 `post_group_message(group_openid=self.group_openid, msg_id=self.id)`。
- **同一条 `msg_id` 回复多次，`msg_seq` 必须递增**，否则后续消息被静默丢弃。
  `bot.py::_make_replier` 自己维护计数器，就是为了这个。
- 不传 `msg_id` 就是主动消息，需要群里开启「机器人主动在群聊内发言」权限。
- `group_openid` **不是群号**，只能从事件里捕获：被拉群时 `on_group_add_robot`，
  或群里有人 @ 时。持久化在 `groups` 表。
- `Client.run()` 阻塞，`Client.start()` 是协程。轮询器在 `on_ready` 里
  用 `self.loop.create_task()` 挂到同一循环。

## 踩过的坑

- **日志必须用 `basicConfig(force=True)`**。`import botpy` 时 `botpy/logging.py` 在模块级
  就执行了一次 `logging.basicConfig()`；此后再调 `basicConfig` 因 root 已有 handler 而**变成空操作**，
  结果本项目所有 `INFO` 日志被静默丢弃（root 停在 WARNING）。
  症状极具误导性：`on_ready` 其实跑了、轮询器其实启动了，但日志里一片空白，
  看起来像「事件没派发」。排查这类问题先用 `scripts/probe_events.py`，
  它直接打印 botpy 派发的每个事件，别靠猜。
- **别用 `self.http` 命名自己的 HTTP 客户端**。`botpy.Client.__init__` 已经占用了
  `self.http`（`botpy.http.BotHttp`），覆盖之后 `Client.close()` 会在
  `await self.http.close()` 处抛 `AttributeError`。本项目用的是 `self.rss_http`。
- **`rsshub.app` 官方公共实例实测返回 HTTP 403**（2026-10），不能作为默认值。
  默认指向自建 `http://127.0.0.1:1200`，`scripts/check_sources.py` 可以复测各源可用性。
- **Python 版本锁 3.11–3.12**。botpy 1.2.1 依赖链较老，本机默认的 3.14 没验证过。
- **群消息事件是 `GROUP_MESSAGE_CREATE`，不是 `group_at_message_create`**。
  QQ 2026 新协议下平台推全量群消息，botpy 1.2.1 无此解析器会静默丢弃。
  `compat.py` 负责补上；`bot.py` 里两个 handler 都要保留（老协议可能仍被启用）。
  报文**没有 mentions 字段**，@ 是明文混在 content 里的，只能字符串匹配。
- **发图是两步**：先 `post_group_file(group_openid, file_type=1, url=图片URL, srv_send_msg=False)`
  拿 `file_info`，再 `post_group_message(msg_type=7, media={"file_info": ...}, content=...)`。
  `url` 传的是**图片地址，QQ 服务器自己去下载**，不需要我们先下来再上传。
  `file_type`：1 图片 / 2 视频 mp4 / 3 语音 silk / 4 文件（未开放）。
  `srv_send_msg=True` 会只发图不带文字，且占用主动消息频次；要「图文一起」必须走两步。
  返回的 `file_info` 有 `ttl`（实测 86400s），过期失效。
  实测 idolmaster 那个源（图片在日本的 CMS 上）QQ 能正常拉到，HTTP 200。
- **图片 URL 可能把扩展名藏在 query 里**。idolmaster 的图是
  `.../Image/get?path=/idolmaster/jp/article/.../xxx.jpeg`，所以抽取图片的正则
  必须连着 query 一起匹配，不能只匹配到 `\.jpg` 就停。
- 首抓必须只建 baseline 不推送，否则订阅瞬间倒灌几十条旧文。
- 自建 RSSHub 默认镜像不含浏览器，且部分路由需要额外环境变量：
  GitHub 系路由要 `GITHUB_ACCESS_TOKEN`（否则 503 ConfigNotFoundError），
  weibo/bilibili 等要换 `diygod/rsshub:chromium-bundled` 镜像。
  给用户推荐路由前先用 `scripts/check_sources.py` 实测，别照文档抄。
- 本地 RSSHub 地址是 `http://127.0.0.1:1200`（容器名 `rsshub`，`--restart unless-stopped`）。
- **别用 grep/cat 打印 `.env`**：它含真实凭证，输出会进入会话记录。
  要检查只列键名，见本项目 README 的做法。

## 现场验证过的结论（2026-10-16）

在真实群 `E521581EF8167F682267A1A50074C056` 上跑通的链路：

- 被动回复 `/ping`：成功（无报错）
- `@` 过滤：发 `111` 被正确忽略，日志 `忽略非 @ 消息: '111'`
- `/sub /v2ex/topics/hot`：成功，抓到 title 并建立 baseline（`initialized=1`）
- **主动消息：HTTP 200 成功**，群内「机器人主动在群聊内发言」开关是开的

## 约定

- 注释和用户可见文案用中文，代码标识符用英文。
- **推送顺序不能反：先推送，成功了才 `mark_seen`**。语义是 at-least-once。
  曾经用 `filter_new_entries`（查的同时就写入已见）在推送**之前**标记，
  结果网络一断，那批条目已算见过、永远不再推 —— 静默丢消息且无任何日志。
  所以 `db.seen_keys()` 只查不写，`db.mark_seen()` 才写；
  `filter_new_entries` 只留给「不推送、只标记」的场景（首抓 baseline、/check）。
- **`poll_once` 里 `asyncio.gather(..., return_exceptions=True)` 的返回值必须检查**。
  不检查的话异常被静默吞掉，推送失败连一行日志都没有。
- **推送是逐条发的，不是合并摘要**。原因：一条图文消息只能带一张图
  （官方 `media` 只收单个对象，传数组 400，实测过），合并就带不了图。
  文案改动看 `scheduler.format_entry`，长度上限 `_MAX_CONTENT = 1500`；
  逐条之间的间隔是 `_PUSH_GAP_SEC`（避免撞群维度 20 qpm 频控 + 稳定到达顺序）。
- `FeedPoller` 的推送出口是 `Pusher` 协议（`send_text` / `send_rich`），
  `RssBot` 自己实现它。测试里换成假实现就不碰 QQ，见 `scripts/smoke.py`。
- 配图抽取在 `rss.first_image()`，必须在 `_clean_text` 剥标签**之前**做，否则 URL 就没了。
- 数据库 schema 改动写在 `db.SCHEMA` 里；目前没有迁移机制，
  表结构变更需要手动处理已有 `data/bot.db`。
