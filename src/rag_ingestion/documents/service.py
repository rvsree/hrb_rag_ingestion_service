"""Saves uploaded files to disk and records their metadata - ported from
hrb_chatbot_service's services/documents_service.py. Rejections are data, not raised.

One deliberate gap, flagged not silently dropped: the original best-effort-clears
hrb_chatbot_service's own answer cache on every document change (Phase 78) - that
cache belongs to the retrieval side, which doesn't exist as a service yet (and when
it does, invalidating its cache from here would be a cross-service HTTP call, not
a local one). _clear_answer_cache_best_effort() below is a no-op until that's wired."""

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret
from rag_ingestion import error_codes
from rag_ingestion.content_hash import compute_content_hash
from rag_ingestion.documents.store import get_metadata_store
from rag_ingestion.gateway import COLLECTION_NAME, get_vector_store
from rag_ingestion.models import (
    ALLOWED_CONTENT_TYPE,
    MAX_FILE_SIZE_BYTES,
    ChunkInfoInput,
    ChunkInfoResult,
    DocumentMetadataInput,
    DocumentMetadataResult,
    DocumentUploadResult,
    PresignedUploadResponse,
    VersioningInfo,
)
from rag_ingestion.pipeline import pipeline
from rag_ingestion.pipeline.indexing import storage_chunk_ids
from rag_ingestion.s3_upload import delete_uploaded_object, download_for_reprocessing, generate_presigned_upload_url

logger = get_logger("documents.service")

UPLOAD_DIRECTORY = Path("data/uploads")


async def _clear_answer_cache_best_effort() -> None:
    """No-op for now - see this module's own docstring."""
    return


def _delete_s3_object_best_effort(document_id: str, filename: str) -> None:
    try:
        delete_uploaded_object(document_id, filename)
    except Exception as error:
        logger.warning("S3 object delete failed for %s: %s: %s", document_id, type(error).__name__, error)


def validate_file(upload: UploadFile, size: int) -> tuple[str, str] | None:
    """Return (message, error_code) for why this file is rejected, or None."""
    filename = upload.filename or ""

    if upload.content_type != ALLOWED_CONTENT_TYPE and not filename.lower().endswith(".pdf"):
        return f"Only PDF files are accepted, got content_type={upload.content_type!r}", error_codes.INVALID_FILE_TYPE

    if size == 0:
        return "File is empty", error_codes.EMPTY_FILE

    if size > MAX_FILE_SIZE_BYTES:
        return f"File is {size} bytes, which exceeds the {MAX_FILE_SIZE_BYTES}-byte limit", error_codes.FILE_TOO_LARGE

    return None


