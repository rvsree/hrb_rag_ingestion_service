"""Lambda entry point - SQS (triggered by an S3 ObjectCreated event) -> index
one document. A thin adapter: reads the event, downloads the object, calls
pipeline.py's existing index_document() unchanged - ported from
hrb_chatbot_service's lambda_handlers/index_document_handler.py (Phase 88/89
there). The one real adaptation: this repo's own index_document() takes
metadata_store as an explicit argument (no combined db_gateway here), so
this handler builds one via get_metadata_store() and passes it through."""

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import unquote_plus

import boto3

from rag_ingestion.content_hash import compute_content_hash
from rag_ingestion.documents.store import get_metadata_store
from rag_ingestion.pipeline import pipeline

# nosec B108 - bandit flags any hardcoded /tmp as a predictable-path risk
# (relevant on a shared multi-user machine); AWS Lambda gives each
# invocation its own isolated execution environment, and /tmp is the only
# writable directory available at all - this is the mandatory, standard
# Lambda pattern, not an arbitrary or insecure choice.
TMP_DIRECTORY = Path("/tmp")  # nosec B108


class _JsonFormatter(logging.Formatter):
    """One JSON line per log record - CloudWatch Logs Insights can then query
    fields directly, unlike the rest of this project's deliberately
    plain-text logs (see the original Phase 88 spec for why Lambda alone gets this)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("rag_ingestion.lambda_handler")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


logger = _get_logger()


def _parse_s3_event(sqs_record: dict) -> tuple[str, str] | None:
    """Return (bucket, key) from one SQS message body, or None for a non-upload
    event (e.g. the s3:TestEvent S3 sends when a notification is first configured).
    S3's own ObjectCreated notification URL-encodes the key (a space becomes "+",
    other characters percent-encoded) - decoded here so a real filename with a
    space in it downloads from the real key."""
    body = json.loads(sqs_record["body"])
    if body.get("Event") == "s3:TestEvent":
        return None

    s3_info = body["Records"][0]["s3"]
    return s3_info["bucket"]["name"], unquote_plus(s3_info["object"]["key"])


def _document_id_and_filename(key: str) -> tuple[str, str]:
    """Key convention: "{document_id}/{filename}" - the same layout
    documents/service.py already uses for local uploads."""
    document_id, _, filename = key.partition("/")
    return document_id, filename or key


def _pending_overrides_kwargs(existing: dict) -> dict:
    """The presigned-upload route stashes chunk_info/document_metadata on the
    row it already created - unpack them into index_document()'s own kwargs.
    Returns {} if there's nothing pending (the direct-S3-put fallback path,
    or a retry after they were already cleared)."""
    raw = existing.get("pending_overrides")
    if not raw:
        return {}

    overrides = json.loads(raw)
    kwargs = {}
    chunk_info = overrides.get("chunk_info")
    if chunk_info:
        kwargs["chunking_strategy"] = chunk_info.get("chunking_strategy")
        kwargs["chunk_size"] = chunk_info.get("chunk_size")
        kwargs["chunk_overlap"] = chunk_info.get("chunk_overlap")
    document_metadata = overrides.get("document_metadata")
    if document_metadata:
        kwargs["document_metadata_override"] = document_metadata
    return kwargs


async def _index_one(bucket: str, key: str) -> None:
    document_id, filename = _document_id_and_filename(key)
    local_path = TMP_DIRECTORY / document_id / filename
    local_path.parent.mkdir(parents=True, exist_ok=True)

    metadata_store = get_metadata_store()
    # Checked before the download (not after) so a real, possibly-slow S3
    # transfer has a status to show while it runs - only possible when the
    # row already exists (the presigned-upload path); the row-less fallback
    # path below still has nothing to update yet.
    existing = await metadata_store.get_document(document_id)
    if existing is not None:
        await metadata_store.update_status(document_id, "downloading")

    s3 = boto3.client("s3")
    s3.download_file(bucket, key, str(local_path))

    if existing is None:
        # No API step created this row yet - a direct-S3-put fallback path,
        # still useful for manual testing. The real presigned-upload endpoint
        # always creates this row first.
        content = local_path.read_bytes()
        content_hash = compute_content_hash(content)
        await metadata_store.create_document(document_id, filename, str(local_path), len(content), content_hash)
        index_kwargs = {}
    else:
        index_kwargs = _pending_overrides_kwargs(existing)

    # index_document() itself sets status to "parsing" as its very first
    # action - a separate status update here would be overwritten within
    # microseconds, so it's not worth a second DB write.

    try:
        await pipeline.index_document(metadata_store, document_id, str(local_path), **index_kwargs)
        if index_kwargs:
            await metadata_store.set_pending_overrides(document_id, None)
    except Exception as error:
        logger.error(
            "Indexing failed for document %s (bucket=%s, key=%s): %s: %s",
            document_id, bucket, key, type(error).__name__, error,
        )
        # The document's current status is the last stage it actually
        # reached - read it before overwriting it to "failed".
        current = await metadata_store.get_document(document_id)
        failed_stage = current["status"] if current else "unknown"
        await metadata_store.update_status(document_id, "failed", f"[{failed_stage}] {error}")
        raise


async def _process_one_record(record: dict) -> str | None:
    """One record's own download/parse/chunk/embed/index - returns its
    messageId if it failed, else None. Exceptions are caught here, not left
    to asyncio.gather() to propagate, so one bad document can't cancel the
    others' concurrent processing."""
    try:
        parsed = _parse_s3_event(record)
        if parsed is None:
            return None
        bucket, key = parsed
        await _index_one(bucket, key)
        return None
    except Exception:
        logger.error("Failed to process SQS message %s", record.get("messageId"), exc_info=True)
        return record["messageId"]


async def _process_batch(records: list[dict]) -> list[dict]:
    """Every record in one SQS batch processes concurrently via
    asyncio.gather() - still exactly one asyncio.run() for the whole batch
    (not per record), so a long-lived async resource created on this loop
    doesn't outlive it. Chroma's PersistentClient isn't safe for concurrent
    writes from multiple threads - vector_indexer's own collection lock
    serializes the actual chromadb calls underneath this concurrency, so the
    slow parts (download, parse, chunk, embed - real network/CPU time)
    genuinely run in parallel while the one truly unsafe resource stays
    serialized."""
    message_ids = await asyncio.gather(*(_process_one_record(record) for record in records))
    return [{"itemIdentifier": message_id} for message_id in message_ids if message_id is not None]


def lambda_handler(event: dict, context) -> dict:
    """One invocation may carry a batch of SQS messages. Reports per-message
    failures via batchItemFailures (AWS's documented partial-batch-failure
    pattern) so one bad document retries/DLQs without blocking the rest."""
    failures = asyncio.run(_process_batch(event.get("Records", [])))
    return {"batchItemFailures": failures}
