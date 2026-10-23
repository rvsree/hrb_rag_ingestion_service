"""End-to-end test of index_document() - real chunking/table-extraction against
a real sample PDF, fake embedding client, fake chat client (metadata extraction),
real in-memory chroma for indexing. Ported in spirit from hrb_chatbot_service's
own test_doc_processing_pipeline.py."""

from rag_ingestion.pipeline import embedding, indexing, metadata_extractor, pipeline
from tests.conftest import FakeChatClient, FakeEmbeddingCache, FakeEmbeddingClient, FakeMetadataStore, FakeVectorStore, SAMPLE_PDF_PATH


async def test_index_document_runs_the_full_pipeline_and_records_metadata(monkeypatch):
    vector_store = FakeVectorStore()
    metadata_store = FakeMetadataStore()
    fake_embedding_client = FakeEmbeddingClient(dimension=4)
    fake_chat_client = FakeChatClient(answer='{"doc_category": "benefits"}')

    monkeypatch.setattr(indexing, "get_vector_store", lambda provider=None: vector_store)
    monkeypatch.setattr(embedding, "get_openai_embedding_client", lambda: fake_embedding_client)
    monkeypatch.setattr(embedding, "_cache", FakeEmbeddingCache())
    monkeypatch.setattr(metadata_extractor, "get_openai_chat_client", lambda: fake_chat_client)

    await metadata_store.create_document("doc-1", "sample.pdf", SAMPLE_PDF_PATH)

    result = await pipeline.index_document(metadata_store, "doc-1", SAMPLE_PDF_PATH)

    assert result["action"] == "insert"
    assert result["chunks_indexed"] > 0
    assert result["document_metadata"]["doc_category"] == "benefits"
    assert metadata_store.documents["doc-1"]["status"] == "indexed"
