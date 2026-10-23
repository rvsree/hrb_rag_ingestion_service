"""Turns chunk texts into embedding vectors - ported from hrb_chatbot_service's
ai/doc_processing/embedding/embedding_generator.py. Checks the embedding cache
first, only real cache misses go to the embedding API, in one batched call."""

from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret
from rag_ingestion.gateway import get_openai_embedding_client
from rag_ingestion.pipeline.embedding_cache import EmbeddingCache, hash_text

logger = get_logger("pipeline.embedding")

DEFAULT_EMBED_MODEL = "text-embedding-3-small"

_cache = EmbeddingCache()


async def generate_embeddings(chunks: list[str], embedding_model: str | None = None) -> list[list[float]]:
    """Return one embedding vector per chunk, in order."""
    if not chunks:
        return []

    resolved_model = embedding_model or read_secret("OPENAI_EMBED_MODEL", DEFAULT_EMBED_MODEL)

    content_hashes = [hash_text(chunk) for chunk in chunks]
    cached = await _cache.get_many(content_hashes, resolved_model)

    missing_indexes = [i for i, content_hash in enumerate(content_hashes) if content_hash not in cached]
    if missing_indexes:
        missing_chunks = [chunks[i] for i in missing_indexes]
        fresh_embeddings = get_openai_embedding_client().get_embeddings(missing_chunks, model=resolved_model)
        new_entries = [
            (content_hashes[i], embedding) for i, embedding in zip(missing_indexes, fresh_embeddings, strict=True)
        ]
        await _cache.set_many(new_entries, resolved_model)
        for i, embedding in zip(missing_indexes, fresh_embeddings, strict=True):
            cached[content_hashes[i]] = embedding

    embeddings = [cached[content_hash] for content_hash in content_hashes]
    logger.info(
        "Generated %d embedding(s) (%d cache hit(s), %d cache miss(es), dimension %d)",
        len(embeddings),
        len(chunks) - len(missing_indexes),
        len(missing_indexes),
        len(embeddings[0]),
    )
    return embeddings
