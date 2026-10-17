# CLAUDE.md

QQ 群 RSS 订阅机器人。单个 Python 进程：botpy 维持 QQ 长连接，同一个事件循环里跑 RSS 轮询。

## 常用命令

```bash
uv sync
scripts/botctl.sh start                        # 启动（后台，日志 /tmp/qq-rss-bot.log）
scripts/botctl.sh restart                      # 重启 —— **改完代码必须重启**，没有热加载
scripts/botctl.sh status                       # 看实例（多于 1 个就是出问题了）★
uv run qq-rss-bot                              # 前台启动（调试用，Ctrl+C 可停）
uv run python scripts/smoke.py                 # 323 项离线+抓取测试
uv run python scripts/integration.py           # 15 项端到端（假推送，不碰 QQ）
uv run python scripts/probe_events.py 20       # 事件探针（先停掉正式实例）
uv run python scripts/probe_panel.py           # 指令面板 schema 探针（自建自删，可复跑）
uv run python scripts/check_sources.py         # 复测 RSSHub 各路由可用性
uv run python scripts/test_push.py             # 测主动消息（不占 WS 会话，可并行）
uv run python scripts/setup_panel.py --list    # 指令面板：查询（只读，先跑这个）
uv run python scripts/setup_panel.py --create  # 指令面板：创建
```

## 测试消息只发测试群或私聊（硬性）

需要往真实 QQ 发消息来验证什么（按钮渲不渲染、推送能不能出去）时，
**只允许发到机器人测试群（群号 575861535）或者私聊用户本人**。**绝不发正式群。**

注意**群号和 `group_openid` 不是一回事**，库里 `groups` 表存的是 openid，
拿群号查不出来 —— 所以「先查一下是不是测试群」这条做不到，只能靠事先确认。
拿不准就把命令交给用户让他自己跑，别自己往群里发。

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

### 部署到群晖（2026-10-17）

代码在 `/volume1/docker/qq-rss-bot/`，`docker-compose.yml` 起 `rsshub` + `bot` 两个容器。

- **GUI 和命令行不能交叉管同一个项目 —— 会出现「幽灵容器」。** 症状是
  Container Manager 里多出一个点不开的条目，提示「不存在」，所有操作都
  `failed`。成因：容器被 GUI 之外的东西重建过（比如命令行的 `up -d`），
  GUI 记录里还存着旧 ID。
  **判据在 Container Manager 的事件日志里**（GUI 可导出 CSV）：
  | 日志形态 | 含义 |
  |---|---|
  | `Signal container qq-rss-bot-rsshub-1.` | 记录正常 |
  | `Signal container 81e9c6491674_qq-rss-bot-bot-1 failed.` | **记录过期** —— `81e9c6491674` 是容器 ID 前 12 位，那个「奇怪字母前缀」就是它 |
  **修法：GUI 项目 → 停止 → 启动**（项目级操作会重新扫描容器，把 ID 刷回来）。
  实测有效，**不用删项目** —— 删项目有碰到 `data/` 的风险，别轻易走那步。
  根治就是上面那条：同一个项目只用一个入口。
- ⚠️ **GUI 的「构建」不可信**：事件日志里连着五次 `Build project 'qq-rss-bot'
  successfully.`（11:06 / 13:06 / 13:52 / 16:40 / 17:28），而镜像时间戳一直停在
  **09:56:40**、容器里 `bot.py` 的 mtime 一直是 **09:08** —— 一次都没真重建。
  判据同上：`COPY` 保留源文件 mtime，所以 `docker exec … ls -l
  /app/src/qq_rss_bot/bot.py` 的时间就是**构建时**源码的时间。
  **挂了 `./src` 之后这条不再要紧**（不需要构建了），但别拿「构建成功」当依据。
- **日常改代码走热更新，不用重建镜像。** compose 里挂了 `./src:/app/src:ro`
  （`uv sync` 装的是**可编辑版**，`.pth` 指向 `/app/src`，容器是运行时去那儿
  import 的），所以改完代码只要：
  ```bash
  sudo docker compose restart bot     # 约 2 秒
  ```
  **改 `docker-compose.yml` 本身**（比如加这个挂载）才需要 `up -d` 一次。
  改 `src/` 下的代码**永远不用重建**。
- **配套的判据：启动日志里的 `运行代码 …（最新改动 <时间>）`**
  （`__main__.code_identity`）。它报的是包内 .py 的**最新** mtime：
  - 那个时间 = **你刚保存的时间** → 代码生效了
  - 那个时间 = **镜像构建时间** → 改的东西没进去

  **这条日志是热更新的前提，别删。** 挂了卷之后「镜像 ≠ 正在跑的代码」，
  而 2026-10-17 那次事故的本质就是这种不一致**完全没有提示**。
- ⚠️ **改完代码必须 `build` + `up` 两步，只 `up` 等于什么都没做。**
  （只在你**没挂** `./src` 时适用；挂了卷就走上面那条。）
  ```bash
  cd /volume1/docker/qq-rss-bot
  sudo docker compose build bot && sudo docker compose up -d
  ```
  **`docker compose up -d` 不会构建**，它只会用现有镜像重建容器。Container
  Manager 图形界面的「清除 + 重新构建」实测同样**不会重建镜像** —— 它等价于
  `up -d`：容器被删了、用旧镜像重开一个，镜像时间戳一动不动。
  代价是**完全静默**：没有报错、日志看起来也正常（`已上线`、`轮询启动` 都在），
  只是新代码一行都没生效。实测为此绕了一大圈 —— 早上 09:56 构建的镜像，
  下午连着三次「重建」都还是它，而源码早就在 13:34 就换过了。
  **判据**（改完代码验一次，别靠感觉）：
  ```bash
  sudo docker images --format '{{.Repository}}:{{.Tag}}  {{.CreatedAt}}' | grep qq-rss-bot
  sudo docker exec qq-rss-bot-bot-1 grep -c '<你新加的函数名>' /app/src/qq_rss_bot/bot.py
  ```
  镜像时间要是**刚才**，第二条要 **≥1**。第二条最直接 —— 它绕过日志，
  直接回答「容器肚子里的文件到底是不是我改的那份」。
