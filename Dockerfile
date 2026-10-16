# 群晖 DS220+ 部署用。本地开发不用这个（本地直接 uv run）。
#
# 基础镜像带 uv，省掉「先装 uv 再装依赖」那一步。
# 用 bookworm-slim 而不是更新的发行版：目标机 Docker 是 24.0.2（偏老）。
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# 把 uv 的行为钉死，免得不同版本的默认值变化让构建结果漂移：
#   UV_COMPILE_BYTECODE   预编译 .pyc，容器冷启动快一点
#   UV_LINK_MODE=copy     别用硬链接（跨构建层硬链接会失效）
#   UV_PYTHON_DOWNLOADS=0 只用镜像里那个 Python，不许 uv 自己下
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先只拷依赖清单 —— 这一层能被缓存住，之后改代码不用重装依赖。
# --no-install-project：这一步只装第三方依赖，本包还没拷进来。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# 再拷源码和打包要用的东西，然后装本包。
# README.md 不能省：pyproject 里 readme = "README.md"，hatchling 打包时要读它。
COPY README.md ./
COPY src/ ./src/
# scripts/ 留着：到了 NAS 上想跑 scripts/check_sources.py 复测路由时用得着
COPY scripts/ ./scripts/
RUN uv sync --frozen

# 直接用 venv 里的解释器，不再套一层 uv run
ENV PATH="/app/.venv/bin:$PATH"

# 挂载点先在镜像里建好，容器第一次起来时属主才对
RUN mkdir -p /app/data

# **不要在这里拷 .env** —— 它由 compose 的 env_file 注入，见 .dockerignore
CMD ["python", "-m", "qq_rss_bot"]
