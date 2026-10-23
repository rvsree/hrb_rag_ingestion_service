"""Tests for the Lambda handler - a thin adapter over pipeline.index_document(),
so these cover the adapter logic only (parsing the SQS/S3 event, creating a
missing metadata row, status transitions, batchItemFailures reporting) - no
real AWS call, matching this repo's existing fake-based test convention.
Ported from hrb_chatbot_service's tests/hrb_chatbot/lambda_handlers/test_index_document_handler.py."""

import asyncio
import json
import logging
import sys

from rag_ingestion import lambda_handler
from tests.conftest import FakeMetadataStore


class _FakeS3Client:
    """Stands in for boto3's S3 client - download_file() writes fixed bytes
    to the given local path instead of making a real network call."""

    def __init__(self, content: bytes = b"%PDF-1.4 fake content"):
        self.content = content
        self.downloaded = []

    def download_file(self, bucket, key, local_path):
        self.downloaded.append((bucket, key, local_path))
        with open(local_path, "wb") as f:
            f.write(self.content)


def _s3_event_sqs_record(bucket: str, key: str, message_id: str = "msg-1") -> dict:
    """One SQS message carrying an S3 ObjectCreated event notification body -
    the real shape S3 -> SQS delivers."""
    body = {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}
    return {"messageId": message_id, "body": json.dumps(body)}


def _wire_fakes(monkeypatch, metadata_store=None, s3_client=None, index_document=None):
    store = metadata_store or FakeMetadataStore()
    monkeypatch.setattr(lambda_handler, "get_metadata_store", lambda: store)
    monkeypatch.setattr(lambda_handler.boto3, "client", lambda service: s3_client or _FakeS3Client())

    async def _default_index_document(metadata_store, document_id, file_path, **kwargs):
        return {"action": "insert", "chunks_indexed": 1, "chunks_removed": 0}

    monkeypatch.setattr(lambda_handler.pipeline, "index_document", index_document or _default_index_document)
    return store


def test_indexes_a_new_document_and_creates_its_metadata_row(monkeypatch):
    store = _wire_fakes(monkeypatch)
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-123/policy.pdf")]}

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": []}
    document = store.documents["doc-123"]
    assert document["filename"] == "policy.pdf"


def test_pending_overrides_from_the_presigned_upload_route_are_passed_through_and_cleared(monkeypatch):
    # The presigned-upload route already created this row with
    # chunk_info/document_metadata stashed - the handler must pass them to
    # index_document() and clear the column afterward, not just ignore them.
    metadata_store = FakeMetadataStore()
    metadata_store.documents["doc-123"] = {
        "id": "doc-123",
        "filename": "policy.pdf",
        "status": "pending_upload",
        "pending_overrides": json.dumps({
            "chunk_info": {"chunking_strategy": "recursive", "chunk_size": 800, "chunk_overlap": 100},
            "document_metadata": {"doc_category": "benefits"},
        }),
    }
    captured_kwargs = {}

    async def _capturing_index_document(metadata_store, document_id, file_path, **kwargs):
        captured_kwargs.update(kwargs)
        return {"action": "insert", "chunks_indexed": 1, "chunks_removed": 0}

    store = _wire_fakes(monkeypatch, metadata_store=metadata_store, index_document=_capturing_index_document)
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-123/policy.pdf")]}

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": []}
    assert captured_kwargs["chunking_strategy"] == "recursive"
    assert captured_kwargs["chunk_size"] == 800
    assert captured_kwargs["chunk_overlap"] == 100
    assert captured_kwargs["document_metadata_override"] == {"doc_category": "benefits"}
    assert store.documents["doc-123"]["pending_overrides"] is None


