"""RAG 检索质量:知识库建得再好,检索不到也没用。"""
import pytest

from app.rag.index import index, tokenize


@pytest.fixture(scope="module", autouse=True)
def built():
    index.build()


def test_index_built():
    assert len(index.docs) >= 10
    assert len(index.chunks) > 80


def test_tokenizer_drops_stopwords_and_punct():
    toks = tokenize("快递,坏了的话,能赔多少钱?")
    assert "的" not in toks and "," not in toks
    assert any("快递" in t for t in toks)


@pytest.mark.parametrize(
    "query,expect_keyword",
    [
        ("未保价 破损 赔偿 运费倍数", "保价"),
        ("七天无理由 拆封 商品完好", "完好"),
        ("彻底延误 时限 多少天", "延误"),
        ("12305 邮政 申诉 怎么投诉", "申诉"),
        ("驿站 未经同意 代收 丢件", "驿站"),
        ("体积重 怎么算 长宽高", "体积"),
        ("充电宝 锂电池 能不能寄", "电池"),
        ("运费险 赔多少 怎么算", "运费险"),
    ],
)
def test_retrieval_hits_relevant_doc(query, expect_keyword):
    hits = index.search(query, top_k=4)
    assert hits, f"检索不到任何内容:{query}"
    blob = " ".join(h["text"] + h["heading"] + h["doc"] for h in hits)
    assert expect_keyword in blob, f"「{query}」的检索结果里没有「{expect_keyword}」"


def test_search_returns_sorted_by_score():
    hits = index.search("快递破损赔偿标准", top_k=5)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_nonsense_query_returns_little():
    assert len(index.search("紫色的大象在跳伞", top_k=5)) <= 5
