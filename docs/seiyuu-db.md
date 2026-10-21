# 声优库：角色 → 现任声优（2026-10-21 加）

一份**进仓库**的公开事实库：生日表里每个角色，现在是哪位声优在配。
它是誕生祭贺图「官号 / 声优 / 画师」三分类的数据底座（那张票是
[#2](https://github.com/JackLin513/qq-rss-bot/issues/2)，本层只做数据）。

    uv run python scripts/fetch_seiyuu.py              # 抓 + 写盘
    uv run python scripts/fetch_seiyuu.py --dry-run    # 只看统计，不写
    uv run python scripts/fetch_seiyuu.py --proxy http://192.168.0.63:6152

**只在 Mac 上手跑，没有定时任务。** 运行时只读 `src/qq_rss_bot/seiyuu.toml`，
一次网都不上。

## 文件为什么在 `src/` 下

`data/` 整个在 `.gitignore` 里（`birthdays.toml` 也在那儿），进不了仓库；
而容器只挂了 `./data` 和 `./src` 两个目录。`src/` 是唯一同时满足
「git 跟踪」和「到达容器」的位置。

## 两个页面，用 `character_id` 关联

| 页面 | 给什么 |
|---|---|
| `imas-db.jp/calendar/birthdays.html` | 角色名 + `data-character-id`（生日表里有的才算） |
| `imas-db.jp/misc/cv.html` | 同一个 `data-character-id` + 声优 + 任期 |

⚠️ **不能按名字关联。** 两边写法不一致（生日表 `エミリー・スチュアート`、
声优页 `エミリー`），按名字只能覆盖一部分，而且**失败是静默的** ——
漏掉的看起来和「这个角色没付声」一模一样。2026-10-21 就是靠用户指出
「エミリー 的 CV 是郁原ゆう」才发现。见
[`adr/0001-join-by-character-id.md`](adr/0001-join-by-character-id.md)。

## ⚠️ 声优页的两个陷阱

### `data-person-id` 是「现任」，不是「这一行的人」

同一个角色会在**每个配过它的人的行**里各出现一个 span，但那些 span 的
`data-person-id` 和 `title` 永远写的是**现任**：

    <span class="idol-name" data-character-id="3" data-person-id="41"
          title="萩原雪歩(CV:浅倉杏美)">萩原雪歩</span>

萩原雪歩的 `〜2010-07-03` 那行，行首名字是 **長谷優里奈**，可 span 上写的是
`data-person-id="41"`、`title` 还写着「CV:浅倉杏美」。拿它当行身份会张冠李戴。

### 现任取 `title`，**不要**取「第一行」

- **人的身份** → 行首 `<rb>` 里的名字
- **现任** → `title="角色(CV:名字)"` 里的名字

「第一行」也不行：真实页面上 Xenoglossia 那套 CV 按假名排序就排在前面
（`井口裕香` < `中村繪里子`），取第一行会把天海春香的现任认成井口裕香。
smoke `[4.100]` 有一条钉子专门钉这个。

## 现任 / 前任

任期只有 14 个 span 带 `<small class="date">(起始〜结束)</small>`：

| 写法 | 含义 |
|---|---|
| `(2010-07-04〜)` | 从这天起，没结束 → 现任 |
| `(〜2010-07-03)` | 已经结束 → 前任 |
| 没有 `<small>` | 没有换届记录，按现任 |

⚠️ **没有结束日期的别的 CV 不算前任** —— Xenoglossia 那套是平行世界的选角，
不是「被换掉的」。实测库里只有 7 条真前任（萩原雪歩 / 三峰結華 / 九十九一希 /
四位トレーナー）。

## 实测数字（2026-10-21）

| | |
|---|---|
| 生日表条目 | 382（其中 366 个带 `character_id`，16 个是品牌/系列，不是角色） |
| 关联上 | **263** 个角色 |
| 没有 CV | **103** 个 |
| 人物 | 263 位（同名合并后 259 + 4 位前任） |
| 有前任 | 7 个角色 |

103 个没有 CV 的分布：**cinderella 94 / sidem 6 / valiv 3** ——
和「只有企划生日和 CG 会出现无声优」这个预期吻合，多数是未付声角色。
明细在 `data/birthday/seiyuu-review.md`（不进 git），每次重跑覆盖。

⚠️ **valiv 那 3 个是特例**：vα-liv 是虚拟主播，角色自己开号，**没有**「另一个配音
的人」。他们的账号会收进 `[character_accounts]`（#5），运行时占「声优」那一格、
标签写 `本人`。所以「没有 CV」对他们是对的，不是漏抓。

## 手改与重跑

`source = "manual"` 的条目**重跑不会覆盖**：

```toml
[persons]
"郁原ゆう" = { "handle" = "sample", "source" = "manual" }

[roles]
"某个角色" = { "current" = "人工确认的名字", "source" = "manual" }
```

`source = "auto"` 的条目每次重跑都按源数据重建 —— 源里没有的会被删掉，
这是有意的（源是事实来源）。

## 运行时怎么用（下一步）

`[roles]` 的 key 是**生日表里的角色名**，运行时手里拿到的就是这个字符串，
同源查询，不引入跨源歧义。`[persons]` 是 handle 的落点（下一张票补）。
`[former_roles]` 只为留档，运行时**不用**。

smoke `[4.100]` 里有一条钉子逐个核对「每个 roles 的 key 都在 `birthdays.toml`
里」—— 生日表哪天改了名，这里会红，而不是静默失效。
