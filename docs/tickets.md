> 本文件是 `CLAUDE.md` 拆出来的一节（原样搬迁，未改动内容）。
> 常驻的精简要点在 [`../CLAUDE.md`](../CLAUDE.md)。

# ASOBI TICKET 抽票提醒（2026-10-18 加）

`tickets.py` + `db.ticket_alerts` + `TICKET_*`。`TicketPoller` 在 `bot.on_ready`
里挂成**第二个 task**（不塞进 `FeedPoller` —— 那个类已经 900+ 行，数据模型、
轮询节奏、失败语义全都不同）。

### 数据源：网站前端的那个公开接口

`asobiticket2.asobistore.jp` 是 Angular SPA，**列表页 DOM 里只有活动名和
「受付中」，一个日期都没有**。日期是前端另外调的：

    GET https://asobi-ticket.api.app.t-riple.com/api/v1/public/receptions

（接口基址是从 `main-JERHSTGS.js` 的 `apiEndpoint` 里找出来的；要再找别的
端点就枚举那 43 个 lazy chunk。）

- **完全公开、不需要登录**，814 KB / 31 条受付 + 17 booth + 17 tour + 17 image。
- **封面在另一条接口上**：`GET …/api/v1/public/booths`（16.8 KB / 17 个 booth
  + 17 张图）。**同一个 API 基址，只差最后一段路径**，所以 `booths_url()` 是从
  `TICKET_URL` 推出来的，没有新环境变量。同样支持 ETag 304。
  还有单条详情 `…/public/booths/<slug>`（998 B），用不上。
- 群晖**直连 1.43 秒**，**不用挂 `MEDIA_PROXY`**（那是给 YouTube / X 的）。
- **ETag 有效**：`If-None-Match` → **304、0 字节、0.47 秒**。所以 5 分钟轮询
  一次几乎不花什么。`TicketFeed` 带缓存，指令侧 60 秒 TTL。
- `?booth_slug=<slug>` 能单活动过滤（26 KB）。**`?brand=` / `?category=` /
  `?brand_slug=` 传了会被静默忽略**，永远返回 31 条 —— 品牌只能客户端筛。
- 详情页 `https://asobiticket2.asobistore.jp/booths/<slug>` 实测 200。
- `relationships.tour` → tour → `relationships.booth` 是**两跳**，两级都要防
  关系为 `null`（存量数据里就有）。
- ⚠️ **receptions 里 booth 的封面永远是空的**：`booth.relationships.cover_image`
  是 `{}`，而且**`?include=` 被静默忽略** —— 实测传 `include=cover_image` /
  `booth` / `tour.booth.cover_image` 五种写法，响应一律还是 814 658 字节、
  booth 照样一张封面都没有。**别再去试 include 了**，只能单独请求 `booths`。

### ⚠️ 品牌判据是 slug 前缀，**不是活动名**（三个反例钉住这条）

站点自己在 slug 前缀上打了品牌标，实测 17 个 booth 完全一致：

| 偶像大师（14 个） | 不是（3 个） |
|---|---|
| `va-liv-` `cinderellagirls-` `idolmaster-` `sidem-` `shinycolors-` `765pro-` `gkmas-` `millionlive-` | `tales-` `denonbu-` `zerokyori-` |

**光看活动名会判错。** 三个真实反例，名字里都没有 IDOLM@STER：

| 活动名 | slug | 实际品牌 |
|---|---|---|
| `xR LIVE REVIVAL` | `idolmaster-261212` | 本家 |
| `ATHOME xR LIVE　Apple of my アイ` | `cinderellagirls-270213` | 灰姑娘 |
| `shiny, the first REFRAC7IONS " Still blue "` | `shinycolors-261219` | 闪耀色彩 |

`smoke.py [4.64]` 里有三条钉子专门钉这个 —— **想改成按名字匹配的话先看它们**。
匹配用 `slug == b or slug.startswith(b + "-")`，**不是裸 `startswith`**（否则
`millionlivex-` 会被 `millionlive` 收进来）。认不出的前缀不推，但打一条 INFO。

### ⚠️ 判定用 JST，显示用北京时间（2026-10-18 用户定的）

**这是两件事，别合并。**

* **判定**：抽选截止是 **23:59 JST**，容器 TZ 是 `Asia/Shanghai`，差一小时就是
  整整早一小时截止。所有比较走 `tickets.now()` / `tickets.JST`，
  **不要用 `datetime.now()`**（那是本地时间）。
* **显示**：`_date` / `_at` / `_stamp` 统一转 `CST`（+08:00）。用户贴的样例工具
  就是北京时间 —— 同一个时刻，`23:59 JST` 显示成 `22:59`，两边都对。