- **「镜像已存在所以不构建」是错的**：`docker compose build` 哪怕 tag 已存在
  也照样重跑 COPY 层（实测自证）。所以别把「没生效」归到这个机制上，
  真正的原因是那条路径里根本没有 build 这一步。
- **`JackLin` 跑不了 docker，这是硬阻塞**：`/var/run/docker.sock` 是 `root:root 660`，
  而 **`administrators` 组不给 docker 权限**（DSM 上容器只认 root）。CLI 在
  `/usr/local/bin/docker`，但 PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，
  非 root 连命令都找不到；`sudo` 要密码。所以**部署这步只能用户本人做**
  （Container Manager UI 建项目，或他自己 `sudo docker compose`），
  SSH 里干不了。别反复试。
- **拉镜像不需要代理**：目标机实测 `ghcr.io/v2/` 与 `registry-1.docker.io/v2/`
  都返回 401（未带 token 的正常响应），0.7–0.9s。compose 里给 rsshub 配代理
  是为了抓被污染的上游，跟构建无关。
- `.env` 在目标机上是 **600**，`RSSHUB_BASE=http://rsshub:1200`、
  `DB_PATH=/app/data/bot.db`。传文件用 **tar-over-ssh**：
  群晖的 `rsync` 是 openrsync（协议 29），不认 `--info=stats2`，
  且它 fork 出来的 ssh 拿不到密钥，`rsync -av` 直接 `Permission denied`。
- **容器里的日志必须同时落一份到挂载卷**（`LOG_FILE=/app/data/bot.log`，
  由 `__main__.setup_logging` 装 `RotatingFileHandler`）。原因：`docker logs`
  要 root，`/volume1/@docker` 是 `drwx--x--- root root`，非 root 完全看不到 ——
  于是「容器起没起来、为什么连不上网关」这种问题在 SSH 里**无从下手**。
  写到挂载卷后宿主上直接 `cat data/bot.log` 就行。
  **`setup_logging` 必须跑在 `RssBot(...)` 构造之前**：botpy 的
  `configure_logging` 会动 root 的 logger。`smoke.py` 的 `[4.93]` 钉住了这条
  （构造完 RssBot 后文件 handler 还在、还能写进去）。
- **判断「容器里的 bot 到底连上网关没有」，只能看日志，别信 netstat。**
  两条都被实测证伪过：
  - **主动消息不能当探针**：本机实例**已经停掉**的情况下，`scripts/test_c2c.py`
    发主动 C2C 照样 **HTTP 200**。脚本里 `50054 机器人不在线` 那条提示是当初的
    推测，**不成立**。
  - **宿主 `netstat` 看不到容器命名空间里的套接字**：群晖上实测 bot 明明
    `启动成功 / 已上线 / 心跳维持启动`，而宿主 `netstat -an | grep ESTABLISHED`
    里**一条到 `:443` 的外部连接都没有**。据此推断「没连上」是**错的**，
    白白绕了一大圈。（容器出网连接会不会出现在宿主 netstat 取决于网络模式，
    别拿它当判据。）
  - 唯一可靠的来源是**容器日志**。而 `docker logs` 要 root、
    `/volume1/@docker` 非 root 进不去 —— 所以上面那条 `LOG_FILE` 是必需品，
    不是锦上添花。要么开 `LOG_FILE`，要么让用户在 Container Manager UI 里看。

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
  **@ 有没有 `mentions` 字段取决于客户端形态**：手工 @ 的报文**没有** mentions，
  @ 以显示名明文混在 content 里；指令面板 @ 的报文**有** mentions 且带 `is_you`。
  早期笔记写成「一律没有 mentions 字段」是错的。
- **@ 有三种形态，正则漏一种就是「面板点了没反应」**：显示名明文
  `@猪排爱好者 /ping`（手工 @）、`<@OPENID>` 占位符（**指令面板按钮发的就是这个**）、
  老数字 `<@!123456>`。占位符里是 **32 位十六进制 openid 不是 QQ 号**，
  `_PLACEHOLDER_RE` 曾经写成 `<@!?\d+>` 只认数字，于是面板消息全部被
  `忽略非 @ 消息` 丢掉（DEBUG 级，INFO 下完全看不见）。
  现在正则用 `[0-9A-Za-z_-]+`。
- **判「有没有 @ 机器人」要优先用 `mentions[].is_you`**：平台直接标了
  「这个 @ 就是你」，是唯一精确的依据。**但 botpy 的 `_User` 把它丢了**
  （只留 `id/username/bot/avatar`），和 `member_role` 一样要在 `compat.Mention`
  里捡回来。判据优先级：`is_you` → 有 mentions 但没 `is_you` 就**明确否定**
  （所以「@队友 + 恰好一个指令名」不会误触发）→ 无 mentions 时才退化成
  显示名匹配 → 再退化成「开头占位符 + 已知指令名」。
  最后那层退化有已知误判（`@队友 ping`），只在完全拿不到 mentions 时才走到。
- **排查「收不到事件」先把 `LOG_LEVEL=DEBUG` 打开**：`compat.py` 会把原始报文
  打出来。这次就是靠它直接看到 `<@OPENID> /help`，不用猜。
- **`compat.py` 还会替换 `parse_group_at_message_create`**，不只是补新事件。
  原因：botpy 的 `GroupMessage._User` 只取 `member_openid`，
  **把 `author.member_role` 丢了**，而 `/sub`、`/unsub` 的管理员限制要靠它。
  补法是子类 `GroupMessageWithRole` 加一个 `__slots__` 槽位——
  `GroupMessage` 用了 `__slots__`，不能事后挂属性。
  两个解析器都替换，幂等靠函数上的 `_qq_rss_bot_patched` 标记。
- **`/list` 的编号是「本群内的位置」，绝不是数据库主键**。主键是全局自增、
  跨群共享的：直接打 `sub.id` 会让从没订阅过的新群第一条订阅显示成「5.」，
  删一条还留下空号。`_pick()` 负责把用户输入的位置编号解析成订阅，
  `_list` 用 `enumerate(subs, 1)`。删完自动重排。
  编号会随增删变化，所以 `/unsub` 的回复必须带上**被删源的名字** ——
  否则用户在 `/list` 之后列表变了，删错了也不知道。