async def save_upload(
    upload: UploadFile,
    supersedes_document_id: str | None = None,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    document_metadata_override: DocumentMetadataInput | None = None,
    uploaded_by: str | None = None,
) -> DocumentUploadResult:
    """Validate, store, and record one uploaded file - never raises."""
    metadata_store = get_metadata_store()

    try:
        content = await upload.read()
    except Exception as error:
        logger.error("Could not read upload %r: %s", upload.filename, error)
        return DocumentUploadResult(
            filename=upload.filename or "unknown", document_id=None, status="rejected",
            error=f"Could not read the uploaded file: {error}", error_code=error_codes.UPLOAD_READ_FAILED,
        )

    size = len(content)
    validation_failure = validate_file(upload, size)
    if validation_failure:
        message, code = validation_failure
        logger.warning("Rejected upload %r: %s", upload.filename, message)
        return DocumentUploadResult(filename=upload.filename or "unknown", document_id=None, status="rejected", error=message, error_code=code)

    content_hash = compute_content_hash(content)
    existing = await metadata_store.find_by_content_hash(content_hash)
    if existing is not None:
        logger.info(
            "Upload %r matches existing document %s (%s) by content - no new document created",
            upload.filename, existing["id"], existing["filename"],
        )
        return DocumentUploadResult(
            filename=upload.filename, document_id=existing["id"], status="duplicate", error=None,
            message=(
                f"Identical content already uploaded as document {existing['id']} "
                f"({existing['filename']!r}), version {existing['document_version']}. "
                "No new document was created - use that document_id to re-index if needed."
            ),
            file_size_bytes=existing["file_size_bytes"],
            versioning_info=VersioningInfo(
                document_version=existing["document_version"],
                is_current=existing.get("is_current"),
                supersedes=existing.get("supersedes"),
                superseded_by=existing.get("superseded_by"),
            ),
        )

    if supersedes_document_id:
        target = await metadata_store.get_document(supersedes_document_id)
        if target is None:
            logger.warning("Upload %r cannot supersede unknown document %r", upload.filename, supersedes_document_id)
            return DocumentUploadResult(
                filename=upload.filename or "unknown", document_id=None, status="rejected",
                error=f"supersedes_document_id {supersedes_document_id!r} does not exist",
                error_code=error_codes.SUPERSEDES_TARGET_NOT_FOUND,
            )

    document_id = uuid.uuid4().hex
    document_directory = UPLOAD_DIRECTORY / document_id

    try:
        document_directory.mkdir(parents=True, exist_ok=True)
        file_path = document_directory / upload.filename
        file_path.write_bytes(content)

        await metadata_store.create_document(
            document_id, upload.filename, str(file_path), size, content_hash,
            supersedes=supersedes_document_id, uploaded_by=uploaded_by,
        )
    except Exception as error:
        logger.error("Failed to store upload %r (document %s): %s", upload.filename, document_id, error)
        return DocumentUploadResult(
            filename=upload.filename, document_id=None, status="rejected",
            error=f"Could not store the file: {error}", error_code=error_codes.STORAGE_FAILURE,
        )

    logger.info("Stored upload %r as document %s", upload.filename, document_id)

    index_outcome = await _index_now(
        document_id, file_path, chunking_strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        document_metadata_override=document_metadata_override,
    )
    document_metadata = index_outcome.get("document_metadata") or {}

    return DocumentUploadResult(
        filename=upload.filename, document_id=document_id, status="uploaded",
        error=index_outcome.get("error"), error_code=index_outcome.get("error_code"),
        file_size_bytes=size, uploaded_by=uploaded_by,
        chunk_info=ChunkInfoResult(
            chunking_strategy=index_outcome.get("chunking_strategy"),
            chunk_size=index_outcome.get("chunk_size"),
            chunk_overlap=index_outcome.get("chunk_overlap"),
            action=index_outcome.get("action"),
            chunks_indexed=index_outcome.get("chunks_indexed"),
            chunks_removed=index_outcome.get("chunks_removed"),
        ),
        document_metadata=DocumentMetadataResult(**document_metadata) if document_metadata else None,
        versioning_info=VersioningInfo(
            document_version=index_outcome.get("document_version", 0), is_current=True, supersedes=supersedes_document_id
        ),
    )


async def _index_now(
    document_id: str,
    file_path,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    document_metadata_override: DocumentMetadataInput | None = None,
) -> dict:
    """Chunk/embed/index a just-uploaded file - never raises, a failure leaves status 'failed'."""
    metadata_store = get_metadata_store()
    override_dict = (
        document_metadata_override.model_dump(exclude={"supersedes_document_id"}) if document_metadata_override else None
    )
    try:
        result = await pipeline.index_document(
            metadata_store, document_id, str(file_path),
            chunking_strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            document_metadata_override=override_dict,
        )
        await _clear_answer_cache_best_effort()
        return result
    except Exception as error:
        logger.error("Indexing failed for document %s: %s: %s", document_id, type(error).__name__, error)
        existing = await metadata_store.get_document(document_id)
        failed_stage = existing["status"] if existing else "unknown"
        await metadata_store.update_status(document_id, "failed", f"[{failed_stage}] {error}")
        return {"error": "Upload succeeded, but indexing failed.", "error_code": error_codes.INDEXING_FAILED}


