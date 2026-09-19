#!/usr/bin/env bash
# Ubuntu 一键部署(22.04 / 24.04 / 26.04,含前端构建,全部在服务器完成)
#
# 用法:以 root 执行
#   curl -fsSL https://raw.githubusercontent.com/rentianle77-byte/AI-Web/main/deploy/ubuntu-setup.sh -o setup.sh
#   bash setup.sh
#
# 和 CentOS 7 的区别:Ubuntu 22.04+ 的 glibc 是 2.35 起步,Node 20 和现代 Python 轮子
# 都能直接用,所以前端也能在服务器上构建,不用先在本地打包再上传。

set -euo pipefail

APP_DIR=/opt/express-agent
REPO=https://github.com/rentianle77-byte/AI-Web.git
DB_NAME=express_agent
DB_USER=express

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[注意] $*\033[0m"; }
die()  { echo -e "\033[1;31m[失败] $*\033[0m" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 执行"
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || warn "这个脚本是为 Ubuntu 写的,当前是 ${PRETTY_NAME:-未知系统}"

export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------ 1. 基础包
say "1/8 更新软件源并安装基础软件"
apt-get update -qq || die "apt update 失败,检查网络"
# build-essential 与 python3-dev 是保险:万一系统自带的 Python 版本太新、
# 某些包还没出预编译轮子,pip 可以现场编译。Ubuntu 的 gcc 足够新,
# 不像 CentOS 7 的 gcc 4.8.5 连 C++17 都不支持。
apt-get install -y -qq curl git nginx mysql-server ca-certificates gnupg \
                      build-essential python3-dev pkg-config >/dev/null \
  || die "基础软件安装失败"
echo "  完成"

# ------------------------------------------------------------------ 2. Python
say "2/8 准备 Python(需要 3.10 以上)"
# 不同 Ubuntu 版本自带的 Python 不一样(22.04 是 3.10、24.04 是 3.12、26.04 更新),
# 所以按优先级找一个能用的,而不是写死版本号。
# 优先 3.12/3.13:这两个版本第三方轮子最齐全;太新的版本有些包还没出预编译包。
pick_python() {
  for c in python3.12 python3.13 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null \
      && { echo "$c"; return 0; }
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  echo "  系统没有 3.10+ 的 Python,尝试从 apt 安装"
  apt-get install -y -qq python3.12 python3.12-venv >/dev/null 2>&1 \
    || apt-get install -y -qq python3 python3-venv >/dev/null 2>&1 || true
  PY="$(pick_python || true)"
fi
[ -n "$PY" ] || die "找不到 3.10 以上的 Python,请手动安装后重跑"

# venv 模块在 Ubuntu 上是单独的包,缺了会在建虚拟环境时才报错
if ! "$PY" -m venv --help >/dev/null 2>&1; then
  PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  apt-get install -y -qq "python${PYV}-venv" >/dev/null 2>&1 \
    || apt-get install -y -qq python3-venv >/dev/null 2>&1 \
    || die "无法安装 venv 模块(python${PYV}-venv)"
fi
echo "  使用 $("$PY" -V)"

# -------------------------------------------------------------------- 3. Node
say "3/8 准备 Node(构建前端需要 18 以上)"
node_major() { command -v node >/dev/null 2>&1 && node -v | sed 's/v\([0-9]*\).*/\1/' || echo 0; }

if [ "$(node_major)" -lt 18 ]; then
  # 先试系统源:新版 Ubuntu 自带的 nodejs 通常已经够新,比加第三方源稳
  apt-get install -y -qq nodejs npm >/dev/null 2>&1 || true
fi
if [ "$(node_major)" -lt 18 ]; then
  echo "  系统源的 Node 太旧,改用 NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 \
    || warn "NodeSource 配置失败(可能还不支持本系统版本)"
  apt-get install -y -qq nodejs >/dev/null 2>&1 || true
fi
[ "$(node_major)" -ge 18 ] || die "Node 18+ 安装失败,当前 $(node -v 2>/dev/null || echo '未安装')"
command -v npm >/dev/null 2>&1 || apt-get install -y -qq npm >/dev/null 2>&1 || true
echo "  Node $(node -v) / npm $(npm -v 2>/dev/null || echo '缺失')"

# ------------------------------------------------------------------ 4. 拉代码
say "4/8 拉取代码到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  ( cd "$APP_DIR" && git fetch --depth 1 origin main && git reset --hard origin/main ) \
    || warn "更新失败,使用现有代码"
elif [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
  TMP="$(mktemp -d)"
  git clone --depth 1 "$REPO" "$TMP/repo" || die "克隆失败"
  cp -rn "$TMP/repo/." "$APP_DIR/" 2>/dev/null || true
  cp -r "$TMP/repo/.git" "$APP_DIR/.git"
  rm -rf "$TMP"
else
  git clone --depth 1 "$REPO" "$APP_DIR" || die "克隆失败"
fi
echo "  $(cd "$APP_DIR" && git log --oneline -1)"

# -------------------------------------------------------------- 5. Python 依赖
say "5/8 创建虚拟环境并安装后端依赖(约 2 分钟)"
VENV="$APP_DIR/backend/.venv"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV" || die "创建虚拟环境失败"
"$VENV/bin/pip" install -q --upgrade pip
echo "  从清华源安装..."
if ! "$VENV/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$APP_DIR/backend/requirements.txt"; then
  echo "  清华源失败,改用官方源"
  "$VENV/bin/pip" install -r "$APP_DIR/backend/requirements.txt" || die "依赖安装失败,错误见上方"
fi
echo "  $("$VENV/bin/python" -V) 依赖就绪"

# ------------------------------------------------------------------ 6. 前端
say "6/8 构建前端"
cd "$APP_DIR/frontend"
npm install --no-audit --no-fund --loglevel=error || die "npm install 失败"
npm run build || die "前端构建失败"
[ -f "$APP_DIR/frontend/dist/index.html" ] || die "构建产物缺失"
echo "  dist 就绪($(du -sh dist | cut -f1))"
cd - >/dev/null

# ------------------------------------------------------------------ 7. 数据库
say "7/8 配置 MySQL"
systemctl enable --now mysql >/dev/null 2>&1 || systemctl enable --now mysqld >/dev/null 2>&1 || true
sleep 2
if [ -f "$APP_DIR/backend/.db_password" ]; then
  DB_PASS="$(cat "$APP_DIR/backend/.db_password")"
  echo "  复用已有数据库密码"
else
  DB_PASS="$(openssl rand -hex 12)"
  umask 077 && echo "$DB_PASS" > "$APP_DIR/backend/.db_password"
  echo "  已生成数据库密码,存于 backend/.db_password"
fi
mysql <<SQL || die "数据库配置失败"
CREATE DATABASE IF NOT EXISTS $DB_NAME DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost';
FLUSH PRIVILEGES;
SQL
echo "  数据库 $DB_NAME 就绪"

ENV_FILE="$APP_DIR/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENV
# ===== 大模型:只需改下面两行 =====
LLM_PROVIDER=openai
OPENAI_API_KEY=在这里填你的LiteLLM_key
OPENAI_BASE_URL=在这里填你的LiteLLM地址/v1
OPENAI_MODEL=litellm/claude-sonnet-5

# ===== 数据库(已自动配好,不用动)=====
DATABASE_URL=mysql+pymysql://$DB_USER:$DB_PASS@127.0.0.1:3306/$DB_NAME?charset=utf8mb4

# ===== 其他 =====
CORS_ORIGINS=
FOLLOWUP_CHECK_INTERVAL=10
LLM_EFFORT=medium
ENV
  chmod 600 "$ENV_FILE"
  echo "  已生成 $ENV_FILE"
else
  echo "  $ENV_FILE 已存在,不覆盖"
fi

# -------------------------------------------------------------- 8. 服务与网关
say "8/8 配置 systemd 与 nginx"
cp "$APP_DIR/deploy/express-agent.service" /etc/systemd/system/
systemctl daemon-reload

cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/conf.d/express-agent.conf
IP="$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
[ -n "$IP" ] && sed -i "s/server_name .*/server_name ${IP};/" /etc/nginx/conf.d/express-agent.conf
# Ubuntu 的 nginx 默认站点占着 default_server,不去掉会优先匹配到它的欢迎页
rm -f /etc/nginx/sites-enabled/default
sed -i 's/^    listen 80;/    listen 80 default_server;/' /etc/nginx/conf.d/express-agent.conf
nginx -t >/dev/null 2>&1 && echo "  nginx 配置检查通过" || warn "nginx -t 未通过,手动执行 nginx -t 查看"

systemctl enable nginx >/dev/null 2>&1 || true
command -v ufw >/dev/null 2>&1 && ufw allow 80/tcp >/dev/null 2>&1 || true

cat <<DONE

────────────────────────────────────────────────────────────
  部署完成。只差填一个 key:
────────────────────────────────────────────────────────────

    vi $APP_DIR/backend/.env

  只改 OPENAI_API_KEY 和 OPENAI_BASE_URL 两行。
  数据库连接串已自动填好,不用动。

  然后启动:

    systemctl restart express-agent nginx
    systemctl status express-agent --no-pager

  验证:

    curl -s http://127.0.0.1:8000/api/health
    curl -s http://${IP:-你的IP}/api/health

  两条都应返回 "status": "ok",且 llm_provider 是 openai 而不是 mock。

  最后别忘了在阿里云控制台的安全组里放行 80 端口 ——
  这一步在服务器上做不了,是最容易漏的一步。

  浏览器打开:http://${IP:-你的IP}

────────────────────────────────────────────────────────────
DONE