- **指令别名表 `COMMAND_ALIASES` 是唯一事实来源**：`COMMAND_TOKENS`（什么算指令）、
  `RESTRICTED_TOKENS`（哪些要管理员）、`COMMAND_METHODS`（派发到哪个方法）
  全从它派生。曾经是手工维护三份，`RESTRICTED_TOKENS` 漏一个中文别名就等于没限制。
  加指令只改 `COMMAND_ALIASES` + 写一个 `_xxx` 方法 + 在 `COMMAND_METHODS` 挂上。
- **角色拿不到时放行（fail-open）并记警告**。宁可漏管，也不能让机器人整个不可用。
  放开限制的开关是 `RESTRICT_ADMIN_COMMANDS=false`。
- **指令面板的字数限制是显示宽度，不是字符数**（name ≤14 / desc ≤30，中文按 2 算）。
  用 `len()` 校验会放行超宽的纯中文描述，到接口才回 `30013`；而该码在文档里写作
  「超出数量限制」，极易误判成「元素太多」。`scripts/setup_panel.py::_width()` 是正确口径。
- **面板的指令名存成裸名**：`POST` 时给 `/sub`，服务端剥成 `sub`（客户端自己补斜杠）。
  `PANEL_ITEMS` 直接写裸名，`compare_items` 用 `_name_key()` 两边归一 ——
  否则回读时每个指令都会报一对「缺少 /sub / 多出 sub」的假差异。
- **`only_admin=false` 时服务端省略该字段**，回读成 `None`。比对要 `bool()` 归一，
  不然每个非管理员指令都报一条假差异。
- **平台 UI 配的面板和我们 API 建的不是同一份数据**（实测：UI 里配了，`GET /v2/panels`
  四个 scope 全返回 0，客户端打 `/` 显示「这里还没有指令」）。UI 那条路配完要
  **扫码确认修改**才生效，且「使用场景」必须勾「QQ群」、「不能勾「消息列表」」。
  所以别把 API 查到的 0 当成「UI 也没配」。
- **指令面板 API 的两个坑**：`GET /v2/panels` 的 **`scope` 是必填查询参数**
  （官方文档把它放在参数表里，容易漏，漏了返回 30011「生效场景不合法」）；
  错误码**文档写 `40030011`、运行时返回 `30011`**，映射时要做归一化。
  host 与消息接口相同（`https://api.sgroup.qq.com`），文档没写但实测可用。
- **先分清两个东西，混起来会得出完全错误的结论**：
  - **指令面板** `/v2/panels`：输入 `/` 或 @机器人 拉起，四场景
    （c2c / group / channel / dm）。**是平的，不能嵌套** —— 见下条实测。
  - **自定义菜单** `GET|PUT /v2/menu`：**只在「C2C 单聊」底部**的那排按钮，
    群里**不显示**（官方原文：「菜单适用于 C2C 单聊场景，对所有用户生效」）。
    **这个能嵌套**：`items` 最多 10 项；`type=menu` 的项用 **`sub_menu_items`**
    存子菜单，最多 5 个，**且只有一层**（官方原文：「二级菜单不支持 menu 类型」
    —— 所以不可能再深）。
    `MenuItem.type` 四个值：`switch` / `send_message` / `link` / `menu`；
    `SubMenuItem.type` 只有 `send_message` / `link`。
    `name` ≤10 字符（中文算 2），子菜单 `name` ≤14；PUT 限 5 QPM。
    `send_message` 型是**把文字填进输入框**（不是直接发送），`link` 须 `https://`。
    实测（2026-10-16）：`GET /v2/menu` → HTTP 200 `{"menu":{}}`，本机器人没配过。
  - **两个接口的错误码是独立的**：菜单类型非法是 **40030014**，
    面板元素类型非法是 **40030015**。别混用（`setup_panel.ERROR_HINTS` 只管面板）。
  - 官方文档入口：`/wiki/develop/api-v2/server-inter/menu-panel/`，
    接口细节在各 `autogen/api/v2_menu.*.html` 与 `autogen/api/v2_panels*.html`
    —— **autogen 那批页面有完整 schema，总览页没有**，直接抓 autogen。
  「QQ 机器人的菜单不能嵌套」这句话是**错的**，只有「指令面板不能嵌套」成立。
- **指令面板不支持嵌套（2026-10-16 实测，别再查了）**。想给**指令**分组，
  面板这条路走不通 —— 只能自建按钮菜单（见本节末尾）或用命名前缀。
  证据脚本 `scripts/probe_panel.py`，可随时复跑当回归。
  - **嵌套字段被静默丢弃**：item 里塞 `children` / `items` / `sub_items`，
    POST **返回 200**，但回读只剩 `{name, desc, type}`。不报错、不生效 ——
    「接口成功」在这里**不等于**「存下来了」，必须回读。
  - **`type` 是校验过的枚举，只有 `command` / `link` 两个值**。
    `menu` / `text` / `button` 一律 HTTP 400 **30015「面板元素类型不合法」**。
    没有第三态，所以**不存在「子菜单」这种元素**。
  - **`link` 的必填字段就叫 `link`**（URL，须 `https://`），不给报
    **30016「必填字段缺失」**。**30015 与 30016 的区别是判读 schema 的关键**：
    30015 = 这个类型不存在；30016 = 类型存在、只是还差字段。
    分不清就会把「存在但缺参数」误判成「不支持」。
  - 上限：单面板 ≤20 元素、单机器人 ≤20 面板（`type` 与元素数**无关**，
    别指望用 link 换配额）；name ≤14 / desc ≤30（**显示宽度**，中文按 2）。
  - **要分组只能靠消息里的 `keyboard` 按钮自建菜单**。schema 见下条，
    代价是菜单本身是一条真消息、占主动消息配额；面板是客户端 `/` 补全，不占。
