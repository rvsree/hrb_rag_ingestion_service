"""Redis-backed embedding cache - ported from hrb_chatbot_service's
common/clients/cache_client/embedding_cache.py, with one real adaptation: the
Redis key uses hrb_common.cache's build_cache_key() convention
("rag_ingestion:embedding:{model}:{content_hash}") instead of the original's own
"embedding_cache:{model}:{content_hash}" prefix - this is a separate service with
its own Redis keyspace now, not sharing hrb_chatbot_service's cache, so the exact
key string doesn't need to match. Exact-match on (content_hash, embedding_model),
avoids re-embedding unchanged chunk text on re-index."""

import hashlib
import json

import redis.asyncio as redis

from hrb_common.cache import build_cache_key
from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret

logger = get_logger("pipeline.embedding_cache")

DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days - a given content hash's embedding never changes


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redis_key(content_hash: str, embedding_model: str) -> str:
    return build_cache_key("rag_ingestion", "embedding", f"{embedding_model}:{content_hash}")


class EmbeddingCache:
    PROVIDER_NAME = "redis"

    def __init__(self, url: str | None = None):
        self.url = read_secret("REDIS_URL", "", passed_in_value=url)
        self.ttl_seconds = int(read_secret("REDIS_EMBEDDING_CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))

        if not self.url:
            logger.warning("[REDIS] REDIS_URL not set")

        self._client: redis.Redis | None = None

    def _client_or_raise(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client

    async def get_many(self, content_hashes: list[str], embedding_model: str) -> dict[str, list[float]]:
        """Never raises, even if Redis is unreachable: degrades to "embed everything"."""
        if not content_hashes:
            return {}
        try:
            client = self._client_or_raise()
            keys = [_redis_key(content_hash, embedding_model) for content_hash in content_hashes]
            values = await client.mget(keys)
            return {
                content_hash: json.loads(value)
                for content_hash, value in zip(content_hashes, values, strict=True)
                if value is not None
            }
        except Exception as error:
            logger.warning("[embedding_cache] Redis unreachable on get_many, treating as a full miss: %s", error)
            return {}

    async def set_many(self, entries: list[tuple[str, list[float]]], embedding_model: str) -> None:
        """Best-effort - a failed cache write must never break ingestion."""
        if not entries:
            return
        try:
            client = self._client_or_raise()
            async with client.pipeline(transaction=False) as pipe:
                for content_hash, embedding in entries:
                    pipe.set(_redis_key(content_hash, embedding_model), json.dumps(embedding), ex=self.ttl_seconds)
                await pipe.execute()
        except Exception as error:
            logger.warning("[embedding_cache] Redis unreachable on set_many, skipping cache write: %s", error)
