# 开发 · 仓库 · 部署 · 迁移（一份看完）

> 本地（Mac）开发 → 推 GitHub → 传到 NAS 并重启 → 换机时怎么迁。
> **CLAUDE.md 只留三条部署命令和判据**，来龙去脉全在这。
>
> ⚠️ **推 GitHub 和部署到 NAS 是两件事**，中间没有自动化：
> NAS 上的代码是 **tar-over-ssh 传过去的，`.git` 没上传**（NAS 也直连不上
> github.com，要代理）。所以 `git push` 完**必须**手动同步一次，否则线上还是旧代码。

## 0. 一张图

```
Mac（开发机）                      GitHub                     NAS DS220+（生产）
─────────────                     ──────                     ─────────────────
uv sync                                                      
botctl.sh start      ──push──▶   origin/main
uv run smoke.py                                                 │
     │                                                          │ tar-over-ssh
     │  smoke 全绿                                              │ （.git / .venv
     ▼                                                          ▼   不上传）
git commit / push                                               /volume1/docker/qq-rss-bot/
                                                                     │
                                              sudo docker compose restart bot
                                                                     │
                                                          容器里 import 挂载卷的 src/
```

三条铁律：

1. **改 `src/` 或 `scripts/` 下的代码 → `restart`**（挂载卷，重启进程重新 import）。
2. **改 `.env` → `up -d`**（环境变量在容器创建时烘进去，`restart` 不重读）。
3. **判据永远是启动日志那一行** `运行代码 /app/src/qq_rss_bot（最新改动 <时间>）`
   —— 时间不对就是没生效，**别看命令有没有报错，它不会报错**。

## 1. 本地开发（Mac）

### 环境

- **Python 锁 3.11–3.12。** botpy 1.2.1 依赖链较老，本机默认的 3.14 没验证过。
- `uv sync` 装依赖。装的是**可编辑版**，`.pth` 指向 `src/`。
- Docker Desktop 只用来跑 RSSHub；bot 本体在宿主原生跑。

### 起停

```bash
scripts/botctl.sh start      # 后台启动，日志 /tmp/qq-rss-bot.log
scripts/botctl.sh restart    # 改完代码必须重启，没有热加载
scripts/botctl.sh status     # ★ 实例数，多于 1 个就是出问题了
uv run qq-rss-bot            # 前台启动（调试用，Ctrl+C 可停）
```

⚠️ **别手写 `pkill`。** `pgrep -f 'python -m qq_rss_bot'` 在 macOS 上**匹配不到**
—— 可执行文件叫 `Python`（**大写**），而 `pgrep -f` 大小写敏感。正确写法是
`pgrep -if -- '-m qq_rss_bot'`，已经封装进 `botctl.sh`。
这个坑造成过两次「以为重启了、其实旧的还在跑」，两个实例同时接网关会
**重复回复 + 重复推送**，旧进程的日志还被新进程覆盖。

### 提交前必跑

```bash
uv run python scripts/smoke.py        # 当前 1299 项，必须 0 failed
uv run python scripts/integration.py  # 端到端，假推送，不碰 QQ
```

**smoke 的项数是硬指标** —— 加功能就要加钉子，项数只增不减。

### 探针的约定

所有 `probe_*.py` **默认不发消息、一次网都不上**；要真发必须显式给
`--send <group_openid>`，而且 **`--group` 故意没有默认值**（群 openid 不是群号，
看不出是不是测试群）。批量探针里正式群尾号 `E0629` 写死拒发。

## 2. 远程仓库（GitHub）

```bash
git remote -v
# origin  ssh://git@ssh.github.com:443/JackLin513/qq-rss-bot.git
```

- **走 `ssh.github.com:443` 而不是 `github.com:22`** —— 22 端口在很多网络下被封，
  443 是 GitHub 官方的 SSH 备用端口。别改成 `git@github.com:...`。
- **单分支 `main`**，本地 `main` 跟踪 `origin/main`。这是单人项目，直接提交到 main。
- 提交信息风格：`feat:` / `fix:` / `docs:` + 中文描述，一句话说清做了什么。

### ⚠️ 推送节奏：新功能**验证通过之后**才 push

用户定的：**先在测试群真跑一次、确认没问题，再 `git push`。**

理由：部署走的是 **tar-over-ssh，不是 `git pull`** —— push **根本不触发部署**，
它纯粹是「存档」。未验证的代码存档没意义，等验证过了再存更合理。

