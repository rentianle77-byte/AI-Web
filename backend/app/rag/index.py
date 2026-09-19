"""RAG 知识库:Markdown 文档 → 分块 → BM25(jieba 分词)(+ 可选向量检索,RRF 融合)。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jieba
from rank_bm25 import BM25Okapi

from ..config import settings

jieba.setLogLevel(logging.WARNING)
log = logging.getLogger(__name__)

_STOP = set("的 了 是 在 和 与 或 及 等 有 为 对 于 也 就 都 而 及其 上 下 中 内 外 后 前 个 之 以 被 把 从 到 由 向 并 但 且 即 如 若 则 这 那 其 各 每 该 此 一 二 三 四 五 六 七 八 九 十 元 天 日 年 月".split())
_PUNCT = re.compile(r"[\s,。,.;;:：!!??、()()【】\[\]《》<>\"'“”‘’—\-—/\\|~`#*_+=]+")


def tokenize(text: str) -> list[str]:
    tokens = []
    for tok in jieba.lcut_for_search(text.lower()):
        tok = tok.strip()
        if not tok or _PUNCT.fullmatch(tok) or tok in _STOP:
            continue
        tokens.append(tok)
    return tokens


@dataclass
class Chunk:
    id: str
    doc: str
    source: str
    heading: str
    text: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "doc": self.doc, "source": self.source, "heading": self.heading, "text": self.text, "tags": self.tags}


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    meta: dict = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            for line in raw[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            raw = raw[end + 4 :]
    return meta, raw.strip()


def chunk_markdown(doc_title: str, source: str, body: str, tags: list[str], max_chars: int = 420) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading_path: list[str] = []
    buffer: list[str] = []
    counter = 0

    def flush():
        nonlocal buffer, counter
        text = "\n".join(buffer).strip()
        buffer = []
        if not text:
            return
        heading = " > ".join(heading_path) if heading_path else doc_title
        counter += 1
        chunks.append(Chunk(id=f"{source}#{counter}", doc=doc_title, source=source, heading=heading, text=text, tags=tags))

    for line in body.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush()
            level = len(m.group(1))
            heading_path = heading_path[: level - 1] + [m.group(2).strip()]
            continue
        if not line.strip():
            if sum(len(x) for x in buffer) >= max_chars * 0.6:
                flush()
            else:
                buffer.append("")
            continue
        buffer.append(line.rstrip())
        if sum(len(x) for x in buffer) >= max_chars:
            flush()
    flush()
    return chunks


class KnowledgeIndex:
    def __init__(self, knowledge_dir: Path):
        self.knowledge_dir = knowledge_dir
        self.chunks: list[Chunk] = []
        self.docs: list[dict] = []
        self._bm25: BM25Okapi | None = None
        self._vectors: list[list[float]] | None = None
        self.embedding_enabled = bool(settings.embedding_api_key and settings.embedding_base_url and settings.embedding_model)

    # ---------- 构建 ----------
    def build(self) -> None:
        self.chunks = []
        self.docs = []
        for path in sorted(self.knowledge_dir.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            title = meta.get("title") or path.stem
            tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
            doc_chunks = chunk_markdown(title, path.name, body, tags)
            self.chunks.extend(doc_chunks)
            self.docs.append({"title": title, "source": path.name, "tags": tags, "chunks": len(doc_chunks), "summary": meta.get("summary", "")})
        corpus = [tokenize(f"{c.doc} {c.heading} {c.text}") for c in self.chunks]
        self._bm25 = BM25Okapi(corpus) if corpus else None
        self._vectors = None
        if self.embedding_enabled and self.chunks:
            try:
                self._vectors = self._embed_all([f"{c.heading}\n{c.text}" for c in self.chunks])
            except Exception as exc:  # 向量失败不影响 BM25
                log.warning("embedding build failed, fallback to BM25 only: %s", exc)
                self._vectors = None
        log.info("knowledge index built: %d docs, %d chunks, vectors=%s", len(self.docs), len(self.chunks), bool(self._vectors))

    # ---------- 向量 ----------
    def _cache_path(self) -> Path:
        return settings.data_dir / "embeddings_cache.json"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        url = settings.embedding_base_url.rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}
        out: list[list[float]] = []
        for i in range(0, len(texts), 16):
            batch = texts[i : i + 16]
            resp = httpx.post(url, json={"model": settings.embedding_model, "input": batch}, headers=headers, timeout=60)
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend([d["embedding"] for d in data])
        return out

    def _embed_all(self, texts: list[str]) -> list[list[float]]:
        cache: dict = {}
        cp = self._cache_path()
        if cp.exists():
            try:
                cache = json.loads(cp.read_text())
            except Exception:
                cache = {}
        keys = [hashlib.sha1((settings.embedding_model + "::" + t).encode()).hexdigest() for t in texts]
        missing = [(k, t) for k, t in zip(keys, texts) if k not in cache]
        if missing:
            vectors = self._embed([t for _, t in missing])
            for (k, _), v in zip(missing, vectors):
                cache[k] = v
            cp.write_text(json.dumps(cache))
        return [cache[k] for k in keys]

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self.chunks or not self._bm25:
            return []
        q_tokens = tokenize(query)
        scores = self._bm25.get_scores(q_tokens) if q_tokens else [0.0] * len(self.chunks)
        bm25_rank = sorted(range(len(self.chunks)), key=lambda i: -scores[i])
        fused: dict[int, float] = {}
        k = 60.0
        for r, i in enumerate(bm25_rank[: top_k * 4]):
            if scores[i] > 0:
                fused[i] = fused.get(i, 0) + 1 / (k + r)
        if self._vectors:
            try:
                qv = self._embed([query])[0]
                sims = [self._cos(qv, v) for v in self._vectors]
                vec_rank = sorted(range(len(self.chunks)), key=lambda i: -sims[i])
                for r, i in enumerate(vec_rank[: top_k * 4]):
                    fused[i] = fused.get(i, 0) + 1 / (k + r)
            except Exception as exc:
                log.warning("vector search failed: %s", exc)
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
        results = []
        for i, score in ranked:
            d = self.chunks[i].to_dict()
            d["score"] = round(score, 5)
            d["bm25"] = round(float(scores[i]), 3)
            results.append(d)
        return results

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(x * x for x in b)) or 1e-9
        return dot / (na * nb)


index = KnowledgeIndex(settings.knowledge_dir)