- **`keyboard` 必须包 `content` 外层，漏了就是「200 但按钮没了」**
  （2026-10-16 实测踩到）。官方两种形式：
  ```json
  "keyboard": {"content": {"rows": [...]}}   // 长形式
  "keyboard": {"id": "..."}                  // 短形式，平台预置模板
  ```
  直接把 `rows` 挂在 `keyboard` 底下 → 接口 **HTTP 200**，消息发得出去，
  **按钮被静默丢弃**。这和指令面板的 `children` 是**同一个坑的第三次出现**：
  **这个平台上「200」从来不等于「生效」，凡是新字段一律回读或肉眼确认。**
  注意 `botpy` 的 `types/inline.py` 里 `Keyboard` 是**内层**结构（`{rows}`），
  `KeyboardPayload` 才是外层（`{id, content}`）—— 别拿内层当参数传。
  - `action.type`：**0=跳转**（http / 小程序）**1=回调**（要 `interaction` intent，
    另一个 bit）**2=指令**（把 `@机器人 data` **插进输入框**，不是直接发）。
    用 0 和 2 不需要新 intent。
  - **`enter: true` 才自动发送**（只对指令按钮有效），且官方注明
    **仅单聊可用** —— 群里点了只会填进输入框，还得用户自己按发送。
  - `render_data`：`label` ≤**10 字符**（中文按 2）、`visited_label`、`style`
    （0 灰框 / 1 蓝框 / 3 白底红字 / 4 蓝底白字）。
  - `permission.type`：0=指定用户 / 1=管理员 / 2=所有人（`specify_role_ids` 仅频道）。
  - `click_limit` **官方已废弃**，别传。`unsupport_tips` = 客户端不支持时的提示语。
  - **`render_data.label` ≤ 10 显示宽度**（中文按 2）—— 比指令面板的
    `name ≤14` **还要紧**。`退订 偶像大师` 宽 13，直接超限。所以
    `menu.py` 生成的 label 里**只放编号**（`退订 1`，宽 6），订阅名字放正文
    （正文就在按钮上方，不会看不懂），`clamp_label()` 兜底 + smoke 逐条断言宽度。
    在 `src/` 里落地成 `menu.py`，它同时也是 `setup_panel._width` 的实现来源
    —— 显示宽度这个口径全项目**只有一份**。
  - **`menu.keyboard()` 是发按钮的唯一出口**，它负责包 `content` 那层。
    自己拼 `{"rows": ...}` 就是「200 但按钮没了」。
  - `action.type=2` 只是把文字**插进输入框**，不会自动发送；所以按钮的 `data`
    要写成「点完直接能发」的形态：带编号的写完整指令（`/unsub 1`），
    要用户补参数的留一个尾空格（`/find `）。
  - 群消息 `msg_type` 只有 **0 / 2 / 7** 三种（文本 / markdown / 富媒体）。
  - markdown 的 `template_id`/`custom_template_id` **都已废弃**，但缺权限会报
    **304036 / 40034127「无 Markdown 模板权限」**。
  - 主动消息频控：发送侧 **60 qpm**（企业/ID 认证）或 30 qpm，接收侧 20 qpm、
    每群每天 1000 条。被动回复 5 分钟内**最多 5 次**（超了 40034128）。
- **`only_admin: false` 时服务端省略该字段**（回读成 `None`），比对要 `bool()`
  归一 —— 和上面那条是同一个坑的另一半：控制台字段「发出去 200」不等于「存下来了」。
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
- **`pgrep -f 'python -m qq_rss_bot'` 匹配不到，会静默漏杀**。macOS 上
  Python 的可执行文件叫 `Python`（**大写**），而 `pgrep -f` 大小写敏感 ——
  这个坑已经造成过两次「以为重启了、其实旧的还在跑」，两个实例同时接网关
  **重复回复 + 重复推送**，旧进程的日志还被新进程覆盖，现场没法还原。
  正确写法 `pgrep -if -- '-m qq_rss_bot'`（前导 `-m` 是为了不误伤 grep 自己）。
  已封装成 `scripts/botctl.sh`，**别再手写 pkill**。
- **判「机器人到底有没有在跑」要 `lsof -p <pid> | grep TCP`** 看有没有
  `ESTABLISHED ... :443`。进程活着 ≠ 网关连上了，登录失败时进程也在。

- **群里分享的卡片消息，`content` 不是空的** —— 平台把卡片**预渲染成了全文**，
  连 `jump_url:` 那一行都在里面。实测形态：
  `[卡片消息] 图文H5\n摘要: …\ntitle: …\ndesc: 来自哔哩哔哩\njump_url: https://b23.tv/xxx\ntag: …`。
  所以解析分享链接**只扫 `content` 就够**，别一上来就去挖 `ark_data`。
- **`ark_data` 的数据嵌在 `fields` 子字典里**，不是平铺。真实结构是
  `{ark_name, ark_type, fields: {title, preview, source, jump_url, …}, prompt}`，
  直接 `ark_data["title"]` 取不到。botpy 的 `GroupMessage` 还**整个丢掉**
  `ark_data` 和 `message_type` —— 两个都要在 `compat.GroupMessageWithRole`
  的 `__slots__` 里自己捡回来（和 `member_role` 是同一手法）。
- **httpx 的 `raise_for_status()` 把 3xx 也算失败**（`is_success` 只认 2xx）。
  短链的正常形态就是 302，所以「跟一次重定向读 Location」这条路上**绝对
  不能**调它 —— 否则所有短链都静默解析失败，日志里一行错都没有。只有
  `>= 400` 才是真错。
- **bilibili 的 `wbi/search` 要 Chrome UA 和 `Referer` 同时带**，缺一个就是
  「HTTP 200 + `code=0` + **空结果**」的静默失败（项目自己的 UA 无论如何
  都是 0 条）。旧的 `search/type` 接口则被 **412 风控**拦死。四组对照见
  「群里分享 bilibili 链接」那一节。

## 现场验证过的结论（2026-10-16）

- **主动私聊（C2C）能发**：`scripts/test_c2c.py --listen` 抓到的群消息
  `member_openid`（就是你的 openid），直接 `POST /v2/users/{openid}/messages`
  发不带 `msg_id` 的消息 → **HTTP 200**。`member_openid` 和 C2C `openid` 是同一个值。
- **那个脚本一开始收不到事件**，因为它没调 `install_new_protocol_support()` ——
  和主程序当初的 bug 一模一样：QQ 2026 推 `GROUP_MESSAGE_CREATE`，
  botpy 1.2.1 没有对应解析器，事件被**静默丢弃**。
  **任何自己连网关的脚本都必须先装协议补丁**（`__main__.py` 里装了，
  独立脚本不会自动继承）。