```
做完功能 → 同步到 NAS（tar-over-ssh）→ 在测试群真跑 → 确认没问题 → git push
              ↑ 中途可以本地 commit（保留历史、方便回退），只是别推
```

### ⚠️ 绝不提交

| 路径 | 为什么 |
|---|---|
| `.env` | 真实凭据（QQ AppSecret、X cookie、Fish Audio key、DeepSeek key） |
| `data/` | 已在 `.gitignore`。含 `bot.db`、`data/morning/`（第三方资料 + 版权素材）、`data/birthday/` |
| `my-chat-history.md` | 会话导出，可能含日志片段和路径 |

**仓库是公开的。** 提交前 `git status` 扫一眼，`data/` 和 `.env` 出现在列表里
就是 `.gitignore` 被改坏了。

### 推送之后

**推完不等于部署完** —— 见开头的图。NAS 那步要单独做，见第 3 节。

## 3. 部署到 NAS

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

#### 部署到群晖（2026-10-17）

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
  和 `./scripts:/app/scripts:ro`
  （`uv sync` 装的是**可编辑版**，`.pth` 指向 `/app/src`，容器是运行时去那儿
  import 的），所以改完代码只要：
  ```bash
  sudo docker compose restart bot     # 约 2 秒
  ```
  **改 `docker-compose.yml` 本身**（比如加这个挂载）才需要 `up -d` 一次。
  改 `src/` 下的代码**永远不用重建**。
- ⚠️ **改 `.env`（尤其是加新变量）时 `restart` 不够，必须 `up -d` 重建容器。**
  compose 里是 `env_file: - .env`，环境变量在**容器创建时**烘进去，
  `docker compose restart` 只是把**现有容器**重启，**不会再读一遍 `.env`**。
  症状是**完全静默**：代码是新的（启动日志的 `最新改动 <时间>` 也对）、
  日志正常、新变量**全是代码里的默认值**。
  2026-10-18 实测踩到：15:37 往 `.env` 加了 `WARMUP_GROUP` / `PREVIEW_GROUP`，
  15:44 `restart` 之后这两个值在容器里仍然是空的 —— 预热三阶段**一次都没触发**，
  而日志里一条报错都没有。判据有两条，都是实测过的：
  ```bash
  # ① 容器里的环境变量：空的就是没读进去
  sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' qq-rss-bot-bot-1 \
    | grep -E '^(WARMUP_GROUP|PREVIEW_GROUP)='
  # ② 行为判据：三个订阅的 last_polled_at 间隔。预热生效时是 45s/30s（串行三段），
  #    没生效时是各自独立轮询的任意间隔（实测 30s/91s）
  ```
  修法：`sudo docker compose up -d bot`（**重建**，不是 `restart`）。
  **热更新那套只对 `src/` 成立；`.env` 一律要重建。**
- ⚠️⚠️ **`up -d` 对「已经在跑、配置没变」的容器是**空操作**，输出是
  `✔ Container qq-rss-bot-bot-1  Running  0.0s`。**
  **`0.0s` 不是「重启用时 0 秒」，是「检测到无需变更，直接跳过」。**
  2026-10-20 实测踩到：改完 `preview.py` 让用户 `up -d`，输出正是这一行，
  容器**根本没重启**，日志里 `运行代码 …最新改动` 还是十几小时前的。
  为什么会这样：**代码在 bind mount 里**（`./src:/app/src:ro`），
  挂载点在配置里只是一个**路径字符串**，Docker 从不哈希它的**内容** ——
  所以改 `src/` 下的文件**永远不可能**改变配置哈希、**永远触发不了重建**。
  三条命令各管各的，别混：
  ```bash
  # 只改了 src/ 或 scripts/ 下的代码（最常见）
  sudo docker compose restart bot              # 不重建，重新起进程 → 重新 import

  # 只改了 .env（加变量 / 改值）
  sudo docker compose up -d bot                # Compose 会 hash env_file，能检测到

  # 两个都改了
  sudo docker compose up -d --force-recreate bot
  ```
  ⚠️ **`up -d` 不能替代 `restart`** —— 容器健康运行时它什么都不做。
  判据永远是启动日志那一行 `运行代码 /app/src/qq_rss_bot（最新改动 <时间>）`，
  **时间不对就是没重启成功**，别看命令有没有报错（它不会报错）。
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

