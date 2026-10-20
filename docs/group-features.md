# 按群的功能开关（群列表**只**存在库里）

每个群单独决定收不收某个功能的推送。**群列表的唯一来源是数据库**
（`group_features` 表）——`.env` 里**一个群参数都没有**，`Config` 里也**没有**
`*_groups` 字段。

涉及 6 个功能：早安语音 / 月曜が近いよ / 抽票提醒 / 生日推送 / 誕生祭贺图 / 复读机
（`features.FEATURES`）。

## 为什么不能放 `.env`

`.env` 是 compose 的 `env_file`，环境变量在**创建容器时**烘进去。三条都堵死了：

| 事实 | 位置 |
|---|---|
| `env_file: - .env` —— 变量在创建容器时烘进去 | `docker-compose.yml:41` |
| `load_dotenv()` **默认不覆盖**已存在的环境变量 | `config.py` |
| `Config` 是 `@dataclass(frozen=True)`，进程内改不了 | `config.py` |

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

- **开 = 有一行且 `enabled=1`；关 = 有一行且 `enabled=0`（不是删行）。**
  留着 0 能看出「这个群曾经开过」，也免掉「删了会不会被什么东西灌回来」的心智负担。
- `set_group_feature` 是 **upsert**，从不删。smoke 的变异专门钉这个
  （改成 `DO NOTHING` → 挂 7 条，关不掉已经开着的群）。
- **`check_feature` 对不认识的功能名要抛，不能静默**：拼错了的症状是
  「这个功能一个群都不发」，从日志上看和「这个群关了」一模一样。

### ⚠️ `ORDER BY rowid` 是故意的

按写入顺序插入，rowid 就保住了那个顺序。`birthday_art._alert` 取的是 `[:1]`
（第一个群），而 `C2CNotifier` 拿这个群去查「谁在这个群发过指令」——
**换个群就是换了一批私聊收件人**。

⚠️ 测这条的顺序钉子**不能用字典序顺着的名字**（`AAA`/`BBB`/`CCC`）——
「按 openid 排」和「按写入顺序」结果一样，钉子会白钉（实测漏过一次）。
现在用的是 `ZZZ`/`MMM`/`AAA`。

## ⚠️ 空库 = 一个群都不发（不是「所有群」）

全新部署（空库）默认**一个群都不开**，得靠 `/set` 或 `sqlite3` 开。
这和项目其他约定一致：「空 = 关闭」比「空 = 所有群」安全 —— 忘了配，最坏是
功能没开，而不是往所有群乱发。

## ⚠️⚠️ `run()` 里**不许**因为「没群」就 return

这是落库之后最容易踩、也最阴的一个坑，**五个 poller 全都踩过一遍**：

```python
async def run(self) -> None:
    if not self.enabled:
        _log.info("早安：库里没有开着的群，功能关闭")
        return          # ← ❌ 这个 return 让 task 永久结束
```

- 放 `.env` 的时代这个 `return` 是**无害的**：群列表在容器创建时就定死了，
  想改本来就得重建容器、重启进程。
- 落库之后它变成**致命**的：启动时库里一个群都没开 → task 直接结束 →
  之后再怎么 `/set` 开群，这个轮询器**再也不会跑**，只能重启容器。
  而日志上看起来只是「功能关闭」，**完全静默**。
- 正确写法：照常打那行「功能关闭」，**然后继续进 while 循环**。
  不花什么钱 —— `poll_once` 每拍开头就有 `if not self.enabled: return 0`。

smoke 有两条钉子守着，缺一不可：

| 钉子 | 位置 | 为什么需要它 |
|---|---|---|
| **源码**：`if not self.enabled:` 那一支里不许有 `return`，且函数体要有 `while` | `_run_keeps_polling`（ast 解析） | 覆盖全部 5 个 poller |
| **行为**：空库时真起一个 task，0.05 秒后**还活着** | `_gf_run_never_dies` | 证明源码钉子说的现象是真的 |

⚠️ 只调 `poll_once` 测不出这个 —— 它本来就会因为 `if not self.enabled: return 0`
返回 0，`run()` 一上来就 return 也一样是 0。**区别只在 task 还活着没有。**

⚠️ 写 `_run_keeps_polling` 时别用 `"self.enabled" in ast.dump(node.test)`：
`ast.dump` 把它渲染成 `Attribute(value=Name(id='self'), attr='enabled')`，
**那个子串根本不出现**，钉子会永远返回 True（第一版就这么写的，月曜的 `return`
直接溜过去了）。要按 `ast.Attribute` + `attr == "enabled"` 找。

## 启动自报

```
功能开关：群列表来自数据库 —— 早安语音 2 个群；月曜が近いよ 2 个群；…
```

`features.summarize(self.db)`，在 `RssBot.on_ready` 里。**这行是「为什么这个功能
没推」的第一判据**：群数不对 = 开关的问题（`/set` 或库里那一行），不是代码的问题。
群列表既然在库里，就没有「改了 `.env` 没重启」那种静默失败了 —— 这行只是让人
一眼看到当前状态。别删。

