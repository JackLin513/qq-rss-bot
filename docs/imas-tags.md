> 本文件是 `CLAUDE.md` 拆出来的一节（原样搬迁，未改动内容）。
> 常驻的精简要点在 [`../CLAUDE.md`](../CLAUDE.md)。

# 偶像大师 NEWS 的企划 / 分类标签（2026-10-18 加）

推送的源名下面多一行标签，**只对偶像大师官网 NEWS 那个源生效**：

    📰 NEWS | アイドルマスター
    【灰姑娘】【演出/谷子】
    · 【シンデレラ】＼明日まで🍎／「Apple of my アイ」…

### 为什么必须自己调 CMS API

**RSS 里只有一半的属性。** 扒过 `<item>`，字段只有
`title / link / guid / pubDate / description / category`：

- RSSHub 把 CMS 的 `categories.subcategory` 映射成了 `<category>`，
  但给的是**日文原名**（`グッズ`、`ライブ・イベント`）；
- **`brand`（企划）一个字节都没输出** —— 而群里最想知道的恰恰是
  「这条是哪个企划的」。

CMS 那边其实是两个独立字段：

    categories: { code: "NEWS", subcategory: [{code:"LIVE-EVENT"},{code:"GOODS"}] }
    brand:      [{code:"MILLIONLIVE", name:"ミリオンライブ！"}]

所以 `idolmaster.py` 自己调 `cmsapi-frontend.idolmaster-official.jp`，
**一次 `Article/list` 同时拿到两个属性、而且都是 code** —— 于是不用去解析
`<category>`，也就没有「日文原名 → code」那层反查。

实测（2026-10-18）：

- CMS API 从 NAS **不走代理**直连得通（200 / 0.57s），所以用 `rss_http`
  而不是 `media_http`（后者挂了代理，是给 YouTube / X 用的）。
- CMS 的 `url` 和 RSS 的 `<link>` **完全一致**（11/11 对得上），可以按 URL 建映射。
- 一篇文章可以挂**多个企划**（180 篇里 21 篇多企划，其中 5 篇七个全挂
  —— 周年 / 通稿类），也可以挂**多个分类**（106 篇 1 个 / 65 篇 2 个 / 9 篇 3 个）。
- `brand` 数组的返回顺序是固定的，按序 join 就是想要的显示顺序。

`matches()` 按 **URL** 判而不是按订阅名判 —— 订阅名是 feed 的 `<title>`，
用户改不了，而 7 个企划路由的标题**一模一样**（都是 `NEWS | アイドルマスター`），
靠名字根本判不出来。

### 翻译表：按 code 翻，别照着 code 直译

企划（`BRAND_NAMES`，**dict 顺序就是显示顺序**）：
`IDOLMASTER` 本家 / `CINDERELLAGIRLS` 灰姑娘 / `MILLIONLIVE` 百万 /
`SIDEM` 男人 / `SHINYCOLORS` 闪彩 / `GAKUEN` 学马 / `OTHER` 其他。

两个 code **名不副实**，照着 code 直译就错了：

- **`CD`** —— 日文原名是「ミュージック」（音乐），含数字发行和主题歌，不只 CD。
  译「音乐」。
- **`LIVE-EVENT`** —— 日文是「ライブ・イベント」，涵盖演唱会**和**线下活动
  （快闪店、见面会、上映会）。译「演出」比「演唱会」准。

表里没有的 code **原样保留**（显示成 `【NEWBRAND】`）。静默丢掉的症状是
「这条怎么没标签」，比显示一个陌生英文难查得多。

### 不许破的规则

- **任何一步失败都静默降级成不带标签，绝不抛。** 这条路在推送主循环里，
  标签拿不到只是少一行字。
- **一次轮询只调一次 CMS**（`tags_for` 整批取）。逐条现取就是 12 次请求。
  非偶像大师订阅在 `tags_for` 里直接返回 `{}`，**一次请求都不发**。
- **每轮重换 token，不跨轮缓存** —— 多一个请求（~0.6s）换掉一整类
  缓存失效问题，划算。

### 为什么订一个源就够

全企划通稿（`【YouTube】ラジオ`、周年、スタマス 5 周年）会**同时**出现在
每个企划路由里。订 7 个企划路由的话同一条要推 7 遍，而且 7 遍的标签
一模一样 —— 重复反而更刺眼。实测：订满 7 个企划推 84 条，去重后只有 65 条。

（顺带：`seen_entries` 的主键是 `(subscription_id, entry_key)`，
**去重是按订阅做的，不是按群**，所以跨订阅的重复挡不住。）

`IMAS_TAGS=false` 关掉。`scripts/probe_imas.py` 离线看标签，
`--raw` 看 CMS 原始字段（回答「这条为什么没标签」）。
