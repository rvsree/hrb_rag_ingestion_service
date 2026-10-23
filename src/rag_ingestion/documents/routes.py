"""Document routes - ported from hrb_chatbot_service's api/rag/ingest_document.py,
same paths/methods/response shapes/status codes. No RBAC here (see ROADMAP.md
Phase 1's own "Out of scope" note) - require_role() calls dropped, user_profile
kept on request bodies for wire-shape parity only. No rate limiting either - the
Gateway that would front this service in production doesn't exist yet."""

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from rag_ingestion import error_codes
from rag_ingestion.documents import service as documents_service
from rag_ingestion.models import (
    ALLOWED_CONTENT_TYPE,
    PAYLOAD_MAX_LENGTH,
    TEST_NOISE_MAX_FILE_SIZE_BYTES,
    DocumentDeleteAllResponse,
    DocumentDeleteResponse,
    DocumentListResponse,
    DocumentRecord,
    DocumentUploadResponse,
    DocumentUploadResult,
    PresignedUploadRequest,
    PresignedUploadResponse,
    ReindexDocumentPayload,
    TestNoiseDocument,
    TestNoisePreviewResponse,
    UploadDocumentsPayload,
)

router = APIRouter(tags=["documents"])


def _json_error(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message, "code": code})


def _parse_payload(raw_payload: str | None) -> UploadDocumentsPayload | None:
    if not raw_payload:
        return UploadDocumentsPayload()
    try:
        return UploadDocumentsPayload.model_validate_json(raw_payload)
    except ValidationError:
        return None


@router.post("/documents", response_model=DocumentUploadResponse)
async def upload_documents(files: list[UploadFile] = File(...), payload: str | None = Form(None, max_length=PAYLOAD_MAX_LENGTH)):
    # Upload, chunk, embed, and index one or more PDFs in one call - a single file is just a list of one.
    parsed_payload = _parse_payload(payload)
    if parsed_payload is None:
        return _json_error(422, "payload is not valid JSON, or doesn't match the expected shape.", error_codes.VALIDATION_ERROR)

    chunk_info = parsed_payload.chunk_info
    document_metadata = parsed_payload.document_metadata
    supersedes_document_id = document_metadata.supersedes_document_id if document_metadata else None
    uploaded_by = parsed_payload.user_profile.employee_id if parsed_payload.user_profile else None

    if supersedes_document_id and len(files) != 1:
        return _json_error(422, "supersedes_document_id is only valid with exactly one file - ambiguous for a batch upload.", error_codes.VALIDATION_ERROR)

    results = await documents_service.save_uploads(
        files, supersedes_document_id=supersedes_document_id,
        chunking_strategy=chunk_info.chunking_strategy if chunk_info else None,
        chunk_size=chunk_info.chunk_size if chunk_info else None,
        chunk_overlap=chunk_info.chunk_overlap if chunk_info else None,
        document_metadata_override=document_metadata, uploaded_by=uploaded_by,
    )

    uploaded_count = sum(1 for r in results if r.status == "uploaded")
    duplicate_count = sum(1 for r in results if r.status == "duplicate")
    rejected_count = len(results) - uploaded_count - duplicate_count

    return DocumentUploadResponse(uploaded_count=uploaded_count, duplicate_count=duplicate_count, rejected_count=rejected_count, results=results)


def _parse_reindex_payload(raw_payload: str | None) -> ReindexDocumentPayload | None:
    if not raw_payload:
        return ReindexDocumentPayload()
    try:
        return ReindexDocumentPayload.model_validate_json(raw_payload)
    except ValidationError:
        return None


@router.post("/documents/{document_id}/reindex", response_model=DocumentUploadResult)
async def reindex_document(document_id: str, file: UploadFile = File(...), payload: str | None = Form(None, max_length=PAYLOAD_MAX_LENGTH)):
    parsed_payload = _parse_reindex_payload(payload)
    if parsed_payload is None:
        return _json_error(422, "payload is not valid JSON, or doesn't match the expected shape.", error_codes.VALIDATION_ERROR)

    chunk_info = parsed_payload.chunk_info
    result = await documents_service.reindex_document(
        document_id, file,
        chunking_strategy=chunk_info.chunking_strategy if chunk_info else None,
        chunk_size=chunk_info.chunk_size if chunk_info else None,
        chunk_overlap=chunk_info.chunk_overlap if chunk_info else None,
    )

    if result is None:
        return _json_error(404, f"No document found with id {document_id!r}", error_codes.DOCUMENT_NOT_FOUND)
    return result


