"""业务引擎直连接口:前端「工具箱」页面不经过大模型也能用,同时方便单测。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..domain.claims import assess_claim
from ..domain.returns import check_eligibility
from ..domain.shipping import estimate
from ..rag.index import index
from ..tracking.provider import query_tracking
from .schemas import ShippingIn

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.post("/shipping/estimate")
def shipping_estimate(body: ShippingIn):
    return estimate(**body.model_dump())


@router.get("/tracking")
def tracking(tracking_no: str, company: str | None = None, scenario_hint: str | None = None):
    if not tracking_no.strip():
        raise HTTPException(400, "单号不能为空")
    return query_tracking(tracking_no.strip(), company, scenario_hint)


@router.post("/claim/assess")
def claim_assess(body: dict):
    if "problem_type" not in body:
        raise HTTPException(400, "缺少 problem_type")
    return assess_claim(**body)


@router.post("/return/check")
def return_check(body: dict):
    if "category" not in body:
        raise HTTPException(400, "缺少 category")
    return check_eligibility(**body)


@router.get("/knowledge/search")
def knowledge_search(q: str = Query(min_length=1), top_k: int = 5):
    return {"query": q, "results": index.search(q, top_k=min(top_k, 10))}


@router.get("/knowledge/docs")
def knowledge_docs():
    return {"docs": index.docs, "chunks": len(index.chunks), "embedding_enabled": index.embedding_enabled}


@router.post("/knowledge/rebuild")
def knowledge_rebuild():
    index.build()
    return {"docs": len(index.docs), "chunks": len(index.chunks), "embedding_enabled": index.embedding_enabled}
