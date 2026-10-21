#!/usr/bin/env bash
#
# 把当前 commit 部署到 NAS：Mac 上打 git bundle → 走 ssh 传过去 → NAS 上
# `git fetch` + `git reset --hard`。
#
# 和 tar 覆盖的区别：传的是**一个可指认的版本**。部署完 NAS 的 HEAD == 你这个
# commit，`git status` 干净，回滚是 `git reset --hard <上一个 sha>` 一条命令。
#
# **不负责重启** —— NAS 上 sudo 要密码，那一步见 scripts/wizard-deploy.sh。
#
#   scripts/deploy.sh                 正常部署（先跑 smoke）
#   scripts/deploy.sh --skip-smoke    跳过 smoke（急修时用，自己先跑过）
#   scripts/deploy.sh --force         允许在非 main 分支 / 工作区脏 / NAS 脏时部署
#
# 环境变量：NAS_HOST（默认 JackLin@100.91.151.20）、NAS_DIR
set -euo pipefail

NAS_HOST=${NAS_HOST:-JackLin@100.91.151.20}
# 服务器每次握手都会往 stderr 打三行 "post-quantum key exchange" 警告，
# 盖住真正有用的输出。LogLevel=ERROR 只留错误。
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR)
NAS_DIR=${NAS_DIR:-/volume1/docker/qq-rss-bot}
BRANCH=main
FORCE=0
SKIP_SMOKE=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    -h|--help) sed -n '3,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：${arg}（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# ---- 1. 分支 ------------------------------------------------------------
CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT" != "$BRANCH" ]]; then
  if (( FORCE )); then
    say "⚠ 当前分支是 ${CURRENT}，不是 $BRANCH —— --force 放行"
  else
    die "当前分支是 ${CURRENT}，不是 ${BRANCH}。先合回 main，或者加 --force"
  fi
fi

# ---- 2. 工作区 ----------------------------------------------------------
DIRTY=$(git status --porcelain)
if [[ -n "$DIRTY" ]]; then
  if (( FORCE )); then
    say "⚠ 工作区有未提交的改动，--force 放行 —— 这些改动**不会**被部署（只传 commit）"
  else
    printf '%s\n' "$DIRTY" | sed 's/^/    /'
    die "工作区有未提交的改动。先 commit，或者加 --force（那样它们不会被部署）"
  fi
fi

SHA=$(git rev-parse --short HEAD)
SUBJECT=$(git log -1 --format=%s)
say "部署 $SHA  $SUBJECT"

# ---- 3. smoke -----------------------------------------------------------
if (( SKIP_SMOKE )); then
  say "⚠ 跳过 smoke（--skip-smoke）"
else
  say "跑 smoke …"
  if ! uv run python scripts/smoke.py > /tmp/deploy-smoke.log 2>&1; then
    tail -20 /tmp/deploy-smoke.log | sed 's/^/    /'
    die "smoke 没通过，没部署。完整日志 /tmp/deploy-smoke.log"
  fi
  say "  $(grep -E '^结果：' /tmp/deploy-smoke.log || echo '（没找到结果行）')"
fi

# ---- 4. 打包 ------------------------------------------------------------
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
BUNDLE="$WORK/repo.bundle"
git bundle create "$BUNDLE" --all >/dev/null 2>&1
say "bundle $(du -h "$BUNDLE" | cut -f1)"

# ---- 5. NAS 那头 --------------------------------------------------------
ssh "${SSH_OPTS[@]}" "$NAS_HOST" true 2>/dev/null \
  || die "ssh 连不上 $NAS_HOST"

OLD=$(ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
  "cd $NAS_DIR && /usr/local/bin/git rev-parse --short HEAD 2>/dev/null" || true)
OLD=${OLD:-（读不到）}

# NAS 上要是有人手改过代码，reset --hard 会把它抹掉 —— 先问清楚
NAS_DIRTY=$(ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
  "cd $NAS_DIR && /usr/local/bin/git status --porcelain" 2>/dev/null || true)
if [[ -n "$NAS_DIRTY" ]]; then
  printf '%s\n' "$NAS_DIRTY" | sed 's/^/    /'
  if (( FORCE )); then
    say "⚠ NAS 工作区有改动，--force 放行 —— reset --hard 会把它们抹掉"
  else
    die "NAS 上的工作区和仓库不一致（上面这些）。确认没用后加 --force"
  fi
fi

say "传 bundle …"
ssh "${SSH_OPTS[@]}" "$NAS_HOST" "cat > $NAS_DIR/data/deploy.bundle" < "$BUNDLE"

# 远端脚本用**带引号**的 heredoc，避免本地展开；只替换两个占位符
REMOTE=$(cat <<'EOS'
set -e
cd __DIR__
G=/usr/local/bin/git
$G remote get-url bundle >/dev/null 2>&1 || $G remote add bundle __DIR__/data/deploy.bundle
$G fetch -q bundle
$G reset --hard bundle/main
printf '%s\n' '__SHA__' > data/DEPLOYED
$G rev-parse --short HEAD
EOS
)
REMOTE=${REMOTE//__DIR__/$NAS_DIR}
REMOTE=${REMOTE//__SHA__/$SHA}

NEW=$(ssh "${SSH_OPTS[@]}" "$NAS_HOST" "$REMOTE" | tail -1)

if [[ "$NEW" != "$SHA" ]]; then
  die "部署后 NAS 的 HEAD 是 ${NEW}，期望 $SHA —— 没对上，别重启"
fi

say ""
say "✓ 部署完成：NAS $OLD → $NEW"
say "  代码还没生效 —— 容器要重启才会重新 import"
say "  下一步：bash scripts/wizard-deploy.sh"