## 4. 迁移 / 换机

评估日期：2026-10-16。**以下每一项都是对目标机实测的结果**，不是按规格推算的。
文中标注了评估过程中被实测推翻的几处初判，避免以后重走弯路。

### 结论

**可行，无阻塞项。** 唯一需要额外配置的是容器的出网代理（原因见「容器网络」一节）。
CPU 和内存都够用，磁盘余量充足，NAS 已稳定运行 23 天。

### 目标机实机信息

| 项目 | 实测值 |
|---|---|
| 架构 | `x86_64` → `diygod/rsshub` 的 amd64 镜像原生运行，无需模拟 |
| DSM | 7.3.2（build 86009） |
| Container Manager | 24.0.2-1606（Docker 24.0.2） |
| CPU | Intel Celeron J4025，2 核 @2.0GHz，实测负载 0.09 |
| 内存 | 5,914,772 kB ≈ 5.64 GiB（已加装到 6GB） |
| 磁盘 | 23TB 卷，剩 8.3TB（已用 64%） |
| 连续运行 | 23 天 |
| 卷加密 | 是（`cryptvol_1`），重启需解锁，自动解锁工作正常 |
| 端口 1200 | 空闲 |
| Compose 项目功能 | 可用（`ContainerManager/etc/projects/` 存在） |
| `/volume1/docker` | 共享文件夹，存在 |

#### 已在运行的容器（占用情况）

| 容器 | 内存 |
|---|---|
| TinyMediaManager (java) | 318 MB |
| qBittorrent | 235 MB |
| Emby | 210 MB |
| Tailscale | 33 MB |
| dockerd + containerd | 45 MB |

### 内存账

```
实测当前已用            2464 MB
实测当前可用            2839 MB
新增 rsshub 峰值         ~650 MB   （本机压测：空载 496MB，并发 20 路由后 646MB）
新增 bot                  ~50 MB
──────────────────────────────────
加完剩余                ~2.1 GB
```

**两个需要知道的实测事实：**

1. **RSSHub 的内存不回落。** 压测后 30 秒仍是 646MB（Node.js 堆保留 + 路由缓存），
   是单调增长而非用完释放。所以看内存不能看空载值。6GB 下无顾虑，
   但如果几个月后一路涨到 2GB 以上，说明有路由泄漏，届时可用 `mem_limit: 2g`
   配合 `restart: unless-stopped` 当安全网（OOM 被杀后自动重启，只表现为一次推送延迟）。
2. **NAS 上那 1964MB 的 swap 使用全是 zram（内存压缩），不碰硬盘。**
   磁盘交换分区 `/dev/md1` 用量为 **0**。
   （评估初判说「群晖 swap 在机械盘上，一进 swap 整个 NAS 都会卡」——**错**，已推翻。）

### 容器网络（本次评估最关键的部分）

#### ⚠️ 宿主机测不出容器的处境

`docker` 需要 root（`/var/run/docker.sock` 是 `root:root` 660，无 docker 组，
sudo 需密码），所以最初只能在宿主机 shell 里测。**那个结论不可用**——
因为宿主机 curl 自己在走代理（`CONNECT www.v2ex.com:443` 就是证据），
测出来的是「宿主机 + 代理」能通，不代表容器能通。

最终靠一个 root 脚本在**容器内**做的实测，结论如下。

#### 容器内可达性（alpine 容器内实测）

| 目标 | 容器内直连 | 说明 |
|---|---|---|
| `api.sgroup.qq.com` | ✅ 通（HTTP 404，正常） | **机器人命脉，无问题** |
| `idolmaster-official.jp` | ✅ 200 | 日本站直连可通 |
| `36kr.com` | ✅ 200 | |
| `www.sspai.com` | ✅ 301 | |
| `www.v2ex.com` | ❌ 连不上 | **需代理** |
| `github.com` | ❌ 连不上 | **需代理** |

`www.v2ex.com` 的症状很具误导性：解析到 `31.13.69.245`（**Facebook 的 IP 段**），
属于 DNS 污染，连接被拒。**宿主机和容器的 DNS 答案是一样的**，
区别只在于宿主机有代理、容器没有。

#### 代理方案