在真实群（group_openid 从库里读，不写进仓库）上跑通的链路：

- 被动回复 `/ping`：成功（无报错）
- `@` 过滤：发 `111` 被正确忽略，日志 `忽略非 @ 消息: '111'`
- `/sub /v2ex/topics/hot`：成功，抓到 title 并建立 baseline（`initialized=1`）
- **主动消息：HTTP 200 成功**，群内「机器人主动在群聊内发言」开关是开的
- **指令面板按钮真的会补 @，补的是 `<@OPENID>` 占位符形态**（实测 18:42）：
  手动 @ 是 `@猪排爱好者 /ping`（content 里是**显示名**，无 mentions 字段），
  面板是 `<@C371C51BA906A22E4...> /help `（**有** mentions 且 `is_you: true`）。
  两种形态现已都能触发。
- **平台会给 `mentions[].is_you`**，实测报文：
  `'mentions': [{'bot': True, 'is_you': True, 'username': '猪排爱好者',
  'id': '<机器人openid>', 'member_openid': '<机器人openid>', 'member_role': 'member',
  'scope': 'single'}]`。机器人自己的 openid 也在报文里（用 `mentions[].is_you` 就够，不必记它的值）。
  注意 `username` 会随改名变（18:48 还是「猪排爱好者」，18:52 已变成「炸猪排爱好者」），
  所以**不能**靠显示名做长期判据。
- **指令面板：`POST /v2/panels` 创建成功**，`panel_id = p_Y8uv4aayeceLDxBMGeKjGB`
  （scope=group、target_type=all、6 个元素），回读与代码完全一致。
  改内容：`scripts/setup_panel.py --update p_Y8uv4aayeceLDxBMGeKjGB`

- **`/find` 的路由表来自 RSSHub 自带的 `GET /api/namespace`**（不是文档站）。
  本地实测 4MB / 2064 站点 / 4004 条 / 24 分类，一次 0.12s。
  精简后缓存 670KB，按天刷新，命中缓存 0.01s。
  **RSSHub 挂掉时用过期缓存兜底** —— 抓不到路由表不该让整个功能不可用。
- **路由的 `features` 能预判能不能跑**，这是 `/find` 比翻文档强的核心：
  `requirePuppeteer` → 需要 chromium 镜像；`requireConfig`（**注意 `optional: true`
  的不算阻塞**，微博的 `WEIBO_COOKIES` 就是）→ 缺环境变量会 503；
  `antiCrawler` → 可能要 cookie/代理。实测：可用 3410 / 需配置 130 /
  反爬 283 / 需浏览器 181。
- **没把 `description` 纳入检索**：实测「新闻」命中 196 → 371 条，
  但**前 5 条完全没变**，纯粹是体积负担。索引只放名称/路径/站点/分类。
  要重新评估就按这个口径测：看**前 5 条有没有变**，不是看命中数。

- **「多条消息合并转发」官方机器人 API 做不了，别再查了**。依据：
  botpy 1.2.1 的群消息出口只有 `post_group_message` / `post_group_file`；
  `msg_type` 枚举是 0 文本 / 1 图文 / 2 markdown / 3 ark / 4 embed / 7 富媒体，
  **没有合并转发**。合并转发是 **QQ 客户端协议**的特性，
  支持它的 NapCat / Lagrange / LLOneBot 走的是非官方客户端协议，不是这条 API。
  所以推送形态只能是逐条 —— 这同时也是一条图文消息只能带一张图的必然结果。
- **`/new` 是「按需读最新」，和定时推送共用推送出口和文案**
  （`commands.CommandHandler` 收一个 `Pusher`，就是 `RssBot` 自己）。
  默认每源 3 条，`_NEW_MAX_PER_SUB=10` / `_NEW_MAX_TOTAL=15` 两个上限挡频控
  （逐条带图，一次刷几十条必撞群维度 20 qpm）。
  **读过的标记已见**，否则用户主动看一眼、过几分钟定时推送又原样刷一遍。
  标记只给**真发出去**的条目（发送失败/被截断的留给定时推送），
  语义和 `scheduler` 的 at-least-once 一致。
  回执走**被动回复**（不占主动消息额度），条目走主动消息（图文那条路只在 Pusher 里）。
  用法：`/new`、`/new 2`、`/new 2 5`、`/new all 5`；编号同 `/list` 的**本群位置**。

## /rss 按钮菜单与进群自动订阅（2026-10-16 加）

- **面板只当入口，分层靠消息按钮**。指令面板实测是**平的、不能嵌套**，
  11 个元素挤在一起之后再加功能就没地方放了。所以 `PANEL_ITEMS` 砍到
  **`rss` / `new` / `help`** 三项，订阅/退订/列表/搜索全收进 `/rss` 的
  按钮卡片。三个都 `only_admin=False` —— `/rss` 只是打开菜单，
  权限在 `commands._is_admin` 里判，面板标管理员会让普通成员连菜单都打不开。
- **被动回复（带 `msg_id`）能带 `keyboard`，实测通过（2026-10-16）**。
  之前只在**主动消息**上验证过按钮渲染，官方文档没说这两条路等价，
  所以「被动回复带按钮」一度是唯一的未知项。现在群里 `/rss` 实际点出来了，
  这条路确认可用 —— 换句话说按钮不必占主动消息配额。
- **`/rss` 走被动回复，不走主动消息**。它永远是为了回应某条群消息，
  被动回复**不占主动消息配额**（配额要留给推送），也不需要群里开
  「机器人主动在群聊内发言」。代价是同一条 `msg_id` 的 5 次回复额度用掉 1 次。
  `CommandContext.reply_card` 是那条通路，默认 `None`（老协议路径、
  smoke 里的假实现都没有），**拿不到就退回纯文本** —— 见
  `menu.build_rss_text()`，它和卡片内容等价，不是一句「出错了」。
- **两个回复闭包必须共用同一个 `msg_seq` 计数器**（`_make_repliers`）。
  各自 `count(1)` 的话，先发卡片再发文本会在同一个 `msg_id` 下撞 seq，
  后一条被**静默丢弃** —— 症状是「偶尔不回复」，日志里什么都看不到。
  所以卡片退回文本时消耗掉的那个 seq 是**故意**的，别去「修」。
