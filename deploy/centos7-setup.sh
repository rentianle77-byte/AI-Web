#!/usr/bin/env bash
# CentOS 7 一键部署(不含前端构建,见脚本末尾说明)
#
# 用法:在服务器上以 root 执行
#   curl -fsSL https://raw.githubusercontent.com/rentianle77-byte/AI-Web/main/deploy/centos7-setup.sh -o setup.sh
#   bash setup.sh
#
# 这个脚本处理 CentOS 7 的三个特殊问题:
#   1. CentOS 7 已 EOL,官方 yum 源下线 → 切到 vault.centos.org
#   2. 自带 Python 3.6,而项目需要 3.10+ → 用 Miniconda 装 3.12(避开源码编译 OpenSSL 的坑)
#   3. Node 18+ 需要 glibc 2.28,CentOS 7 只有 2.17 → 前端不在服务器构建,在本地构建后上传

set -euo pipefail

APP_DIR=/opt/express-agent
CONDA_DIR=/opt/miniconda3
REPO=https://github.com/rentianle77-byte/AI-Web.git
DB_NAME=express_agent
DB_USER=express

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[注意] $*\033[0m"; }
die()  { echo -e "\033[1;31m[失败] $*\033[0m" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 执行"
grep -q 'CentOS Linux 7' /etc/os-release 2>/dev/null || warn "这个脚本是为 CentOS 7 写的,当前系统可能不匹配,继续执行风险自负"

# ---------------------------------------------------------------- 1. 修 yum 源
say "1/8 修复 yum 源(CentOS 7 已 EOL,官方镜像已下线)"
if ! yum repolist >/dev/null 2>&1 || ! yum -q list nginx >/dev/null 2>&1; then
  cp -an /etc/yum.repos.d /etc/yum.repos.d.bak.$(date +%s) 2>/dev/null || true
  for f in /etc/yum.repos.d/CentOS-*.repo; do
    [ -f "$f" ] || continue
    sed -i -e 's|^mirrorlist=|#mirrorlist=|g' \
           -e 's|^#\?baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' "$f"
  done
  yum clean all >/dev/null 2>&1 || true
  yum makecache fast >/dev/null 2>&1 || warn "makecache 有警告,继续"
fi
yum repolist >/dev/null 2>&1 && echo "  yum 源可用" || die "yum 源仍不可用,请检查网络或手动配置阿里云镜像"

say "2/8 安装基础软件"
yum install -y -q wget bzip2 git nginx mariadb-server mariadb || die "基础软件安装失败"
echo "  完成"

# ------------------------------------------------------------- 3. Python 3.12
say "3/8 安装 Python 3.12(通过 Miniconda,避开 CentOS 7 的 OpenSSL/gcc 版本问题)"
if [ ! -x "$CONDA_DIR/bin/conda" ]; then
  # 用 curl 不用 wget:CentOS 7 的 wget 是 1.14,不认 --show-progress。
  # 国内源优先,否则从 repo.anaconda.com 下 150MB 会非常慢。
  MC=/tmp/miniconda.sh
  rm -f "$MC"
  for u in \
    https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    https://mirrors.aliyun.com/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    https://mirrors.bfsu.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh \
    https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
  do
    echo "  尝试:${u%%/anaconda*}"
    if curl -fL --connect-timeout 10 --retry 2 -o "$MC" "$u" && [ -s "$MC" ]; then
      echo "  下载完成($(du -h "$MC" | cut -f1))"
      break
    fi
    rm -f "$MC"
  done
  [ -s "$MC" ] || die "Miniconda 下载失败,请检查服务器能否访问外网"

  # 上一次跑到一半留下的残目录会让安装器直接罢工
  # (报 "File or directory already exists"),先清掉。
  if [ -d "$CONDA_DIR" ] && [ ! -x "$CONDA_DIR/bin/conda" ]; then
    echo "  清理上次残留的 $CONDA_DIR"
    rm -rf "$CONDA_DIR"
  fi

  # -u 允许覆盖已有安装;不要把输出丢掉,否则出错时无从排查
  echo "  开始安装(约 1 分钟)..."
  if ! bash "$MC" -b -u -p "$CONDA_DIR"; then
    die "Miniconda 安装失败,错误见上方输出。常见原因:磁盘空间不足、/opt 权限问题、glibc 过旧"
  fi
  rm -f "$MC"
fi
[ -x "$CONDA_DIR/bin/conda" ] || die "$CONDA_DIR/bin/conda 不存在,安装未成功"
echo "  Miniconda 就绪:$("$CONDA_DIR/bin/conda" --version)"

# --------------------------------------------------------------- 4. 拉取代码
say "4/8 拉取代码到 $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only || warn "git pull 失败,使用现有代码"
elif [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
  # 目录已存在且非空(比如前端 dist 已经先传上来了)。
  # git clone 不允许克隆进非空目录,所以先克隆到临时目录再合并,
  # 已上传的 frontend/dist 和 backend/.env 都会原样保留。
  echo "  $APP_DIR 已有内容,改用合并方式(保留已上传的前端与配置)"
  TMP="$(mktemp -d)"
  git clone --depth 1 "$REPO" "$TMP/repo" || die "克隆失败"
  cp -rn "$TMP/repo/." "$APP_DIR/" 2>/dev/null || true
  cp -r "$TMP/repo/.git" "$APP_DIR/.git"
  rm -rf "$TMP"
  git -C "$APP_DIR" checkout -- . 2>/dev/null || true
else
  git clone --depth 1 "$REPO" "$APP_DIR" || die "克隆失败"
fi
echo "  $(git -C "$APP_DIR" log --oneline -1)"
[ -f "$APP_DIR/frontend/dist/index.html" ] && echo "  已检测到前端 dist,无需再上传"

# ------------------------------------------------------------- 5. Python 环境
say "5/8 创建 Python 3.12 环境并安装依赖(约 2-4 分钟)"
VENV="$APP_DIR/backend/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  "$CONDA_DIR/bin/conda" create -y -p "$VENV" python=3.12 || die "创建 Python 环境失败,错误见上方"
fi
"$VENV/bin/pip" install -q --upgrade pip
echo "  从清华源安装依赖..."
if ! "$VENV/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$APP_DIR/backend/requirements.txt"; then
  echo "  清华源失败,改用官方源重试(会慢一些)"
  "$VENV/bin/pip" install -r "$APP_DIR/backend/requirements.txt" || die "依赖安装失败,错误见上方"
fi
echo "  $("$VENV/bin/python" -V) 依赖安装完成"

# ------------------------------------------------------------------ 6. 数据库
say "6/8 配置数据库(MariaDB,与 MySQL 协议兼容)"
# CentOS 7 的 systemd 是 219,不支持 enable --now,必须拆成两条
systemctl enable mariadb >/dev/null 2>&1 || true
systemctl start mariadb >/dev/null 2>&1 || die "MariaDB 启动失败,执行 journalctl -u mariadb 查看原因"
sleep 2
if [ -f "$APP_DIR/backend/.db_password" ]; then
  DB_PASS="$(cat "$APP_DIR/backend/.db_password")"
  echo "  复用已有的数据库密码"
else
  # 不用 tr </dev/urandom | head:set -o pipefail 下 tr 会因 SIGPIPE 让整条管道失败
  DB_PASS="$(openssl rand -hex 12)"
  umask 077 && echo "$DB_PASS" > "$APP_DIR/backend/.db_password"
  echo "  已生成数据库密码,存于 backend/.db_password"
fi
# CentOS 7 自带 MariaDB 5.5,不支持 CREATE USER IF NOT EXISTS(10.1.3 才有),
# 用 GRANT ... IDENTIFIED BY,它在旧版里会自动建用户,新版也兼容。
mysql -uroot <<SQL || die "数据库配置失败(若 root 已设密码,请手动执行 deploy/DEPLOY.md 第 2 步建库)"
CREATE DATABASE IF NOT EXISTS $DB_NAME DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON $DB_NAME.* TO '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
FLUSH PRIVILEGES;
SQL
echo "  数据库 $DB_NAME 就绪"

# ------------------------------------------------------------------ 7. 配置文件
say "7/8 生成配置文件"
ENV_FILE="$APP_DIR/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<ENV
# ===== 大模型:填你的 LiteLLM 信息 =====
LLM_PROVIDER=openai
OPENAI_API_KEY=在这里填你的LiteLLM_key
OPENAI_BASE_URL=在这里填你的LiteLLM地址/v1
OPENAI_MODEL=litellm/claude-sonnet-5

# ===== 数据库(已自动配好)=====
DATABASE_URL=mysql+pymysql://$DB_USER:$DB_PASS@127.0.0.1:3306/$DB_NAME?charset=utf8mb4

# ===== 其他 =====
# 前端和 /api 同域名走 nginx,不需要跨域配置
CORS_ORIGINS=
FOLLOWUP_CHECK_INTERVAL=10
LLM_EFFORT=medium
ENV
  chmod 600 "$ENV_FILE"
  echo "  已生成 $ENV_FILE(数据库连接串已自动填好)"
else
  echo "  $ENV_FILE 已存在,不覆盖"
fi

# --------------------------------------------------------------- 8. 服务与网关
say "8/8 配置 systemd 与 nginx"
cp "$APP_DIR/deploy/express-agent.service" /etc/systemd/system/
systemctl daemon-reload

mkdir -p /etc/nginx/conf.d
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/conf.d/express-agent.conf
IP="$(curl -s -m 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
[ -n "$IP" ] && sed -i "s/server_name .*/server_name ${IP};/" /etc/nginx/conf.d/express-agent.conf
# CentOS 7 默认 nginx.conf 里有个 listen 80 default_server 的欢迎页站点,
# 不处理的话直接访问 IP 会打到 nginx 欢迎页,而不是我们的前端。
# 把它的 default_server 摘掉,改由我们的站点接管。
if grep -qE 'listen\s+80\s+default_server' /etc/nginx/nginx.conf 2>/dev/null; then
  cp -n /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak 2>/dev/null || true
  sed -i -E 's/listen\s+80\s+default_server;/listen 80;/' /etc/nginx/nginx.conf
  sed -i -E 's/listen\s+\[::\]:80\s+default_server;/listen [::]:80;/' /etc/nginx/nginx.conf
fi
sed -i 's/^    listen 80;/    listen 80 default_server;/' /etc/nginx/conf.d/express-agent.conf
nginx -t >/dev/null 2>&1 && echo "  nginx 配置检查通过" || warn "nginx -t 未通过,稍后手动执行 nginx -t 查看原因"

systemctl enable nginx >/dev/null 2>&1 || true
firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
firewall-cmd --reload >/dev/null 2>&1 || true

cat <<'DONE'

────────────────────────────────────────────────────────────
  服务器端准备完成。还差两步,都要你来做:
────────────────────────────────────────────────────────────

【第一步】填大模型配置

    vi /opt/express-agent/backend/.env

  只需改 OPENAI_API_KEY 和 OPENAI_BASE_URL 两项。
  数据库连接串已经自动填好了,不用动。

【第二步】上传前端(必须在你的 Mac 上构建)

  CentOS 7 的 glibc 是 2.17,而 Node 18+ 需要 2.28,
  所以前端没法在这台机器上构建。在你的 Mac 上执行:

    cd ~/Desktop/express-agent/frontend
    export PATH="/opt/homebrew/opt/node@20/bin:$PATH"
    npm run build
    scp -r dist root@你的服务器IP:/opt/express-agent/frontend/

  产物只有 140KB,几秒就传完。

【然后启动】

    systemctl start express-agent && systemctl start nginx
    systemctl status express-agent
    journalctl -u express-agent -f      # 看日志

【验证】

    curl -s http://127.0.0.1:8000/api/health     # 应返回 status: ok
    curl -s http://你的IP/api/health              # 经过 nginx

  最后别忘了在阿里云控制台的安全组里放行 80 端口,
  只在服务器上开 firewalld 是不够的。

────────────────────────────────────────────────────────────
DONE