| 项目 | 值 |
|---|---|
| 代理地址 | `http://192.168.0.63:6152`（Apple TV 上的 Surge，常开） |
| 可用性 | ✅ 实测 HTTP 200 |
| 容器能否到达该代理 | ✅ 能（容器内 nc 测试通过） |
| 国内站经代理的延迟 | 0.205s（vs 直连 0.2s，**无代价**，Surge 在做规则分流） |

**规则：`rsshub` 全走代理，`bot` 绝对不能走代理。**

`bot` 不能走的原因有两条，第一条是地雷：

1. **bot 要访问 `http://rsshub:1200`**，这是 Docker 内部服务名，只有容器网络 DNS 认识。
   一旦 bot 流量走代理，代理解析不了 `rsshub`，抓取会全部失败
   （症状：机器人不推送，日志全是连接错误）。
2. bot 的命脉 `api.sgroup.qq.com` 是国内站，直连已验证可通（0.26s）。
   给它套代理等于凭空增加一个「代理挂了我就掉线」的依赖。

另注：`dockerd.json` 里配了 `proxies: {"http-proxy":"192.168.0.63:6152", ...}`，
但**运行中的守护进程没有应用它**（容器内实测无任何代理环境变量）。
不要依赖这个配置，在 compose 里显式写。

### compose 骨架

```yaml
services:
  rsshub:
    image: diygod/rsshub
    restart: unless-stopped
    ports: ["1200:1200"]
    environment:
      - NODE_ENV=production
      # 全部上游走代理：国外源必需，国内源实测无延迟代价
      - HTTP_PROXY=http://192.168.0.63:6152
      - HTTPS_PROXY=http://192.168.0.63:6152
      - NO_PROXY=localhost,127.0.0.1

  bot:
    build: .
    restart: unless-stopped
    depends_on: [rsshub]
    environment:
      - RSSHUB_BASE=http://rsshub:1200        # 内部服务名，绝不能走代理
      - TZ=Asia/Shanghai
      # 显式清空：防止守护进程级代理哪天生效后把这条内部调用打断
      - HTTP_PROXY=
      - HTTPS_PROXY=
      - NO_PROXY=localhost,127.0.0.1,rsshub,api.sgroup.qq.com,bots.qq.com
    volumes:
      - ./data:/app/data
    env_file: [.env]
```

代码**不需要改动** —— `config.py` 已把 `RSSHUB_BASE` 做成环境变量。

### 迁移时的四个坑

1. **`RSSHUB_BASE` 必须从 `127.0.0.1` 改成服务名 `rsshub`。**
   容器里的 `127.0.0.1` 是容器自己，不是宿主机。忘了改的症状是抓取全部超时。
2. **bot 要容器化，不要原生装 Python。** DSM 不提供 Python 3.12，
   原生装得往系统分区塞东西（见下「数据放置」），且 `uv` 在 DSM 上不可靠。
3. **代理**（见上一节）。
4. **卷路径用 `/volume1/docker/qq-rss-bot/`，并设 `TZ=Asia/Shanghai`。**

### 数据放置

DSM 的 SSH 登录横幅会警告「数据只应存放在共享文件夹」——**这条是实质性的**：

- 共享文件夹（`/volume1/<share>/`，在 23TB 数据卷上）→ 持久，DSM 升级后仍在
- 共享文件夹之外（`/opt`、`/etc`、`/usr/local`，在 7.9GB 系统分区上）→ **升级或重置会被抹掉**

本项目全部状态都放在共享文件夹内：

```
/volume1/docker/qq-rss-bot/     ← 共享文件夹，与现有 emby/ qBittorrent/ 同级
├── docker-compose.yml
├── Dockerfile
├── .env
└── data/                        ← SQLite
```

Docker 自身数据在 `/volume1/@docker`，由 Container Manager 管理，无需干预。

### 运维注意事项

- **给 `192.168.0.63`（Apple TV）做 DHCP 固定。** 它也是 DHCP 取址，
  路由器重启或地址池变动后 IP 可能变化，届时代理配置失效、所有 RSS 抓取会停。
- **Docker 是 24.0.2（偏老）**，compose 文件别用太新的特性，`version:` 字段不用写。
- 搬过去之后会**失去笔记本上那套临时环境**，但 NAS 网络更稳（笔记本走手机热点时
  实测一小时 DNS 失败 8 次、WebSocket 异常断开 4 次）。NAS 实测 `api.sgroup.qq.com`
  可达、QQ API 握手 0.26s。

