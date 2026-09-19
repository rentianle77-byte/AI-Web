#!/usr/bin/env bash
# 一键本地启动:后端 8000 + 前端 5173
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 前端需要 Node 18+,本机 node@20 装在 homebrew 里
if [ -d /opt/homebrew/opt/node@20/bin ]; then
  export PATH="/opt/homebrew/opt/node@20/bin:$PATH"
fi

if [ ! -d backend/.venv ]; then
  echo "==> 创建 Python 虚拟环境"
  (python3.12 -m venv backend/.venv 2>/dev/null) || /opt/homebrew/bin/python3.12 -m venv backend/.venv
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
if [ ! -f backend/.env ]; then
  echo "==> 生成 backend/.env(默认演示模式,填入 API Key 可启用真实模型)"
  cp backend/.env.example backend/.env
  echo "DATABASE_URL=sqlite:///./data/app.db" >> backend/.env
fi
if [ ! -d frontend/node_modules ]; then
  echo "==> 安装前端依赖(node $(node -v))"
  (cd frontend && npm install)
fi

echo "==> 后端 http://127.0.0.1:8000   (API 文档 /docs)"
backend/.venv/bin/uvicorn main:app --reload --port 8000 --app-dir backend &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT

sleep 2
echo "==> 前端 http://127.0.0.1:5173"
cd frontend && npm run dev