## 改造范围

各 poller 的群列表从 `self.cfg.X_groups` 改成 `self.groups` 属性：

```python
@property
def groups(self) -> tuple[str, ...]:
    """这个功能该发到哪些群。**每次重查库** —— `/set` 改完下一拍就生效。"""
    return features.groups_for(self.db, "morning")
```

`enabled` 也必须把群数算进去（`bool(self.cfg.morning_enabled and self.groups)`）——
否则库里一个群都没开时日志会写「已启用，目标 0 个群」，很难看，而且
「功能关闭」这个信号就没了。smoke 对 5 个 poller 逐个钉了这两条。

`cfg.X_groups` **已经不存在**（反向钉子：`config.py` 里不许有
`_env_list("*_GROUPS")`、`.env.example` 里不许有 `*_GROUPS=`、5 个 poller 里
不许有 `self.cfg.X_groups`）。有人加回去的话 smoke 会红。

## `/set`（**还没实现**）

- `features.render_settings()` / `resolve_feature()` / `parse_bool()` 已经写好并测过了
- 指令本身还没接进 `commands.py`

那之前只能手工改库：

```bash
sqlite3 data/bot.db \
  "INSERT INTO group_features (group_openid, feature, enabled, updated_at) \
   VALUES ('<群 openid>', 'morning', 1, datetime('now')) \
   ON CONFLICT(group_openid, feature) DO UPDATE SET enabled = excluded.enabled"
```

⚠️ **别 DELETE**（见上）。改完**不用重启**，下一拍就生效。

### ⚠️ 群里的按钮不能直接触发动作

| `action.type` | 行为 | 群里能用吗 |
|---|---|---|
| 0 跳转 | 打开链接 | 能，但没用 |
| 1 回调 | 真·点一下就执行 | ❌ 要 `interaction` intent，当前只开了 `public_messages` |
| **2 指令** | **把文字插进输入框** | ✅ `/rss` 用的就是这个 |

而且 `enter: true`（自动发送）**只在私聊有效**。所以按钮的交互只能是
「点 → 补全指令 → 用户按发送」两步。平台限制，绕不过去。

## 曾经有过「`.env` 只用于首次灌入」，**废掉了**

那一版 `.env` 还留着 `*_GROUPS`，只是降级成灌入用的默认值，配一套
`feature_seeded` / `seed_feature` 的机制。它的毛病是**灌过之后改 `.env` 不再生效，
而日志不会报错** —— 这个项目被这种静默失败咬过好几次。

既然群列表本来就该由库管，就别留半个在 `.env` 里。现在库是唯一来源，
那个陷阱连同那套机制一起删掉了（`seed_feature` / `feature_seeded` 都不存在了）。

代价就是上面那条：**全新部署默认一个群都不开。**

## 踩过的坑

### 批量替换自杀了 `groups` 属性

写了个脚本一次性改 5 个 poller 的 `self.cfg.X_groups` → `self.groups`，
结果把 `groups` 属性里那行也换了，变成自我递归（`features.groups_for(…, self.groups)`）。
当时的形态是 `groups_for(db, "morning", cfg.morning_groups)`（第三个参数是默认值来源），
现在没有第三个参数了，但**同类风险还在**。

**教训**：批量替换要**逐行判断、跳过含 `groups_for` 的行**。而且当时脚本因为
另一个文件的锚点没找到中途崩了，`&&` 链断掉，后面那句 `grep … || echo "✅ 干净"`
**照样打出了「干净」** —— 假阳性。别把 `A && B || C` 当成「B 成功了才 C」。

### 假库要套**真的** `Database`，不能手写桩

`features.groups_for` 要 `feature_groups`。smoke 里的假库
（`_MorningDb` / `_BdDb` / 贺图那个 `_Db`）本来没有这个方法，一跑就 `AttributeError`。

现在它们混入 `_FeatureDbMixin`，**内部套一个真的 `Database`（临时文件）**。
不手写桩的理由：这一层的语义（群顺序、`enabled` 的读写）**本身就是要测的东西**
—— 手写桩等于把被测逻辑再抄一遍，抄错了还测不出来。

⚠️ `_Db` 定义在 `_ba_db()` **函数内部**，所以 `grep '^class _.*Db'` 找不到它。
找假库要连缩进的类定义一起搜。

### 变异测试跑挂时会把源码留在改坏的状态

变异脚本靠 `finally` 还原，**中途被 Ctrl-C / 打断就不会执行**。踩过一次：
`repeat.py` 带着「`groups` 缓存住第一次结果」的变异留在了工作区，
之后 smoke 一直挂一条，而那条看起来像是新写的测试有问题。

**教训**：跑完变异先 `git status` + 扫一眼关键行，别默认还原成功了。