- **botpy 只在 HTTP 非 2xx 时抛异常，业务错误码是 200 带 `code` 回来的**。
  `reply_card` 因此显式检查 `resp["code"]` 并抛错 —— 不判这一下，
  `304036 / 40034127`（无 Markdown 权限）会被当成发送成功，用户那边一片空白，
  而纯文本兜底永远不会触发。
- **退订按钮按本群实际订阅现算**（插的是完整 `/unsub 1`），超过 4 个折叠成
  「退订…」走二级卡片。单条消息最多几行按钮官方没写、也没实测过，所以主动压住行数。
- **进群自动订阅只在数据库插一行 `initialized=0`，绝不在这里抓源**
  （`autosub.AutoSubscriber`）。`fetch_feed` 最长要 `fetch_timeout_sec`（20 秒），
  在 botpy 的事件回调里干等会把分发循环卡住。轮询器下一拍（≤30 秒）走
  `scheduler` 的 `if not sub.initialized:` 分支建 baseline（**不推送**）并写回真实标题。
  所以 `/list` 里先看到的是路由串，半分钟后变成真名字 —— **这是预期的，不是 bug**。
- **启动补订阅给「所有已知的群」都补**（`backfill()` 默认 `only_if_empty=False`）。
  有订阅的群也补：要么已经订了这个源（幂等，白补），要么订的是别的源、正好缺这一个。
  已知副作用：**某个群故意退订了这个源，下次启动会被补回来**。
  补订阅不发消息（启动时刷一堆太吵）。旧开关 `backfill(only_if_empty=True)`
  保留成「只补零订阅的群」，`smoke [4.92]` 两种模式都钉住了。
- **补订阅只看 `groups` 表，空库补不了。** 全新部署时那张表是空的，
  日志会写「本次给 0 个群补上了」——**这不是功能坏了**，是机器人还不认识任何群。
  群只能从事件里学：有人 @ 一次（`touch_group`）或被拉进群。
  `/volume1/docker/qq-rss-bot/data/bot.db` 迁移到新机器时最容易踩这个 ——
  老库里的 `group_openid` 是「群 × AppID」维度的，**同 AppID 下可以直接搬**，
  但 `subscriptions.url` 存的是**解析后的全量地址**（老机器上是
  `http://127.0.0.1:1200/...`），搬到容器里必须改写成 `http://rsshub:1200/...`，
  否则指向容器自己、抓取全挂。
- **`db.forget_group()` 必须连 `groups` 那行一起删**。`known_groups()` 是补订阅的
  依据：只删订阅、留着 groups 的话，机器人退群后下次启动会把这个群当成
  「零订阅的新群」重新订上，然后一直往一个自己已经不在的群里推送 ——
  每个周期失败一次、**永远不停**（`fail_count` 只统计**抓取**失败，
  推送失败不会让它自动暂停）。这行是真会咬人的。
- **进群欢迎语只能是主动消息**：`GroupManageEvent.__slots__` 里没有 `msg_id`，
  发不了被动回复。新群多半还没开「机器人主动在群聊内发言」，那条会**静默失败** ——
  所以只记警告，绝不让它影响订阅本身。
- **`on_ready` 断线重连会再跑一次**，所以补订阅必须幂等（它确实是）。

## 群里分享 bilibili 链接自动回卡片（2026-10-17 加）

群里任何人发 bilibili 链接（**不需要 @ 机器人**），回一张带封面/标题/UP 主/
播放数据的 markdown 卡片。走**被动回复**（`msg_id` 就在手上），不占主动消息
配额。开关是 `BILIBILI_PREVIEW`（默认开）。实现全在 `src/qq_rss_bot/bilibili.py`。

**能做的前提**：新协议推的是全量群消息，非 @ 的分享本来就会到达机器人 ——
以前被 `is_bot_mentioned` 直接丢掉。**旧协议收不到这些消息，这个功能无从谈起。**

### 四种分享形态（2026-10-17 抓的 5 条真实报文实测）

| 形态 | `message_type` | 视频 ID 在哪 | 处理 |
|---|---|---|---|
| QQ 小程序卡片 | 3 (ark) | **报文里没有** | 拿标题反查 |
| 纯文本 + `b23.tv` | 0 | `content` 里的短链 | 302 拿 BV |
| 图文 H5 卡片 | 3 (ark) | `content` 里的 `jump_url` | 302 拿 BV |
| 纯文本 + BV 号 | 0 | `content` | 直接用 |

`scripts/smoke.py [4.94]` 把这四条 `content` **原样**当回归样本；
`scripts/probe_bilibili.py` 能在真发到群里之前先把渲染结果打出来看
（**默认什么都不发**，要真发必须显式给 `--send <group_openid>`）。

### 标题反查（只有小程序卡片需要）

接口用 **`wbi/search`**（`search/type` 被 412 拦死），且**必须同时带
Chrome UA 和 `Referer: https://www.bilibili.com/`**：

| UA | Referer | 结果 |
|---|---|---|
| 项目 UA | 无 | `code=0`，**0 条** |
| 项目 UA | 有 | `code=0`，**0 条** |
| Chrome UA | 无 | `code=0`，**0 条** |
| Chrome UA | 有 | `code=0`，**20 条** ✓ |

**判据是「去标点归一化后恰好一条相等」**，0 条或 ≥2 条都放弃 ——
宁可不回，也不能回错视频。实测分享标题和线上标题就差标点：分享版
`…拿起了刀《机器纪元》`，线上是 `…拿起了刀！！！《机器纪元》`，
归一化后相等（36 字），20 条结果里**唯一命中** `BV1JzeK66EQB`。
归一化长度不足 8 字（「测试」「哈哈」）直接不查 —— 短标题必然误判。

### 其它实测结论

- `api.bilibili.com/x/web-interface/view?bvid=` **无需 cookie**，项目自己的
  UA 就能过。返回 `title` / `pic` / `owner.name` / `duration` /
  `stat.{view,danmaku,reply,like,…}`。
- 封面 `pic` 返回的是 **`http://`**，改 `https://` 同样 200；加图床后缀
  `@480w_270h_1c.jpg` 后 **214KB → 33KB**（QQ 要自己下载转存这张图）。
