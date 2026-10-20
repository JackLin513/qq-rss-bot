# 按群的功能开关（群列表存在库里，不在 `.env`）

每个群单独决定收不收某个功能的推送。**群列表存在数据库里**（`group_features` 表），
`.env` 里的 `*_GROUPS` **只用于首次灌入**。

涉及 6 个功能：早安语音 / 月曜が近いよ / 抽票提醒 / 生日推送 / 誕生祭贺图 / 复读机
（`features.FEATURES`）。

## 为什么不能放 `.env`

`.env` 是 compose 的 `env_file`，环境变量在**创建容器时**烘进去。三条都堵死了：

| 事实 | 位置 |
|---|---|
| `env_file: - .env` —— 变量在创建容器时烘进去 | `docker-compose.yml:41` |
| `load_dotenv()` **默认不覆盖**已存在的环境变量 | `config.py:357` |
| `Config` 是 `@dataclass(frozen=True)`，进程内改不了 | `config.py:73` |

所以改一次群权限就得 `up -d --force-recreate` 重建容器 —— 而**机器人没法重建自己
的容器**（要 root，而且那等于自杀）。「在群里点一下就把功能开了」这件事，
在 `.env` 上根本做不到。

落库之后：**下一拍就生效**，不用重启、不用碰 NAS 上的文件。

## 表

```sql
CREATE TABLE IF NOT EXISTS group_features (
    group_openid TEXT NOT NULL,
    feature      TEXT NOT NULL,   -- features.FEATURES 里的名字
    enabled      INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (group_openid, feature)
);
```

### ⚠️ 支点：关掉 = 写 `enabled=0`，**不是删行**

| 库里 | 含义 |
|---|---|
| 没有这一行 | **还没灌过** → 从 `.env` 灌一次 |
| `enabled=1` | 开 |
| `enabled=0` | 关 |

删行的话，下次启动 `COUNT(*)` 是 0，又会被当成「没灌过」按 `.env` 灌回来 ——
用户会以为开关失灵，**而且日志上看不出来**。

`db.feature_seeded()` 的判据因此必须是「**有没有行**」，不是「有没有**开着**的行」。
`db.set_group_feature()` 只 upsert，从不删。smoke 里两个变异专门钉这个
（去掉守卫 / 改成只数开着的行，各挂 1 条）。

### ⚠️ `ORDER BY rowid` 是故意的

灌入时按 `.env` 的顺序插入，rowid 就保住了那个顺序。
`birthday_art._alert` 取的是 `[:1]`（第一个群），而 `C2CNotifier` 拿这个群去查
「谁在这个群发过指令」—— **换个群就是换了一批私聊收件人**。

## ⚠️⚠️ 灌过之后改 `.env` 不再生效

这正是这个项目被咬过好几次的「静默失败」形态：改了 `MORNING_GROUPS`、重启、
什么都没变，**日志一行错都没有**。所以三件事缺一不可：

1. **启动自报**（`RssBot.on_ready` 里那行，`features.summarize`）：
   ```
   功能开关：群列表来自数据库（.env 只用于首次灌入）—— 早安语音 2 个群；月曜が近いよ 0 个群；…
   ```
   它同时是**灌入点** —— `summarize` 会逐功能把 `.env` 灌进库（幂等）。
2. `/set` 能看能改（**还没实现**，见下）
3. 这份文档

### 留空 = 先不开，不是「所有群」

和项目其他约定一致（「空 = 关闭」比「空 = 所有群」安全：忘了配，最坏是功能没开，
而不是往所有群乱发）。

⚠️ 但这里有个细节：`.env` 留空时**不灌**（`seed_feature` 灌 0 行），
所以 `feature_seeded` 仍是假 —— **下次启动还会再试**。
这正好让「先把 `.env` 填上、再重启」这条路留着。

## 改造范围

各 poller 的群列表从 `self.cfg.X_groups` 改成 `self.groups` 属性：

```python
@property
def groups(self) -> tuple[str, ...]:
    """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。"""
    return features.groups_for(self.db, "morning", self.cfg.morning_groups)
```

⚠️ **`groups_for` 的第三个参数必须还是 `cfg.X_groups`** —— 它是**默认值来源**。
批量替换时把这一行也换掉的话会变成 `self.groups` 自我递归（真踩过，
见下面「踩过的坑」）。

`cfg.X_groups` **只允许出现在 `groups` 属性里**。别处还在读它 = 那个功能
`/set` 对它失效，而且症状静默。smoke 对 5 个 poller 逐个钉了这条。

## `/set`（**还没实现**）

- `features.render_settings()` / `resolve_feature()` / `parse_bool()` 已经写好并测过了
- 指令本身还没接进 `commands.py`

那之前只能手工改库：

```bash
sqlite3 data/bot.db \
  "UPDATE group_features SET enabled=0, updated_at=datetime('now') \
   WHERE feature='morning' AND group_openid='<群 openid>'"
```

⚠️ **别 DELETE**（见上）。

### ⚠️ 群里的按钮不能直接触发动作

| `action.type` | 行为 | 群里能用吗 |
|---|---|---|
| 0 跳转 | 打开链接 | 能，但没用 |
| 1 回调 | 真·点一下就执行 | ❌ 要 `interaction` intent，当前只开了 `public_messages` |
| **2 指令** | **把文字插进输入框** | ✅ `/rss` 用的就是这个 |

而且 `enter: true`（自动发送）**只在私聊有效**。所以按钮的交互只能是
「点 → 补全指令 → 用户按发送」两步。平台限制，绕不过去。

## 踩过的坑

### 批量替换自杀了 `groups` 属性

写了个脚本一次性改 5 个 poller 的 `self.cfg.X_groups` → `self.groups`，
结果把 `groups` 属性里那行**默认值来源**也换了：

```python
return features.groups_for(self.db, "morning", self.groups)   # ← 无限递归
```

**教训**：批量替换要**逐行判断、跳过含 `groups_for` 的行**。而且当时脚本因为
另一个文件的锚点没找到中途崩了，`&&` 链断掉，后面那句 `grep … || echo "✅ 干净"`
**照样打出了「干净」** —— 假阳性。别把 `A && B || C` 当成「B 成功了才 C」。

### 假库要套**真的** `Database`，不能手写桩

`features.groups_for` 需要 `seed_feature` / `feature_seeded` / `feature_groups`。
smoke 里的假库（`_MorningDb` / `_BdDb` / 贺图那个 `_Db`）本来没有这几个方法，
一跑就 `AttributeError`。

现在它们混入 `_FeatureDbMixin`，**内部套一个真的 `Database`（临时文件）**。
不手写桩的理由：这一层的语义（关掉不复活、顺序、「没灌过才灌」）**本身就是要测的
东西** —— 手写桩等于把被测逻辑再抄一遍，抄错了还测不出来。

⚠️ `_Db` 定义在 `_ba_db()` **函数内部**，所以 `grep '^class _.*Db'` 找不到它。
找假库要连缩进的类定义一起搜。