@router.post("/documents/{document_id}/rechunk", response_model=DocumentUploadResult)
async def rechunk_document(document_id: str, payload: ReindexDocumentPayload):
    chunk_info = payload.chunk_info
    result = await documents_service.rechunk_document(
        document_id,
        chunking_strategy=chunk_info.chunking_strategy if chunk_info else None,
        chunk_size=chunk_info.chunk_size if chunk_info else None,
        chunk_overlap=chunk_info.chunk_overlap if chunk_info else None,
    )

    if result is None:
        return _json_error(404, f"No document found with id {document_id!r}", error_codes.DOCUMENT_NOT_FOUND)
    return result


@router.post("/documents/presigned-upload", response_model=PresignedUploadResponse)
async def request_presigned_upload(body: PresignedUploadRequest):
    if body.content_type != ALLOWED_CONTENT_TYPE and not body.filename.lower().endswith(".pdf"):
        return _json_error(422, f"Only PDF files are accepted, got content_type={body.content_type!r}", error_codes.INVALID_FILE_TYPE)

    supersedes_document_id = body.document_metadata.supersedes_document_id if body.document_metadata else None
    uploaded_by = body.user_profile.employee_id if body.user_profile else None

    result = await documents_service.request_presigned_upload(
        body.filename, body.content_type, supersedes_document_id=supersedes_document_id,
        chunk_info=body.chunk_info, document_metadata=body.document_metadata, uploaded_by=uploaded_by,
    )

    if result is None:
        return _json_error(422, f"supersedes_document_id {supersedes_document_id!r} does not exist", error_codes.SUPERSEDES_TARGET_NOT_FOUND)
    return result


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents():
    """List every uploaded document and its current status."""
    documents = await documents_service.list_documents()
    return DocumentListResponse(count=len(documents), documents=[DocumentRecord.from_row(doc) for doc in documents])


@router.get("/documents/cleanup/preview", response_model=TestNoisePreviewResponse)
async def preview_test_noise_documents():
    """Preview only - what DELETE .../documents/cleanup would remove, without removing anything."""
    documents = await documents_service.list_test_noise_documents(TEST_NOISE_MAX_FILE_SIZE_BYTES)
    return TestNoisePreviewResponse(
        count=len(documents), threshold_bytes=TEST_NOISE_MAX_FILE_SIZE_BYTES,
        documents=[TestNoiseDocument(**document) for document in documents],
    )


@router.delete("/documents/cleanup", response_model=DocumentDeleteAllResponse)
async def delete_test_noise_documents():
    """Delete every document below TEST_NOISE_MAX_FILE_SIZE_BYTES - same full-delete semantics as DELETE /documents."""
    result = await documents_service.delete_test_noise_documents(TEST_NOISE_MAX_FILE_SIZE_BYTES)
    return DocumentDeleteAllResponse(**result)


@router.get("/documents/{document_id}", response_model=DocumentRecord)
async def get_document(document_id: str):
    """Get one document's metadata by id."""
    document = await documents_service.get_document(document_id)
    if document is None:
        return _json_error(404, f"Unknown document '{document_id}'", error_codes.DOCUMENT_NOT_FOUND)
    return DocumentRecord.from_row(document)


@router.delete("/documents/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(document_id: str):
    """Delete one document completely: vectors, metadata rows, uploaded file."""
    result = await documents_service.delete_document(document_id)
    if result is None:
        return _json_error(404, f"Unknown document '{document_id}'", error_codes.DOCUMENT_NOT_FOUND)
    return DocumentDeleteResponse(**result)


@router.delete("/documents", response_model=DocumentDeleteAllResponse)
async def delete_all_documents():
    """Delete every document completely - same full delete as DELETE /documents/{id}, just for all of them."""
    result = await documents_service.delete_all_documents()
    return DocumentDeleteAllResponse(**result)