- QQ 群 markdown **支持图片**，语法 `![文字 #宽px #高px](url)`，
  **尺寸提示是必填的**（官方示例就带尺寸），平台自己下载转存，
  所以防盗链、图床都不用管。
- 标题**不能**放进 `[文字](url)` —— 实测 100 条热门标题里 7 条含
  `|` / `[` / `]` / `!`。所以标题单独一行加粗，并把星号、下划线、
  反斜杠、波浪号和反引号剥掉，
  链接另起一行写「在 bilibili 打开」。
- **不做去重**（用户明确选的）：同一条视频发两遍就回两遍。单条消息里含
  多个链接时只取第一个 —— 这是「一条消息回一次」，不是跨消息去重。
- **对群里静默，对日志不静默**：解析不出来就什么都不发，异常在
  `bot._preview_bilibili` 里全吃掉 —— 但**日志按 INFO/WARNING 记全**。
  见下。

### ⚠️ 失败路径一律不许写 DEBUG（2026-10-17 血的教训）

第一版把 `lookup` 的每条失败路径都写成 `_log.debug`，理由是「这是顺手的
额外功能，坏了不该刷日志」。**这个理由是错的**，代价是：线上群里发了链接
毫无反应，而日志里**也毫无痕迹** —— 当时 `LOG_LEVEL=INFO`，所有失败原因
全被吞掉，排查只能靠猜，猜了一轮全猜错。

判据很简单：**这行日志只在「有人分享 bilibili 链接」时才可能出现**，
频率天然极低；而它一旦出现，就意味着「有人分享了但我们没回」——
**这正是唯一需要看见的事件**。这种日志写 DEBUG 等于没写。

现在的分工：

| 位置 | 级别 | 为什么 |
|---|---|---|
| 认出分享 / 成功回卡片 | INFO | 频率极低，是正常路径的痕迹 |
| 认出了但没解析出视频 | **WARNING** | 「有人分享了我们没回」——必须看得见 |
| `lookup` 内部各步放弃的原因 | INFO | 上面那条 WARNING 的具体原因 |
| 异常栈 | **WARNING**（带 `exc_info`） | 对群里静默，日志留全栈 |
| 「忽略非 @ 消息」 | DEBUG | 保持原样，量大且无信息量 |

### 轮询心跳：`INFO` 级别下必须能看出「进程还活着」

`scheduler.FeedPoller.run()` 每 **20 拍**（默认 10 分钟）打一条
`轮询心跳：第 N 拍，进程正常`（INFO）。一天 144 行，不构成噪音。

**这不是冗余日志。** 踩到的原状是：群里发消息没反应，日志**静默 22 分钟**，
而当时唯一的周期日志 `本轮待抓取` 是 DEBUG，`LOG_LEVEL=INFO` 下看不到 ——
于是「消息没到机器人」和「整个进程卡死了」**在日志上完全一样**，而这两个
方向的排查完全不同。加上心跳后这条线是明确的：

| 有心跳 | 无心跳 |
|---|---|
| 进程和事件循环都正常 → 去查消息链路 | 进程死了或事件循环冻结 → 先看容器 |

`poll_once()` 在没有到期订阅时**直接 return**（不记日志），所以
`本轮待抓取` 不能当心跳用 —— 订阅间隔 15 分钟，它 15 分钟才可能出现一次。

### 启动自检：一条命令定位三种故障

`bot.py` 的 `_announce_bilibili()` 在 `on_ready` 里探一次 bilibili 并打日志
（`bilibili.probe()`，最多等 6 秒）。**`on_ready` 每次断线重连都会再跑**，
所以这条线还会顺便证明「网关掉过没有」。

日志长这样：

```
bilibili 预览已启用；出站代理环境变量 {'HTTP_PROXY': '', 'HTTPS_PROXY': '', ...}
bilibili 预览连通性自检：✅ 正常（BV15eJt6pEam 【中字熟肉】气象厅の狠活！…）
```

三种故障的判读方式**互不重叠**：

| 现象 | 结论 |
|---|---|
| 自检 ❌ | 容器出网有问题（DNS / 代理 / CA），后面全都不用查了 |
| 自检 ⚠️「网络通但接口非 0」 | 网络没问题，只是样例视频没了 —— **别当成连不上** |
| 自检 ✅，发链接后**没有任何** bilibili 行 | 消息没到机器人（网关/群配置），不是这个功能的问题 |
| 自检 ✅，有「消息里有 bilibili 字样但没认出链接」 | 消息到了，是 `find_target` 没认出这个形态 |
| 自检 ✅，有「认出 bilibili 分享」但没有成功行 | 消息和识别都没问题，卡在 `lookup`，下一行 INFO 写了原因 |

**别拿宿主机的网络结果推断容器。** 实测在群晖宿主机上：`curl -4 -k` 返回
200、`openssl s_client` 验证通过、Python 裸 socket 直连 16 个解析 IP **全部
通过**，但 `urllib.request.urlopen` 五次全挂在
`self-signed certificate in certificate chain` —— 猴补 `wrap_socket` 才发现
对端是 `192.168.0.63:6152`（那台 Surge），而 `getproxies()` 返回 `{}`、
没有任何 `_proxy` 环境变量、没有 `/etc/docker/daemon.json`。**容器里的
Debian Python 不会继承这套东西**，所以宿主机上的结论对容器无效。

### 还没实测的（第一次真发到群里时要盯这三条）

- `![封面 #208px #117px](url)` 这套尺寸语法 QQ 认不认 —— 文档说要用，
  但没人见过实际渲染效果
- 标题里的 `|` / `[` 会不会破坏卡片排版
- `i1.hdslb.com` 的封面平台能不能下载转存（项目里只验证过 idolmaster 那个源的图）

## 限流、配额与出错通知（2026-10-16 加）

- **推送必须全局节流，`_PUSH_GAP_SEC` 挡不住**。scheduler 用信号量并发跑
  最多 5 个抓取循环，每个循环内部的逐条 sleep 是**各自**的 —— 实测 5 个源
  同时有新内容时 25 条全部落在 1.6 秒内，而群维度频控是 20 条/分钟。
  现在把 `RssBot` 包进 `quota.GroupSender`：**群维度一把锁**（顺带保证到达
  顺序）+ **令牌桶**（20/分钟，超了排队等，消息一条不丢）+ **每日配额落库**。
  桶用容量=速率的形式，所以允许「一次突发用满 20 条」，比「每条 sleep 3 秒」
  合理得多。
