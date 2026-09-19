"""快递全流程 AI Agent —— 后端入口。

启动:
    backend/.venv/bin/uvicorn main:app --reload --port 8000 --app-dir backend
或:
    ./scripts/dev.sh
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import db_kind, init_db
from app.followup import scheduler
from app.llm.factory import get_client
from app.rag.index import index
from app.routers import chat, demo, tasks, tools_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("express-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("初始化数据库(%s)...", db_kind())
    init_db()
    log.info("构建知识库索引...")
    index.build()
    client = get_client()
    log.info("模型:%s / %s", client.provider, client.model)
    if client.provider == "mock":
        log.warning("未配置大模型 API Key,当前为演示模型。复制 backend/.env.example 为 backend/.env 并填入 Key 可启用真实 Agent。")
    scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(
    title="快递全流程 AI Agent",
    description="你的私人快递管家:索赔维权 / 退货管家 / 寄件决策",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(tasks.router)
app.include_router(tools_api.router)
app.include_router(demo.router)


@app.get("/api/health")
def health():
    client = get_client()
    return {
        "status": "ok",
        "llm_provider": client.provider,
        "llm_model": client.model,
        "database": db_kind(),
        "knowledge_chunks": len(index.chunks),
        "scheduler_running": scheduler.is_running(),
    }


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    log.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})