两者比的是 tz-aware 的绝对时刻，所以显示用什么时区都不影响判定。**最容易犯的
错是把 `now()` 一起改成 CST**，那会让所有判定整体平移一小时。`smoke.py [4.64]`
里钉了「同一时刻 JST / CST / UTC 三种构造判定一致」。

排版本身照着用户给的样例（见 `tickets.py` 渲染段的注释）：月/日不补零、
受付期間带星期而当落发表不带、同一场的多个受付之间是一个全角空格行、
每场一张封面且图在该场序号**上面**。

### ⚠️ baseline 是**逐群**的

库里有 31 条存量受付（14 条 IM@S），直接开跑会当场倒灌十几条进群。所以一个群
**第一次**轮询时，把当前 `within_entry_period` 的全部记成已推、一条都不发。

**但締切提醒不 baseline** —— 已经进 24h/2h 窗口的照推。所以第一次启动就会收到
几条締切提醒，这是**预期行为，不是 bug**。新开的群也一样：不补发存量 `new`，
但締切照推，所以它不会「一片空白」。

判据是 **`db.ticket_alert_kinds(group)` 为空**，也就是「**这个群**一行记录都没有」。

#### ⚠️⚠️ 曾经按「整个表有没有行」判，线上真的重复推送了（2026-10-21）

旧判据是 `db.ticket_alerts_any()` —— **表里有没有任何一行**，还特意在 docstring
里写了「别改成按群查」。方向正好反了：

| | |
|---|---|
| 正式群 | 10-18 起就有 11 行记录 |
| 测试群 | 用 `/set` 打开抽票时**一行都没有** |
| 结果 | 全局判据说「不是首轮」→ 不做 baseline → **5 条几天前就推过的旧受付**被当成「新受付」连发进群 |

表里只要有**别的群**的记录，这个群就永远建不了 baseline。用户的原话：
「为什么不是新出的抽票信息又重复发了一遍」。

`ticket_alerts_any()` 已经**删掉**了 —— 留着它就有人会再写一遍全表判据。
smoke 里 `★★ 新开的群不补发存量「新受付」` 钉这一条（退回全表判据会红）。

**别去删 `ticket_alerts` 里的行**：删了这个群就会被当成首次启用，重新走一遍
baseline（现在不会倒灌了，但那天的締切提醒会重算）。

### `kind` 的尾数位数不一样（踩过）

kind ∈ `new` / `entry24` / `entry2` / `deposit24` / `deposit2`。解析前缀
**只能 `kind.rstrip("0123456789")`**。写过 `kind[:-2]`，结果 `entry2` 解析成
前缀 `entr`，静默退到**入金日期**，推出去是一条日期错误的締切 —— 而且文案上
看不出来（标签只是退化成「締切」）。smoke 里四条钉子钉这个。

### 状态和日期打架时不推

`due_alerts` 的 `new` 看服务端的 `entry_period_status`，但**还要自己算一遍
`now < entry_end`**：接口有缓存、我们的钟也可能快，状态说「受付中」而日期已过
的时候不推。締切提醒一律**按日期自己算**，不用状态。

### ⚠️ 封面有两个层级，**列表要用 booth 级**（2026-10-18 修正）

用户说「每个可选 live 不都配了一个封面吗」—— **他是对的**，之前那句
「17 场里只有 8 场有封面」是**只看了一条接口**得出的错误结论。两个层级：

| 层级 | 在哪 | 有几张 | 是什么 |
|---|---|---|---|
| **booth 级** | `…/public/booths` 的 `relationships.cover_image` | **17/17 全有** | 这场 live 的**主视觉** |
| reception 级 | `…/public/receptions` 的 `relationships.cover_image` | 31 条受付里 17 条有 | **单条受付**自己的横幅 |

两套图**没有一张重合**（实测 17 条 reception 封面与 booth 封面逐张比对，
相同 0 / 不同 17），尺寸都是 800×450 16:9，所以肉眼分不出哪个是哪个 ——
**只能靠「它挂在哪一层」判**。

`cover_url(rec, size, booths)` **优先 booth 级**，reception 级只兜底。
原因有两条，第二条是用户实际看到的 bug：

1. booth 级 17/17 全有，reception 级只有一半的受付有。
2. **列表是「一场一行、一场一张图」**。用 reception 级的话，同一场挂哪张图
   取决于**哪条受付排在最前面** —— 场内的受付一变（新增一轮先行、排序变了），
   封面就跟着换；而且 va-liv / xR LIVE / 765pro 那三场的受付**一条都没带
   封面**，于是整场没图。这正是「有些图片根本没写到文本里去」。

