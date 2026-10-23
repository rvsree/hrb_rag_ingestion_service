"""Representative subset of hrb_chatbot_service's own test_vector_indexer.py -
covers the highest-value logic: insert vs. update, stale-chunk cleanup on
re-index, and the supersede-flip."""

from rag_ingestion.gateway import COLLECTION_NAME
from rag_ingestion.pipeline import indexing
from tests.conftest import FakeMetadataStore, FakeVectorStore


def _patch(monkeypatch, vector_store):
    monkeypatch.setattr(indexing, "get_vector_store", lambda provider=None: vector_store)


async def test_first_index_of_a_document_is_reported_as_insert(monkeypatch):
    vector_store = FakeVectorStore()
    metadata_store = FakeMetadataStore()
    _patch(monkeypatch, vector_store)
    await metadata_store.create_document("doc-1", "a.pdf", "/tmp/a.pdf")

    result = await indexing.write_chunks(metadata_store, "doc-1", ["chunk a", "chunk b"], [[0.1, 0.2], [0.3, 0.4]])

    assert result["action"] == "insert"
    assert result["chunks_indexed"] == 2
    assert result["chunks_removed"] == 0
    assert vector_store.chunk_ids_present(COLLECTION_NAME) == {"doc-1:0", "doc-1:1"}


async def test_reindex_with_fewer_chunks_removes_stale_ones(monkeypatch):
    vector_store = FakeVectorStore()
    metadata_store = FakeMetadataStore()
    _patch(monkeypatch, vector_store)
    await metadata_store.create_document("doc-1", "a.pdf", "/tmp/a.pdf")

    await indexing.write_chunks(metadata_store, "doc-1", ["a", "b", "c"], [[0.1], [0.2], [0.3]])
    result = await indexing.write_chunks(metadata_store, "doc-1", ["a"], [[0.1]])

    assert result["action"] == "update"
    assert result["chunks_removed"] == 2
    assert vector_store.chunk_ids_present(COLLECTION_NAME) == {"doc-1:0"}


async def test_supersede_marks_old_documents_chunks_not_current(monkeypatch):
    vector_store = FakeVectorStore()
    metadata_store = FakeMetadataStore()
    _patch(monkeypatch, vector_store)
    await metadata_store.create_document("old-doc", "old.pdf", "/tmp/old.pdf")
    await metadata_store.create_document("new-doc", "new.pdf", "/tmp/new.pdf", supersedes="old-doc")

    await indexing.write_chunks(metadata_store, "old-doc", ["old chunk"], [[0.1]])
    await indexing.write_chunks(metadata_store, "new-doc", ["new chunk"], [[0.2]])

    old_metadata = vector_store.metadata_for(COLLECTION_NAME, "old-doc:0")
    assert old_metadata["is_current"] is False
    assert metadata_store.documents["old-doc"]["superseded_by"] == "new-doc"
