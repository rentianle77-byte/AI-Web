# 服务器部署

目标环境:Linux 服务器,nginx 提供前端静态文件并反向代理 `/api` 到后端,MySQL 存数据。

> 下面所有命令在服务器上执行。假设代码放在 `/opt/express-agent`。

## CentOS 7 用户请走这里

CentOS 7 已于 2024 年 6 月停止维护,yum 源下线、自带 Python 3.6、glibc 2.17 装不了 Node 18+,
照下面的通用步骤会连续踩坑。用专门的脚本:

```bash
curl -fsSL https://raw.githubusercontent.com/rentianle77-byte/AI-Web/main/deploy/centos7-setup.sh -o setup.sh
bash setup.sh
```

脚本会自动:切 yum 源到 vault、装一份独立的 Python 3.12、建 MariaDB 库并生成连接串、
配好 systemd 与 nginx。剩下两件事要你做:填大模型 key、**在本地构建前端后上传 dist**
(CentOS 7 跑不了 Node 18+,前端必须在别处构建)。

---

## 0. 装依赖

```bash
# Ubuntu / Debian
apt update
apt install -y git nginx mysql-server python3.12 python3.12-venv curl

# CentOS / Alibaba Cloud Linux
dnf install -y git nginx mysql-server python3.12 curl
```

前端构建需要 Node 18+:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt install -y nodejs
node -v    # 要 v18 以上
```

---

## 1. 拉代码

```bash
git clone https://github.com/rentianle77-byte/AI-Web.git /opt/express-agent
cd /opt/express-agent
```

---

## 2. 建数据库

```bash
mysql -uroot -p
```

```sql
CREATE DATABASE express_agent DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'express'@'localhost' IDENTIFIED BY '换成你自己的强密码';
GRANT ALL PRIVILEGES ON express_agent.* TO 'express'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

表结构由程序启动时自动创建,不用手动建表。

---

## 3. 后端

```bash
cd /opt/express-agent
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip
backend/.venv/bin/pip install -r backend/requirements.txt
```

写配置文件:

```bash
cp backend/.env.example backend/.env
vim backend/.env
```

最少要填这几项:

```ini
LLM_PROVIDER=openai
OPENAI_API_KEY=你的LiteLLM虚拟key
OPENAI_BASE_URL=你的LiteLLM地址/v1
OPENAI_MODEL=litellm/claude-sonnet-5

DATABASE_URL=mysql+pymysql://express:刚才设的密码@127.0.0.1:3306/express_agent?charset=utf8mb4

# 前端和 /api 都走 nginx 同一个域名时,这项留空
CORS_ORIGINS=
```

```bash
chmod 600 backend/.env    # 里面有 key,别让别人读
```

先手动跑一次确认能起来:

```bash
backend/.venv/bin/uvicorn main:app --port 8000 --app-dir backend
```

看到这几行说明正常,然后 `Ctrl+C` 停掉:

```
初始化数据库(mysql)...
knowledge index built: 10 docs, 155 chunks
LLM provider=openai model=litellm/claude-sonnet-5
跟进调度器启动
```

---

## 4. 前端

```bash
cd /opt/express-agent/frontend
npm ci          # 没有 package-lock.json 时用 npm install
npm run build   # 产物在 frontend/dist
```

**前端不需要配后端地址。** 代码里请求的是相对路径 `/api/...`,由 nginx 转发,所以前后端同域名部署时什么都不用改。

---

## 5. 后端设为系统服务

```bash
cp /opt/express-agent/deploy/express-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now express-agent
systemctl status express-agent
journalctl -u express-agent -f     # 实时看日志
```

---

## 6. nginx

```bash
cp /opt/express-agent/deploy/nginx.conf /etc/nginx/conf.d/express-agent.conf
vim /etc/nginx/conf.d/express-agent.conf    # 把 server_name 改成你的 IP 或域名
nginx -t && systemctl reload nginx
```

开放端口(阿里云还要在控制台安全组里放行 80):