### ⚠️ 列表的图片预算是**场数上限**，不是 `multiimage.MAX_IMAGES`

`MAX_COVERS = LIST_LIMIT`（10）。**别改成 `multiimage.MAX_IMAGES`（6）** ——
那个数是「一篇资讯最多 6 张图」，和 `rss.MAX_IMAGES` 对齐，管的是另一回事。
拿它卡列表的症状和上面那条**一模一样**：前 6 场有图、第 7 场往后没有，
看起来就像图丢了。smoke 里有一条「8 场就配 8 张图」的钉子钉这个。

### 封面图其余规矩

图放正文**最上面**（和 `preview.py` 的 bilibili 卡片一致），所以没用
`multiimage.build()`（那个是正文在前），自己拼 `image_line → 正文 → 链接`。

- 默认 `medium`（800×450 / 144 KB）。**别用 `original`**（1920×1080 / 1.8 MB）——
  实测冷链接失败和体积强相关（1.6 MB 不炸 / 3.4 MB 全炸）。
- 读不到尺寸就**丢图发纯文字，不猜比例**（比例猜错图会被拉伸；而且
  `#Wpx #Hpx` 在手机端是必填的，缺了整条都不渲染）。
- **URL 原样用，绝不加 `&_cb=` 之类的 query 参数** —— 见上面那节，加了 QQ
  的图片代理就抓不到。
- 开关 `TICKET_IMAGES`，用户要自己测会不会不显示。
- **2026-10-22 起封面也会收进图库**（`MIRROR_IMAGES`，ticket #13）：先下载
  入库再用图库地址，某一张失败就退回它的原 URL。见 [`imgsrv.md`](imgsrv.md)。
- **booth 封面表拉失败不抛异常**（`TicketFeed.covers()` 自己吞），退到
  reception 级、再退纯文字。封面只是加分项，不该把整条列表指令搞挂。

### ⚠️ 列表里每场的链接是**单独一行**，不挂在场名上（2026-10-18 用户要求）

用户：「之前你定方案的时候是有超链接的，能加上吗」—— 方案里的样例消息确实是
这么写的，实现时漏了。现在 `/抽选` `/当落` 每一场在正文之后加一行：

```
![图1 #320px #180px](https://media.app.t-riple.com/…/44895db6-….png.800)
**1. ATHOME xR LIVE　Apple of my アイ**
アソビストアプレミアム会員先行
受付期間：2026年9月28日(月) 20:30 〜 2026年10月18日(日) 22:59
当落发表：2026年10月31日 12:00
[在 ASOBI TICKET 打开](https://asobiticket2.asobistore.jp/booths/cinderellagirls-270213)
```

`_title_line()` 只管加粗场名，`_link_line()` 单独给链接 —— **和提醒那两条
（`render_new` / `render_deadline`）是同一个写法**，文案也一模一样。

**为什么不写成 `**[场名](url)**`（链接挂在名字上，省一行）**：

- 项目里**没有任何地方**在用这个构造。bilibili 卡片（`preview.py`）是
  「标题单独一行加粗」，`bilibili.py` 里还专门写了一句「标题单独一行加粗，
  **不放进 `[文字](url)`**」。
- `**加粗**` 和 `[文字](url)` **各自都实测过**，但**套在一起没验过**。
- 而 markdown 渲染失败在这个平台上是**静默**的 —— 群里不会报错，用户只会看到
  裸的 `**1. [名字](url)**`。这就是为什么宁可多占一行。

要改成挂在场名上，**先跑 `scripts/probe_mdfmt.py` 的链接 A/B 确认**（那个探针
里 ① 就是这个构造），确认渲染正常再动 `_title_line`。

**`booth_slug` 为空时不加链接行**（返回 `None`）。`tour` 关系为 null 的存量数据
slug 是空的，硬拼出来是 `…/booths/`，点了是个 404 —— 不如干脆不加。

顺带一个好处：链接是**单独一行**的，所以活动名里的 `[` `]` **不可能跑进链接
语法**里（活动名里真的出现过方括号，smoke 用的就是 `A [B] (C) | D * E _ F`）。

### ⚠️ 图片宽度：QQ **不会**放大，定多少渲染多少（2026-10-18 用户反馈）

用户原话「图片没有占满整个文本框宽度」。原因不是渲染失败 —— **`#Wpx` 是
显示尺寸，QQ 照做，小图不会被拉大到气泡宽度**。列表里原来用的是
`multiimage.COVER_W = 208`（当初从 bilibili 封面抄的，从没实测过），
所以看着就是「没占满」。