- **每日配额必须落库（`send_counters` 表），不能放内存**。进程一重启就清零的话，
  一天的真实用量会被反复低估。配额按**本地日期**算 —— QQ 在本地零点重置。
- **`QuotaExceeded` 必须穿透 `_push_entries`**。它是 RuntimeError，会被那里的
  `except Exception` 当成「配图失败」吞掉，然后每条再试一遍纯文字、刷两行日志，
  最后什么都没解决。两处 `except Exception` 前面都加了 `except QuotaExceeded: raise`。
- **`_poll_one` 返回三态，不是布尔**：`True` 抓取成功 / `False` 抓取失败 /
  **`None` 本轮跳过**（配额用尽）。混成两态会让 `_check_global_outage`
  把「配额用尽」误报成「所有源都挂了」，把人引到完全错误的方向
  （这个是演练时才暴露的）。
- **配额用尽时把没发的也标记已见**。不标的话，第二天配额恢复会倒灌一整天的积压。
  这是**有意丢弃**，和 `max_items_per_push` 溢出时同样处理。
- **出错只走私聊（C2C），群里一条不报**。`notify.C2CNotifier` 负责投递。约束：
  - **绝不回落到群消息**：C2C 失败只记日志。宁可少一条通知，也不把错误刷进群。
  - **同类消息按 key 冷却 30 分钟**。RSSHub 一挂是所有源一起挂，不冷却就是 DM 风暴
    （实测 5 个源会连发 5 条同样的通知，冷却后只发 1 条）。
- **收件人两种模式，`NOTIFY_OPENIDS` 一票否决**。`C2CNotifier.targets()`：
  配了白名单就**只发给名单上的人、完全不查数据库**；没配才退回
  `notify_targets`（该群发过指令的 owner/admin，最多 3 人）。
  为什么要有白名单：自动登记收的是**群主 + 所有管理员**（最多 3 人），
  群里只要有别的管理员，他们也会收到。白名单把收件人钉死成一个人，
  换群主 / 加撤管理员都不影响。
  （**2026-10-16 更正**：先前据 `notify_targets` 长期为空推断「`member_role`
  可能拿不到、默认模式静默发不出去」—— **这个推断是错的**。实测群里发指令时
  `角色='owner'` 正常打出来了，登记也真的写进去了。表当时是空的，只是因为
  加这个功能之后还没有管理员发过指令。）
  - **别把默认行为说成「只发给你」**：没配白名单时群主和**所有管理员**都收得到。
  - **`is_target()` 是唯一该用的判定**（私聊回执里也用它）。别再直接读
    `db.notify_targets` 判「这个人收不收得到」—— 白名单模式下那是错的。
  - **白名单超 `MAX_TARGETS`(3) 会截断并打 WARNING**，不静默丢。
  - **openid 是「用户 × 机器人」维度的匿名 ID，不是 QQ 号**：同一 AppID 下稳定，
    **换 AppID 全变**（`group_openid` 和订阅数据一起废）。群消息里的
    `member_openid` == C2C 的 `openid`（实测）。写进 README 了。
- **撤了管理员要摘掉登记**。`_handle_group_message` 里角色**明确**不是
  owner/admin 且人在登记表里 → `remove_notify_target`。不摘的话被撤权的人
  会一直收到私聊。角色取不到时不动（与 fail-open 约定一致）。
- **主动 C2C 消息实测可用（2026-10-16）**：群消息里的 `author.member_openid`
  **就是** C2C 的 `openid`，拿它直接 `POST /v2/users/{openid}/messages`
  发不带 `msg_id` 的主动消息，**HTTP 200**，不需要对方先私聊过机器人。
  验证脚本 `scripts/test_c2c.py`（裸 HTTP，不占 WS 会话，可与机器人并行跑）。
  所以「在群里用过一次指令」就等于私聊通道打通了。
- **连续失败 `max_fail_before_pause`（默认 5）次自动暂停**该订阅并私聊通知。
  `/resume` 会**清零 fail_count**，否则一恢复就立刻又被自动暂停。
- **整轮全军覆没 → 提醒一次「怀疑 RSSHub 或网络不可达」**。这比单独探活准：
  测的就是真正依赖的东西，也不额外发请求。只要有**一个**源成功就不报，
  免得单个源的 404 被误报成整体故障。
- **`/check` 必须用只读的 `seen_keys`**。原本用的是 `filter_new_entries`
  （查的同时就写 seen），于是用户只是想预览，那几条新内容被**静默消费**掉、
  定时推送永远不再发 —— 而回复里还写着「本次新 3 条」，读起来像状态报告。
- **`/sub` 报的编号也必须是群内位置**。`/list` 早先修过同一类 bug（泄漏全局自增
  主键，新群第一条显示成「5.」），但 `_sub` 的成功/重复提示漏了 ——
  而且它出现在**订阅成功那一刻**，正是用户最可能照着做的时候。
- **`/interval` 不能清 `initialized`**。清了就重建 baseline，把两次抓取之间的
  增量整段丢掉 —— 那正是「退订重订」的老问题。改间隔本身要清
  `last_polled_at`，不然从 60min 改成 5min 要等一小时才生效。
- **数据库迁移**：`db._MIGRATIONS` + `_migrate()` 只做**加列**（老库用
  `PRAGMA table_info` 检查后 `ALTER TABLE`）。改类型/删列仍然要手工处理。
  新表走 `CREATE TABLE IF NOT EXISTS`，不用管。

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
- **bilibili 预览的提取只扫 `content`**（理由见「群里分享 bilibili 链接」那节）。
  `bilibili.find_target` 是唯一入口，`bot._preview_bilibili` 是唯一调用点。
  以后加新的分享形态：先往 `smoke.py [4.94]` 里加一条**真实 `content` 原文**
  当样本，再改提取逻辑。
- 数据库 schema 改动写在 `db.SCHEMA` 里；目前没有迁移机制，
  表结构变更需要手动处理已有 `data/bot.db`。