### 部署进度（2026-10-17 更新）

#### 已完成

- **`Dockerfile` / `docker-compose.yml` / `.dockerignore` 已写好**（仓库根目录）。
  基础镜像是 `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`，
  compose 里两个服务：`rsshub`（`diygod/rsshub`，端口 1200）+ `bot`（本仓库构建）。
- **本地 `docker compose` 全链路验证通过**，实测覆盖到：
  - 镜像能构建；容器内配置解析正确（`rsshub=http://rsshub:1200`、`db=/app/data/bot.db`）
  - 机器人连上网关
  - **双容器互通实测 HTTP 200，抓到 150KB 真实 feed**（这是原来唯一没验过的环节）
  - 代理分流正确：bot 侧为空、rsshub 侧走 `192.168.0.63:6152`
  - C2C 主动推送 HTTP 200
  - 挂载的 `data/` 里正常生成了 `bot.db`

#### 目标机上已就绪

代码与配置已通过 tar-over-ssh 传到 `/volume1/docker/qq-rss-bot/`：

- `Dockerfile`、`docker-compose.yml`、`.dockerignore`、`pyproject.toml`、`uv.lock`、
  `README.md`、`CLAUDE.md`、`src/`、`scripts/`
- `.env` 已生成并设为 **600**，其中 `RSSHUB_BASE=http://rsshub:1200`、
  `DB_PATH=/app/data/bot.db` 已按容器口径改写
- `.venv` / `.git` / `botpy.log` **未上传**（`.dockerignore` 也排除了它们）
- `data/` 目录已建、可写

**构建期网络实测**（2026-10-17，目标机）：

| 目标 | 结果 |
|---|---|
| `ghcr.io/v2/` | HTTP 401，0.68s —— 可达（401 是未带 token 的正常响应） |
| `registry-1.docker.io/v2/` | HTTP 401，0.85s —— 可达 |
| 经 `192.168.0.63:6152` 取 ghcr | HTTP 401，0.56s |

所以**拉镜像不需要代理**，compose 里只有 rsshub 配了代理是为了抓被污染的上游。
`/volume1` 余量 8.3T，绰绰有余。

#### docker 权限（已绕过）

`JackLin` 跑不了 docker：

```
uid=1026(JackLin) gid=100(users) groups=100(users),101(administrators)
srw-rw---- 1 root root  /var/run/docker.sock      ← 只给 root
docker: command not found                          ← CLI 也不在非 root 的 PATH 里
sudo: a password is required
```

注意 **`administrators` 组不给 docker 权限** —— DSM 上容器只认 root。
CLI 实际在 `/usr/local/bin/docker`（软链到 Container Manager 的安装目录），
PATH 是 `/usr/bin:/bin:/usr/sbin:/sbin`，所以连绝对路径都要显式写。

三条出路，任选其一（都需要用户本人操作，SSH 会话拿不到 root）：

1. **Container Manager UI 建项目**（推荐，原生工作流、以 root 运行）
2. 用户自己跑 `sudo docker compose -f /volume1/docker/qq-rss-bot/docker-compose.yml up -d --build`
3. 给 root 配 NOPASSWD sudo，或把 `JackLin` 加进 docker 组（DSM 上非标准，不推荐）

### 部署完成（2026-10-17 11:07）

机器人在 NAS 上稳定运行，迁移结束。实测确认：

| 项 | 结果 |
|---|---|
| 网关连接 | `[botpy] 机器人「炸猪排爱好者」启动成功！` + 心跳维持启动 |
| 双容器互通 | 订阅标题抓到真实值 `NEWS ｜ アイドルマスター`，`fail_count=0` |
| 订阅 URL | `http://rsshub:1200/idolmaster/news`（**不是** `127.0.0.1`） |
| 自动补订阅 | 日志 `本次给 2 个群补上了` —— 新逻辑生效 |
| baseline | 两个订阅各 12 条、`initialized=1`，**没有倒灌旧文** |
| 主动推送链路 | `send_counters` 有记录（`/new` 实测真发出去了） |
| 错误 | 日志里 ERROR / WARNING / Traceback **均为 0** |