async def reindex_document(
    document_id: str, upload: UploadFile, chunking_strategy: str | None = None,
    chunk_size: int | None = None, chunk_overlap: int | None = None,
) -> DocumentUploadResult | None:
    """Replace an existing document's file content in place, under the same document_id, and re-index it.
    Returns None if document_id doesn't exist - the route turns that into a 404."""
    metadata_store = get_metadata_store()
    existing = await metadata_store.get_document(document_id)
    if existing is None:
        return None

    try:
        content = await upload.read()
    except Exception as error:
        logger.error("Could not read reindex upload %r for document %s: %s", upload.filename, document_id, error)
        return DocumentUploadResult(
            filename=upload.filename or "unknown", document_id=document_id, status="rejected",
            error=f"Could not read the uploaded file: {error}", error_code=error_codes.UPLOAD_READ_FAILED,
        )

    size = len(content)
    validation_failure = validate_file(upload, size)
    if validation_failure:
        message, code = validation_failure
        logger.warning("Rejected reindex upload %r for document %s: %s", upload.filename, document_id, message)
        return DocumentUploadResult(filename=upload.filename or "unknown", document_id=document_id, status="rejected", error=message, error_code=code)

    content_hash = compute_content_hash(content)
    if content_hash == existing.get("content_hash"):
        logger.info("Reindex upload for document %s matches existing content - no-op", document_id)
        return DocumentUploadResult(
            filename=existing["filename"], document_id=document_id, status="unchanged", error=None,
            message="Uploaded content is identical to the current version - nothing to re-index.",
            file_size_bytes=existing["file_size_bytes"],
            versioning_info=VersioningInfo(document_version=existing["document_version"], is_current=True),
        )

    document_directory = UPLOAD_DIRECTORY / document_id
    try:
        shutil.rmtree(document_directory, ignore_errors=True)
        document_directory.mkdir(parents=True, exist_ok=True)
        file_path = document_directory / upload.filename
        file_path.write_bytes(content)
    except Exception as error:
        logger.error("Failed to store reindex upload %r for document %s: %s", upload.filename, document_id, error)
        return DocumentUploadResult(
            filename=upload.filename, document_id=document_id, status="rejected",
            error=f"Could not store the file: {error}", error_code=error_codes.STORAGE_FAILURE,
        )

    logger.info("Stored reindex upload %r for document %s - re-indexing", upload.filename, document_id)

    index_outcome = await _index_now(document_id, file_path, chunking_strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    await metadata_store.update_document_content(document_id, upload.filename, str(file_path), size, content_hash)

    return DocumentUploadResult(
        filename=upload.filename, document_id=document_id, status="reindexed",
        error=index_outcome.get("error"), error_code=index_outcome.get("error_code"), file_size_bytes=size,
        chunk_info=ChunkInfoResult(
            chunking_strategy=index_outcome.get("chunking_strategy"), chunk_size=index_outcome.get("chunk_size"),
            chunk_overlap=index_outcome.get("chunk_overlap"), action=index_outcome.get("action"),
            chunks_indexed=index_outcome.get("chunks_indexed"), chunks_removed=index_outcome.get("chunks_removed"),
        ),
        versioning_info=VersioningInfo(
            document_version=index_outcome.get("document_version", existing["document_version"]), is_current=True,
            supersedes=existing.get("supersedes"), superseded_by=existing.get("superseded_by"),
        ),
    )


async def rechunk_document(
    document_id: str, chunking_strategy: str | None = None, chunk_size: int | None = None, chunk_overlap: int | None = None
) -> DocumentUploadResult | None:
    """Re-chunk/embed/index an existing document's already-stored file with new settings - no file
    upload, no content-hash check. Returns None if document_id doesn't exist."""
    metadata_store = get_metadata_store()
    existing = await metadata_store.get_document(document_id)
    if existing is None:
        return None

    file_path = download_for_reprocessing(existing["file_path"], document_id, existing["filename"])
    logger.info("Re-chunking document %s with new settings (file unchanged)", document_id)

    index_outcome = await _index_now(document_id, file_path, chunking_strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    return DocumentUploadResult(
        filename=existing["filename"], document_id=document_id, status="reindexed",
        error=index_outcome.get("error"), error_code=index_outcome.get("error_code"), file_size_bytes=existing["file_size_bytes"],
        chunk_info=ChunkInfoResult(
            chunking_strategy=index_outcome.get("chunking_strategy"), chunk_size=index_outcome.get("chunk_size"),
            chunk_overlap=index_outcome.get("chunk_overlap"), action=index_outcome.get("action"),
            chunks_indexed=index_outcome.get("chunks_indexed"), chunks_removed=index_outcome.get("chunks_removed"),
        ),
        versioning_info=VersioningInfo(
            document_version=index_outcome.get("document_version", existing["document_version"]), is_current=True,
            supersedes=existing.get("supersedes"), superseded_by=existing.get("superseded_by"),
        ),
    )


async def request_presigned_upload(
    filename: str, content_type: str, supersedes_document_id: str | None = None,
    chunk_info: ChunkInfoInput | None = None, document_metadata: DocumentMetadataInput | None = None,
    uploaded_by: str | None = None,
) -> PresignedUploadResponse | None:
    """Records a pending document row and hands back a presigned S3 PUT url. Returns None if
    supersedes_document_id doesn't exist."""
    metadata_store = get_metadata_store()
    if supersedes_document_id:
        target = await metadata_store.get_document(supersedes_document_id)
        if target is None:
            return None

    document_id = uuid.uuid4().hex
    bucket = read_secret("S3_UPLOAD_BUCKET", "")
    placeholder_path = f"s3://{bucket}/{document_id}/{filename}"

    await metadata_store.create_document(document_id, filename, placeholder_path, 0, None, supersedes=supersedes_document_id, uploaded_by=uploaded_by)
    await metadata_store.update_status(document_id, "pending_upload")

    overrides = {
        "chunk_info": chunk_info.model_dump() if chunk_info else None,
        "document_metadata": document_metadata.model_dump(exclude={"supersedes_document_id"}) if document_metadata else None,
    }
    if chunk_info or document_metadata:
        await metadata_store.set_pending_overrides(document_id, json.dumps(overrides))

    presigned = generate_presigned_upload_url(document_id, filename, content_type)
    logger.info("Issued a presigned upload url for document %s (%r)", document_id, filename)

    return PresignedUploadResponse(document_id=document_id, upload_url=presigned["upload_url"], expires_in_seconds=presigned["expires_in_seconds"], status="pending_upload")


async def save_uploads(
    uploads: list[UploadFile], supersedes_document_id: str | None = None, chunking_strategy: str | None = None,
    chunk_size: int | None = None, chunk_overlap: int | None = None,
    document_metadata_override: DocumentMetadataInput | None = None, uploaded_by: str | None = None,
) -> list[DocumentUploadResult]:
    """Validate, store, and record every uploaded file - one result per file."""
    results = []
    for upload in uploads:
        result = await save_upload(
            upload, supersedes_document_id=supersedes_document_id, chunking_strategy=chunking_strategy,
            chunk_size=chunk_size, chunk_overlap=chunk_overlap, document_metadata_override=document_metadata_override,
            uploaded_by=uploaded_by,
        )
        results.append(result)
    return results


def _parse_chunk_ids(document: dict) -> dict:
    raw = document.get("chunk_ids")
    document["chunk_ids"] = json.loads(raw) if raw else None
    return document


async def list_documents() -> list[dict]:
    documents = await get_metadata_store().list_documents()
    return [_parse_chunk_ids(document) for document in documents]


async def get_document(document_id: str) -> dict | None:
    document = await get_metadata_store().get_document(document_id)
    if document is None:
        return None
    return _parse_chunk_ids(document)


async def delete_document(document_id: str, deleted_by: str | None = None) -> dict | None:
    """Delete one document: vectors, metadata row, uploaded file - None if unknown."""
    metadata_store = get_metadata_store()
    document = await metadata_store.get_document(document_id)
    if document is None:
        return None

    chunk_ids = json.loads(document["chunk_ids"]) if document.get("chunk_ids") else []

    if chunk_ids:
        vector_store = get_vector_store(provider=document.get("vector_db"))
        await asyncio.to_thread(
            vector_store.delete,
            collection_name=COLLECTION_NAME,
            ids=storage_chunk_ids(vector_store.PROVIDER_NAME, document_id, chunk_ids),
        )
        logger.info("Deleted %d vector(s) for document %s", len(chunk_ids), document_id)

    await metadata_store.delete_document(document_id)

    document_directory = UPLOAD_DIRECTORY / document_id
    shutil.rmtree(document_directory, ignore_errors=True)

    await asyncio.to_thread(_delete_s3_object_best_effort, document_id, document["filename"])
    await _clear_answer_cache_best_effort()

    logger.info("Deleted document %s (%r) - %d chunk(s) removed", document_id, document["filename"], len(chunk_ids))
    return {"document_id": document_id, "filename": document["filename"], "chunks_removed": len(chunk_ids), "deleted_by": deleted_by}


async def delete_all_documents(deleted_by: str | None = None) -> dict:
    documents = await list_documents()
    documents_deleted = 0
    chunks_removed = 0
    for document in documents:
        result = await delete_document(document["id"], deleted_by=deleted_by)
        if result is not None:
            documents_deleted += 1
            chunks_removed += result["chunks_removed"]

    logger.info("Deleted all %d document(s) - %d chunk(s) removed in total", documents_deleted, chunks_removed)
    return {"documents_deleted": documents_deleted, "chunks_removed": chunks_removed, "deleted_by": deleted_by}


async def _find_test_noise_documents(max_file_size_bytes: int) -> list[dict]:
    documents = await list_documents()
    return [document for document in documents if (document.get("file_size_bytes") or 0) < max_file_size_bytes]


async def list_test_noise_documents(max_file_size_bytes: int) -> list[dict]:
    return await _find_test_noise_documents(max_file_size_bytes)


async def delete_test_noise_documents(max_file_size_bytes: int, deleted_by: str | None = None) -> dict:
    documents = await _find_test_noise_documents(max_file_size_bytes)
    documents_deleted = 0
    chunks_removed = 0
    for document in documents:
        result = await delete_document(document["id"], deleted_by=deleted_by)
        if result is not None:
            documents_deleted += 1
            chunks_removed += result["chunks_removed"]

    logger.info(
        "Deleted %d test-noise document(s) (< %d bytes) - %d chunk(s) removed in total",
        documents_deleted, max_file_size_bytes, chunks_removed,
    )
    return {"documents_deleted": documents_deleted, "chunks_removed": chunks_removed, "deleted_by": deleted_by}
