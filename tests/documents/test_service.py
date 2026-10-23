"""Representative subset of hrb_chatbot_service's own documents_service behavior,
covered there via test_routes_documents.py (no standalone test_documents_service.py
exists in that repo either). Pipeline indexing itself is faked here - already
covered by tests/pipeline/test_pipeline.py - these tests focus on documents/
service.py's own orchestration (dedup, supersedes, delete)."""

from rag_ingestion.documents import service as documents_service
from rag_ingestion.pipeline import pipeline
from tests.conftest import FakeMetadataStore, FakeUploadFile, FakeVectorStore


def _patch(monkeypatch, metadata_store=None, vector_store=None, index_result=None):
    metadata_store = metadata_store or FakeMetadataStore()
    vector_store = vector_store or FakeVectorStore()

    monkeypatch.setattr(documents_service, "get_metadata_store", lambda: metadata_store)
    monkeypatch.setattr(documents_service, "get_vector_store", lambda provider=None: vector_store)

    async def _fake_index_document(metadata_store_arg, document_id, file_path, **kwargs):
        # Mirrors the one real side effect downstream code relies on - chunk_ids
        # actually recorded on the document row, same as the real pipeline's
        # own write_chunks()->record_successful_index() call.
        chunk_ids = [f"{document_id}:0"]
        document_version = await metadata_store_arg.record_successful_index(
            document_id, chunk_ids, embedding_model="fake-model", embedding_dimension=4,
            vector_db="chromadb", chunking_strategy="recursive", chunk_size=1000, chunk_overlap=150,
        )
        return index_result or {
            "action": "insert", "chunks_indexed": 1, "chunks_removed": 0, "chunk_ids": chunk_ids,
            "document_version": document_version, "chunking_strategy": "recursive", "chunk_size": 1000,
            "chunk_overlap": 150, "document_metadata": {},
        }

    monkeypatch.setattr(pipeline, "index_document", _fake_index_document)
    return metadata_store, vector_store


async def test_save_upload_rejects_non_pdf_content_type(tmp_path, monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    upload = FakeUploadFile("notes.txt", b"hello", content_type="text/plain")
    result = await documents_service.save_upload(upload)

    assert result.status == "rejected"
    assert result.error_code == "INVALID_FILE_TYPE"


async def test_save_upload_rejects_empty_file(tmp_path, monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    upload = FakeUploadFile("empty.pdf", b"")
    result = await documents_service.save_upload(upload)

    assert result.status == "rejected"
    assert result.error_code == "EMPTY_FILE"


async def test_save_upload_stores_and_indexes_a_new_document(tmp_path, monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    upload = FakeUploadFile("benefits.pdf", b"%PDF-1.4 fake content")
    result = await documents_service.save_upload(upload)

    assert result.status == "uploaded"
    assert result.document_id is not None
    assert result.chunk_info.chunks_indexed == 1


async def test_save_upload_matches_duplicate_content_by_hash(tmp_path, monkeypatch):
    metadata_store, _ = _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    content = b"%PDF-1.4 same bytes every time"
    first = await documents_service.save_upload(FakeUploadFile("a.pdf", content))
    second = await documents_service.save_upload(FakeUploadFile("b.pdf", content))

    assert second.status == "duplicate"
    assert second.document_id == first.document_id


async def test_save_upload_rejects_unknown_supersedes_target(tmp_path, monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    upload = FakeUploadFile("new.pdf", b"%PDF-1.4 new content")
    result = await documents_service.save_upload(upload, supersedes_document_id="does-not-exist")

    assert result.status == "rejected"
    assert result.error_code == "SUPERSEDES_TARGET_NOT_FOUND"


async def test_delete_document_removes_vectors_and_metadata(tmp_path, monkeypatch):
    metadata_store, vector_store = _patch(monkeypatch)
    monkeypatch.setattr(documents_service, "UPLOAD_DIRECTORY", tmp_path)

    upload = FakeUploadFile("to_delete.pdf", b"%PDF-1.4 delete me")
    saved = await documents_service.save_upload(upload)

    result = await documents_service.delete_document(saved.document_id)

    assert result is not None
    assert result["chunks_removed"] == 1
    assert await metadata_store.get_document(saved.document_id) is None


async def test_delete_document_returns_none_for_unknown_id(monkeypatch):
    _patch(monkeypatch)

    result = await documents_service.delete_document("unknown-id")

    assert result is None