```bash
# ufw
ufw allow 80/tcp
# 或 firewalld
firewall-cmd --permanent --add-service=http && firewall-cmd --reload
```

打开 `http://你的IP` 就能用了。

---

## 7. 验证

```bash
curl -s http://127.0.0.1:8000/api/health          # 后端直连
curl -s http://你的IP/api/health                   # 经过 nginx
```

两条都应返回 `"status": "ok"`,且 `llm_provider` 不是 `mock`。

再验证流式输出没被 nginx 缓冲住(关键):

```bash
curl -N -X POST http://你的IP/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"你好"}'
```

文字应该**一点一点冒出来**。如果是卡几十秒后一次性全部出现,说明 `proxy_buffering off` 没生效,回去检查 nginx 配置。

---

## 更新代码

```bash
cd /opt/express-agent
git pull
backend/.venv/bin/pip install -r backend/requirements.txt   # 依赖有变动时
(cd frontend && npm ci && npm run build)                    # 前端有变动时
systemctl restart express-agent
```

---

## 四个容易踩的坑

### 1. `npm run dev` 不能用于线上

开发模式下 vite 自带的 `/api` 代理只在 `npm run dev` 时生效,`npm run build` 出来的静态文件没有代理能力。线上必须靠 nginx 转发 `/api`,这是最容易搞错的一点。

### 2. SSE 被 nginx 缓冲

这个产品的对话和主动跟进推送都走 SSE。nginx 默认开启 `proxy_buffering`,会把流式响应攒着,表现为「点了发送半天没反应,然后一次性全出来」,主动跟进的实时推送也收不到。`deploy/nginx.conf` 里已经关掉了,别删。

### 3. uvicorn 只能开 1 个 worker

主动跟进的调度器跑在进程内(`app/followup/scheduler.py`)。开多个 worker 会让每个进程都跑一份调度器,同一条跟进被触发多次,用户收到重复消息。

`deploy/express-agent.service` 里写死了 `--workers 1`。以这个产品的场景(每次请求都在等大模型,瓶颈在模型不在 CPU),单 worker 足够。真要扩容,得先把调度器拆成独立进程,并给 `followups` 表加一个基于数据库的抢占锁。

### 4. glibc 2.17 卡住一切现代工具链

CentOS 7 的 glibc 是 2.17,而如今很多官方二进制都以 2.28 为基线,会直接拒绝运行:

| 想装的东西 | 结果 |
| --- | --- |
| Node 18+ | 装不上,Vite 5 构建只能在别的机器做 |
| 最新版 Miniconda | 安装器报 `Installer requires GLIBC >=2.28` |
| 系统自带 Python | 只有 3.6.8,而依赖要求 3.10+ |

脚本的解法是 python-build-standalone 的独立构建:它的 `x86_64-unknown-linux-gnu`
目标就以 glibc 2.17 为基线,且自带 OpenSSL 3(CentOS 7 系统只有 1.0.2,装不了现代 Python)。
实测该发行包内所有二进制要求的最高 glibc 符号正好是 2.17。

### 5. MySQL 字符集

必须是 `utf8mb4`。用 `utf8` 会在存 emoji 或部分中文时报错。建库语句里已经指定了,连接串末尾的 `?charset=utf8mb4` 也不能少。

---

## 加 HTTPS(可选)

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

certbot 会自动改 nginx 配置并配好自动续期。注意它需要域名,纯 IP 签不了证书。

---

## 安全提醒

- `backend/.env` 里有大模型 key,权限设成 600,且已在 `.gitignore` 里,不会进仓库
- 后端只监听 `127.0.0.1:8000`,不直接对外,所有流量经 nginx
- 生产环境建议关掉 root 密码登录,改用 SSH 密钥:
  ```bash
  # 本机执行,把公钥传上去
  ssh-copy-id root@你的服务器IP
  # 服务器上执行,禁用密码登录
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  systemctl restart sshd
  ```