def test_existing_document_is_not_recreated_idempotency(monkeypatch):
    metadata_store = FakeMetadataStore()
    metadata_store.documents["doc-123"] = {"id": "doc-123", "filename": "policy.pdf", "status": "uploaded"}
    store = _wire_fakes(monkeypatch, metadata_store=metadata_store)
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-123/policy.pdf")]}

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": []}
    # The handler itself sets "downloading" for an existing row, before the
    # S3 download - the rest of the progression (chunking/embedding/indexed)
    # lives inside the real index_document(), faked out here, so it never
    # fires past this point.
    assert store.documents["doc-123"]["status"] == "downloading"


def test_indexing_failure_marks_the_document_failed_and_reports_batch_item_failure(monkeypatch):
    async def _failing_index_document(metadata_store, document_id, file_path, **kwargs):
        raise ValueError("could not reach pinecone")

    store = _wire_fakes(monkeypatch, index_document=_failing_index_document)
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-456/benefits.pdf", message_id="msg-2")]}

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-2"}]}
    document = store.documents["doc-456"]
    assert document["status"] == "failed"
    assert "could not reach pinecone" in document["error_message"]


def test_indexing_failure_prefixes_the_stage_it_actually_failed_at(monkeypatch):
    # The stage comes from the document's own real, live status right
    # before the overwrite, not a guess or a stale pre-set value:
    # _index_one() itself sets "downloading" for an existing row before
    # calling index_document() - faking index_document() to fail immediately
    # means "downloading" is the real last-reached stage here.
    metadata_store = FakeMetadataStore()
    metadata_store.documents["doc-789"] = {"id": "doc-789", "filename": "policy.pdf", "status": "uploaded"}

    async def _failing_index_document(metadata_store, document_id, file_path, **kwargs):
        raise ValueError("could not reach pinecone")

    store = _wire_fakes(monkeypatch, metadata_store=metadata_store, index_document=_failing_index_document)
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-789/policy.pdf", message_id="msg-3")]}

    lambda_handler.lambda_handler(event, context=None)

    document = store.documents["doc-789"]
    assert document["error_message"] == "[downloading] could not reach pinecone"


def test_s3_test_event_is_skipped_not_treated_as_a_failure(monkeypatch):
    _wire_fakes(monkeypatch)
    test_event_body = {"Event": "s3:TestEvent"}
    record = {"messageId": "msg-3", "body": json.dumps(test_event_body)}

    result = lambda_handler.lambda_handler({"Records": [record]}, context=None)

    assert result == {"batchItemFailures": []}


def test_a_batch_of_several_records_runs_on_one_shared_event_loop(monkeypatch):
    # Regression for a real bug class: calling asyncio.run() once PER record
    # closes the loop a long-lived async resource was created on, raising
    # "Event loop is closed" starting on the second record. This fake raises
    # that same way if it's ever called from a different running loop than
    # the one it first saw.
    seen_loop = {}

    async def _index_document(metadata_store, document_id, file_path, **kwargs):
        current_loop = asyncio.get_running_loop()
        if "loop" not in seen_loop:
            seen_loop["loop"] = current_loop
        elif seen_loop["loop"] is not current_loop:
            raise RuntimeError("Event loop is closed")
        return {"action": "insert", "chunks_indexed": 1, "chunks_removed": 0}

    _wire_fakes(monkeypatch, index_document=_index_document)
    event = {
        "Records": [
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-1/a.pdf", message_id="msg-1"),
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-2/b.pdf", message_id="msg-2"),
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-3/c.pdf", message_id="msg-3"),
        ]
    }

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": []}