- 抽票那条路走 `TICKET_IMAGE_W`（默认 **320**）。
- `multiimage.box_for` / `image_line` 的 `width` **默认值仍是 208** ——
  bilibili 封面和 RSS 多图一直在用，**别顺手改默认值**，那会同时动三条路。
- 这个数**取决于设备**，猜不出来。`scripts/probe_mdfmt.py --group <测试群> --send`
  会把同一张图按 208/320/480/640 发到测试群，挑刚好贴齐的那一档。
- 同一个脚本还会发一条 **markdown 语法 A/B**（`#` 标题 / `>` 引用 / `-` 列表 /
  `~~删除线~~` / `*斜体*`）。**目前只有 `**加粗**` 是验证过的**（bilibili 卡片
  一直在用），其余一律算未验证 —— 别照着文档就往生产文案里写，先跑这个探针。

### ⚠️ 丢图必须记日志，两个原因要能分开

用户反馈「有些图片根本没写到文本里去」时，日志里**什么都没有** —— 因为
`multiimage.measure` 的失败路径是 `_log.debug`，而生产是 INFO。这和项目里
「失败路径一律不许写 DEBUG」那条是同一个坑的又一次出现。

现在两条路径分开记：

| 情况 | 级别 | 说明 |
|---|---|---|
| 两个来源都没封面 | INFO | `这场两个来源都没封面` —— 只有「booth 表没拉到」或者「这场是站点新加的、封面还没配」才会出现 |
| 给了但读不到尺寸 | **WARNING** | `封面读不到尺寸` —— 要么图床挂了，要么网络抖了 |

另外 `_size_of` **只缓存成功的结果**。曾经把 `None` 也缓存住（「免得每轮重试」），
代价是一次抖动就让那张图在**整个进程生命周期**里永远不出现。指令是手动触发的，
多探一次可以忽略。smoke 里两条都钉了。

### 主动推送：预览 → 正式群（**预热已于 2026-10-22 删除**）

和 RSS 那条路**共用同一组配置**：`PREVIEW_GROUP` / `PREVIEW_VETO_SEC`。
没配预览群就是一个都不做，直接发正式群。

当年那套「预热群 + 等 45 秒」的依据是「冷链接第一次必炸」，而那条结论是在
`pbs.twimg.com` 上量到的；换成自建图床后复现不出来（见 [`imgsrv.md`](imgsrv.md)），
所以整个预热机制删掉了（ticket #10）。

现在只剩**人工预览**：

1. `PREVIEW_GROUP` → 等 `PREVIEW_VETO_SEC`（默认 30 秒，这期间可以喊停）
2. `TICKET_GROUPS` 里的正式群

- **预览群如果本身就是正式群，不能再单独发一遍**。
- **被「不发」挡下的提醒照样 `mark_ticket_alert`**，按「已推」处理 —— 不记的话
  下一轮又捞出来，你得每轮否一次。
- `_wait()` 用 `asyncio.wait_for(self._stopped.wait(), …)`，**不是裸 `sleep`** ——
  否则停机要干等。

喊停是 `bot.py` 里那条「不发」，现在**同时问两个轮询器**（`self.poller` 和
`self.tickets`），而且不能短路 —— RSS 和抽票各有自己的预览窗口，只问一个的话
正在预览的是抽票、你喊「不发」却回「什么都没挡到」。

代价：一条提醒最多晚 30 秒到正式群。抽票提醒本来就稀疏（一天几条），可以接受。

### 指令

`/抽选`（`ticket` `tickets` `抽票` `受付`）和 `/当落`（`result` `进度` `入金`）。
**只读，不加进 `RESTRICTED_CANONICAL`**（谁都能看）。**指令输出是带封面的
markdown**（一场一张，见上），走 `ctx.reply_card`；拿不到卡片通道时退回纯文本
并把图片行剥掉（`strip_images`）。数据走 60 秒内存 TTL，防群里连点刷爆接口。

⚠️ 原文写的「指令输出不带图（列十几条，带图会刷屏）」是**旧设计**，已经作废 ——
用户明确要求列表里每场都配封面。别照着那句话把 `images=` 关掉。

`scripts/probe_ticket.py` 离线看（**默认不发消息**）：`--now <ISO>` 假装成某个
时刻验提醒判定，`--scan-days N` 看未来 N 天会推什么，`--send <group_openid>`
才真发 —— **`--group` 没有默认值**，这个项目里「默认发到第一个已知群」太危险。
