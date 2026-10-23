"""Ported subset of hrb_chatbot_service's own test_embedding_generator.py."""

from rag_ingestion.pipeline import embedding
from tests.conftest import FakeEmbeddingCache, FakeEmbeddingClient


def _patch(monkeypatch, fake_client=None, cache=None):
    fake_client = fake_client or FakeEmbeddingClient(dimension=4)
    monkeypatch.setattr(embedding, "get_openai_embedding_client", lambda: fake_client)
    monkeypatch.setattr(embedding, "_cache", cache or FakeEmbeddingCache())
    return fake_client


async def test_generates_one_embedding_per_chunk_in_the_same_order(monkeypatch):
    _patch(monkeypatch)

    embeddings = await embedding.generate_embeddings(["first chunk", "second chunk", "third chunk"])

    assert len(embeddings) == 3
    for vector in embeddings:
        assert len(vector) == 4


async def test_empty_chunk_list_returns_empty_without_calling_the_client(monkeypatch):
    fake_client = _patch(monkeypatch)

    embeddings = await embedding.generate_embeddings([])

    assert embeddings == []
    assert fake_client.calls == []


async def test_a_cached_chunk_is_not_sent_to_the_real_embedding_client(monkeypatch):
    cache = FakeEmbeddingCache()
    fake_client = _patch(monkeypatch, cache=cache)

    await embedding.generate_embeddings(["first chunk"])
    assert len(fake_client.calls) == 1

    embeddings = await embedding.generate_embeddings(["first chunk", "new chunk"])

    assert len(embeddings) == 2
    assert len(fake_client.calls) == 2
    assert fake_client.calls[1]["texts"] == ["new chunk"]


async def test_a_full_cache_hit_skips_the_embedding_client_entirely(monkeypatch):
    cache = FakeEmbeddingCache()
    fake_client = _patch(monkeypatch, cache=cache)

    await embedding.generate_embeddings(["only chunk"])
    assert len(fake_client.calls) == 1

    await embedding.generate_embeddings(["only chunk"])

    assert len(fake_client.calls) == 1
