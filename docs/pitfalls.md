> 本文件是 `CLAUDE.md` 拆出来的一节（原样搬迁，未改动内容）。
> 常驻的精简要点在 [`../CLAUDE.md`](../CLAUDE.md)。

# 踩过的坑

- **会 MITM 的代理会让 httpx 报 `self-signed certificate`，而且「图能下、接口挂」。**
  实测 Surge（`192.168.0.63:6152`）只重签了 `www.youtube.com`，**没碰**
  `i.ytimg.com` / `api.fxtwitter.com` —— 于是同一份探测脚本里，YouTube 的
  oEmbed / youtubei 全挂（`CERTIFICATE_VERIFY_FAILED: self-signed certificate in
  certificate chain`），图片却 21KB / 65KB 正常下载。httpx 只信 **certifi** 那份
  CA 列表，**不读系统 CA**。修法：`MEDIA_CA_BUNDLE` 指到代理的根证书（推荐），
  或 `MEDIA_INSECURE_TLS=true` 兜底。**看到「图片能下」绝不能推断「接口没问题」** ——
  这是这个功能里最容易走偏的地方。
- **改 `.env` 用 `up -d`，不是 `restart`。** 环境变量在**创建容器时**注入，
  `restart` 只是把同一个容器再启动一次（配置不变）。实测：`echo MEDIA_PROXY=... >> .env`
  之后 `restart`，启动日志照样打「MEDIA_PROXY 没配」。
  **只有改 `src/` 下的代码才是 `restart`**（那是挂载卷，运行时读）。
- **QQ 的服务器拉不到 `i.ytimg.com`（2026-10-17 实测）。** 发图接口有两种给法：
  `url`（QQ 自己去下载）和 `file_data`（base64 字节）。同一时刻、同一个接口：
  `i1.hdslb.com` 走 `url` ✅，`i.ytimg.com` 走 `url` ❌ `850027 富媒体文件上传超时`
  （err_code 40034003）。**是超时，不是 404 也不是 DNS 失败** —— QQ 的机房够得到
  域名、拉不下内容。所以「图能不能显示」取决于 **QQ 机房到那个图床的连通性**，
  我们控制不了；被墙站点的图只能**我们自己下载再上传字节**。
- **botpy 的 `post_*_file` 用 `payload = locals()` 打包请求体**，所以只传得进
  **签名里声明过**的形参 —— 想加 `file_data` 必须换签名（见 `compat.py` 补丁三），
  不能指望 `**kwargs` 漏进去。改的时候也别把 `url=None` 一起打包，
  `url: null` 和 `file_data` 同时出现有被平台拒的风险。
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
