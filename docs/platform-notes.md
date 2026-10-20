> 本文件是 `CLAUDE.md` 拆出来的一节（原样搬迁，未改动内容）。
> 常驻的精简要点在 [`../CLAUDE.md`](../CLAUDE.md)。

# 现场验证过的结论（2026-10-16）

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