**踩到的坑：同一套 compose 被两条路管过。** 在 Container Manager 里建了项目，
又在命令行跑了 `docker compose up`，于是 Compose 重建容器时把旧容器改名成
`<十六进制>_<原名>` 再删，中途失败就留下孤儿 —— Container Manager 列表里
能看到它、点进去说「不存在」，项目也关不掉。**一个项目只用一条路管。**
（容器管理那步只能用户本人做：DSM 上 `administrators` 组不给 docker 权限，
`sudo` 要密码，SSH 会话里干不了。）

#### 迁移到新机器时注意

`subscriptions.url` 存的是**解析后的全量地址**。老机器上是
`http://127.0.0.1:1200/...`，直接搬到容器里会指向容器自己、抓取全挂，
必须改写成 `http://rsshub:1200/...`。本次是让机器人在群里重新订阅来规避的
（`group_openid` 同 AppID 下可以直接搬，URL 不行）。

新部署的空库 `groups` 表是空的，**启动补订阅会空转**（日志写「补了 0 个群」）。
群必须先被事件看见一次（有人 @ 机器人），此后重启才会自动补上。

## 5. 故障判据速查

**先看这一节，再去翻日志。** 每一行的左列都是实际踩过的症状。

| 症状 | 判据 | 结论 / 修法 |
|---|---|---|
| 改完代码没生效 | 启动日志 `运行代码 …（最新改动 <时间>）` | 时间 ≠ 你刚保存的时间 = **没重启** |
| `up -d` 输出 `Running 0.0s` | —— | **空操作**（配置没变，跳过了），不是「0 秒重启完」 |
| 新加的 `.env` 变量在容器里是空的 | `docker inspect -f '{{range .Config.Env}}…' qq-rss-bot-bot-1` | 用了 `restart` → 改 `up -d` |
| 「构建成功」但代码没变 | `docker exec … grep -c '<新函数名>' /app/src/…` | 那条路径里根本没有 build 步骤 |
| 机器人没反应 | `scripts/botctl.sh status` | 实例数 >1 = 两个实例抢网关，重复回复 |
| 进程活着但没连上 | `lsof -p <pid> \| grep TCP` | 没有 `ESTABLISHED … :443` = 登录失败 |
| 群里静默，日志也静默 | 该功能的失败路径是不是写了 DEBUG | **失败路径一律不许写 DEBUG**（见 CLAUDE.md） |
| 群里「今天什么都没有」 | 私聊有没有告警 | cookie 失效 / 抓取失败的症状和「今天真没有」一模一样 |
| 图片裂 | 是不是冷链接？URL 上有没有 `_cb=`？ | 冷链接第一次必炸；加了 query 参数 QQ 直接抓不到 |
| 容器起没起来 | `cat data/bot.log` | `docker logs` 要 root，`/volume1/@docker` 非 root 进不去 |
| 收不到群消息事件 | `LOG_LEVEL=DEBUG` | `compat.py` 会把原始报文打出来 |
| 面板点了没反应 | 正则认不认 `<@OPENID>` 占位符 | 面板补的是占位符形态，不是显示名 |
| 按钮没渲染 | `keyboard` 有没有包 `content` 外层 | 漏了就是 HTTP 200 但按钮没了 |
| 任何「接口返回 200」 | 回读一次 | **这个平台上 200 从来不等于生效** |

### 换机 / 重建时最容易漏的

- **`data/` 不在 git 里，必须单独搬。** 里面是 `bot.db`（订阅、已见、配额计数、
  各种 posted 记账）、`data/morning/`（音频 + manifest + 头像）、`data/birthday/`。
  **`bot.db` 丢了 = 当天所有「已发」记账清零，可能重复推送。**
- **`bot.db` 里的 `group_openid` 是「群 × AppID」维度的** —— 同 AppID 下可以直接搬，
  **换 AppID 全废**。
- **`subscriptions.url` 存的是解析后的全量地址。** 从 Mac 搬到容器里必须把
  `http://127.0.0.1:1200/...` 改写成 `http://rsshub:1200/...`，否则指向容器自己。
- **`.env` 是 600**，且**每个新变量都要在目标机上补**（`env_file` 只读容器创建那一刻）。
- **补订阅只看 `groups` 表。** 空库补不了任何东西 —— 群只能从事件里学（有人 @ 一次
  或被拉进群）。全新部署时日志写「本次给 0 个群补上了」**不是功能坏了**。
- **`data/morning/.manifest.json` 必须跟 `audio/` 一起同步。** 缺了的症状是
  「早安的文字静默消失」（会退回纯语音并打 WARNING）。
