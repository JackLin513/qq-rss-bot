# 迁移到群晖 DS220+ 的评估

评估日期：2026-10-16。**以下每一项都是对目标机实测的结果**，不是按规格推算的。
文中标注了评估过程中被实测推翻的几处初判，避免以后重走弯路。

## 结论

**可行，无阻塞项。** 唯一需要额外配置的是容器的出网代理（原因见「容器网络」一节）。
CPU 和内存都够用，磁盘余量充足，NAS 已稳定运行 23 天。

## 目标机实机信息

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

### 已在运行的容器（占用情况）

| 容器 | 内存 |
|---|---|
| TinyMediaManager (java) | 318 MB |
| qBittorrent | 235 MB |
| Emby | 210 MB |
| Tailscale | 33 MB |
| dockerd + containerd | 45 MB |

## 内存账

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

## 容器网络（本次评估最关键的部分）

### ⚠️ 宿主机测不出容器的处境

`docker` 需要 root（`/var/run/docker.sock` 是 `root:root` 660，无 docker 组，
sudo 需密码），所以最初只能在宿主机 shell 里测。**那个结论不可用**——
因为宿主机 curl 自己在走代理（`CONNECT www.v2ex.com:443` 就是证据），
测出来的是「宿主机 + 代理」能通，不代表容器能通。

最终靠一个 root 脚本在**容器内**做的实测，结论如下。

### 容器内可达性（alpine 容器内实测）

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

### 代理方案

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

## compose 骨架

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

## 迁移时的四个坑

1. **`RSSHUB_BASE` 必须从 `127.0.0.1` 改成服务名 `rsshub`。**
   容器里的 `127.0.0.1` 是容器自己，不是宿主机。忘了改的症状是抓取全部超时。
2. **bot 要容器化，不要原生装 Python。** DSM 不提供 Python 3.12，
   原生装得往系统分区塞东西（见下「数据放置」），且 `uv` 在 DSM 上不可靠。
3. **代理**（见上一节）。
4. **卷路径用 `/volume1/docker/qq-rss-bot/`，并设 `TZ=Asia/Shanghai`。**

## 数据放置

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

## 运维注意事项

- **给 `192.168.0.63`（Apple TV）做 DHCP 固定。** 它也是 DHCP 取址，
  路由器重启或地址池变动后 IP 可能变化，届时代理配置失效、所有 RSS 抓取会停。
- **Docker 是 24.0.2（偏老）**，compose 文件别用太新的特性，`version:` 字段不用写。
- 搬过去之后会**失去笔记本上那套临时环境**，但 NAS 网络更稳（笔记本走手机热点时
  实测一小时 DNS 失败 8 次、WebSocket 异常断开 4 次）。NAS 实测 `api.sgroup.qq.com`
  可达、QQ API 握手 0.26s。

## 尚未完成

- `Dockerfile` 和 `docker-compose.yml` 还没写（等功能开发完成后统一打包）
- 打包后应先在本地用 `docker compose` 完整验证一遍，能验证到
  「两个容器互通 + 机器人能推消息」；只有群晖专有的路径差异需要到机器上再调
- bot 内的容器级网络验证已通过（`api.sgroup.qq.com` 可达），
  但**双容器互通**（bot → `rsshub:1200`）尚未实测，需要在本地 compose 阶段覆盖