def test_a_batch_of_several_records_actually_processes_concurrently(monkeypatch):
    """The real symptom this guards against: documents in one batch used to
    process strictly one at a time. Proves real concurrency: each fake
    _index_one sleeps briefly and records when it started: if they're
    concurrent, all 3 start within a narrow window; if still sequential,
    each one's start time would trail the previous one's full sleep duration."""
    start_times = []

    async def _index_document(metadata_store, document_id, file_path, **kwargs):
        start_times.append(asyncio.get_event_loop().time())
        await asyncio.sleep(0.2)
        return {"action": "insert", "chunks_indexed": 1, "chunks_removed": 0}

    _wire_fakes(monkeypatch, index_document=_index_document)
    event = {
        "Records": [
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-1/a.pdf", message_id="msg-1"),
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-2/b.pdf", message_id="msg-2"),
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-3/c.pdf", message_id="msg-3"),
        ]
    }

    lambda_handler.lambda_handler(event, context=None)

    assert len(start_times) == 3
    # Sequential would spread these ~0.2s apart each (~0.4s total spread);
    # concurrent keeps them within a tight window since all 3 start before
    # any of them finishes sleeping.
    assert max(start_times) - min(start_times) < 0.1


# --- the JSON log formatter must not silently drop real tracebacks ---


def test_json_formatter_includes_the_traceback_when_exc_info_is_set():
    formatter = lambda_handler._JsonFormatter()
    try:
        raise ValueError("could not reach pinecone")
    except ValueError:
        record = logging.LogRecord(
            name="rag_ingestion.lambda_handler", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="Failed to process SQS message msg-1", args=(), exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))

    assert "could not reach pinecone" in payload["exception"]
    assert "Traceback" in payload["exception"]


def test_json_formatter_omits_exception_key_when_there_is_no_error():
    formatter = lambda_handler._JsonFormatter()
    record = logging.LogRecord(
        name="rag_ingestion.lambda_handler", level=logging.INFO, pathname=__file__, lineno=1,
        msg="Polling for messages", args=(), exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert "exception" not in payload


# --- a real filename with a space, URL-encoded by S3's own event notification ---


def test_a_url_encoded_key_with_spaces_decodes_to_the_real_filename(monkeypatch):
    # S3's ObjectCreated notification encodes a space as "+". "Fake content"
    # is written here under the DECODED local path - a test proving the
    # handler actually calls download_file() with the decoded key, not the
    # still-encoded one.
    s3_client = _FakeS3Client()
    store = _wire_fakes(monkeypatch, s3_client=s3_client)
    encoded_key = "doc-789/Global+Data+Privacy+Statement_FY27.pdf"
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", encoded_key)]}

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": []}
    downloaded_bucket, downloaded_key, _ = s3_client.downloaded[0]
    assert downloaded_key == "doc-789/Global Data Privacy Statement_FY27.pdf"
    document = store.documents["doc-789"]
    assert document["filename"] == "Global Data Privacy Statement_FY27.pdf"


def test_percent_encoded_characters_in_the_key_are_also_decoded(monkeypatch):
    s3_client = _FakeS3Client()
    _wire_fakes(monkeypatch, s3_client=s3_client)
    # "(1)" URL-encoded - parentheses are percent-encoded by S3's own notification.
    encoded_key = "doc-999/FDE+Pulse+%281%29.pdf"
    event = {"Records": [_s3_event_sqs_record("hrb-chatbot-kb-uploads", encoded_key)]}

    lambda_handler.lambda_handler(event, context=None)

    _, downloaded_key, _ = s3_client.downloaded[0]
    assert downloaded_key == "doc-999/FDE Pulse (1).pdf"


def test_one_bad_message_in_a_batch_does_not_block_the_others(monkeypatch):
    async def _index_document(metadata_store, document_id, file_path, **kwargs):
        if document_id == "doc-bad":
            raise ValueError("boom")
        return {"action": "insert", "chunks_indexed": 1, "chunks_removed": 0}

    store = _wire_fakes(monkeypatch, index_document=_index_document)
    event = {
        "Records": [
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-good/a.pdf", message_id="msg-good"),
            _s3_event_sqs_record("hrb-chatbot-kb-uploads", "doc-bad/b.pdf", message_id="msg-bad"),
        ]
    }

    result = lambda_handler.lambda_handler(event, context=None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}
    assert store.documents["doc-good"]["filename"] == "a.pdf"
    assert store.documents["doc-bad"]["status"] == "failed"
