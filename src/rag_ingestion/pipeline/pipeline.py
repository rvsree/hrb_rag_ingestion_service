"""Orchestrates turning one stored PDF into indexed, searchable chunks - ported
from hrb_chatbot_service's ai/doc_processing/pipeline.py. One real adaptation:
the metadata_store is passed in explicitly rather than resolved via a combined
db_gateway singleton (see indexing.py's own note)."""

from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret
from rag_ingestion.documents.store import DocumentMetadataStore
from rag_ingestion.pipeline.chunking import (
    CHUNKING_STRATEGIES,
    DEFAULT_CHUNK_OVERLAP,
    chunk_text_with_sections,
    decide_chunk_size,
    decide_chunking_strategy,
    extract_text_from_pdf,
)
from rag_ingestion.pipeline.embedding import generate_embeddings
from rag_ingestion.pipeline.indexing import apply_extracted_chunk_metadata, write_chunks
from rag_ingestion.pipeline.metadata_extractor import EMPTY_RESULT, extract_document_metadata

logger = get_logger("pipeline.pipeline")


def chunk_document(
    text: str,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> list[str]:
    from rag_ingestion.pipeline.chunking import chunk_text

    resolved_overlap = DEFAULT_CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    return chunk_text(text, chunking_strategy=chunking_strategy, chunk_size=chunk_size, chunk_overlap=resolved_overlap)


async def index_document(
    metadata_store: DocumentMetadataStore,
    document_id: str,
    file_path: str,
    vector_db: str | None = None,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    embedding_model: str | None = None,
    document_metadata_override: dict | None = None,
) -> dict:
    if chunking_strategy and chunking_strategy not in CHUNKING_STRATEGIES:
        raise ValueError(
            f"Unknown chunking_strategy {chunking_strategy!r} - choose one of {list(CHUNKING_STRATEGIES)}"
        )

    resolved_vector_db = read_secret("ACTIVE_VECTOR_DB", "chromadb", passed_in_value=vector_db)
    resolved_embedding_model = embedding_model or read_secret("OPENAI_EMBED_MODEL", "text-embedding-3-small")

    resolved_chunk_overlap = DEFAULT_CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap

    logger.info(
        "Indexing document %s from %s (vector_db=%s, chunking_strategy=%s, embedding_model=%s, "
        "chunk_size=%s, chunk_overlap=%s)",
        document_id, file_path, resolved_vector_db, chunking_strategy or "auto",
        resolved_embedding_model, chunk_size or "auto", resolved_chunk_overlap,
    )

    await metadata_store.update_status(document_id, "parsing")

    text = extract_text_from_pdf(file_path)
    resolved_chunking_strategy = chunking_strategy or decide_chunking_strategy(text)
    resolved_chunk_size = chunk_size or decide_chunk_size(text)

    await metadata_store.update_status(document_id, "chunking")
    chunks, section_ids = chunk_text_with_sections(
        text, chunking_strategy=chunking_strategy, chunk_size=resolved_chunk_size, chunk_overlap=resolved_chunk_overlap
    )

    await metadata_store.update_status(document_id, "embedding")
    embeddings = await generate_embeddings(chunks, embedding_model=resolved_embedding_model)

    await metadata_store.update_status(document_id, "indexing")
    result = await write_chunks(
        metadata_store,
        document_id,
        chunks,
        embeddings,
        vector_db=resolved_vector_db,
        embedding_model=resolved_embedding_model,
        chunking_strategy=resolved_chunking_strategy,
        chunk_size=resolved_chunk_size,
        chunk_overlap=resolved_chunk_overlap,
        section_ids=section_ids,
    )

    logger.info(
        "Document %s: %s (%d chunks indexed, %d stale chunks removed)",
        document_id, result["action"], result["chunks_indexed"], result["chunks_removed"],
    )

    document_metadata = dict(EMPTY_RESULT)
    if result["action"] == "insert":
        try:
            document_metadata = extract_document_metadata(text)
            if document_metadata_override:
                for key, value in document_metadata_override.items():
                    if value is not None:
                        document_metadata[key] = value

            await metadata_store.record_document_metadata(document_id, **document_metadata)
            await apply_extracted_chunk_metadata(
                document_id,
                result["chunk_ids"],
                resolved_vector_db,
                doc_category=document_metadata["doc_category"],
                department=document_metadata["department"],
                doc_description=document_metadata["doc_description"],
            )
        except Exception as error:
            logger.warning(
                "Document metadata extraction/recording failed for %s: %s: %s",
                document_id, type(error).__name__, error,
            )

    return {
        "document_id": document_id,
        "vector_db": resolved_vector_db,
        "chunking_strategy": resolved_chunking_strategy,
        "embedding_model": resolved_embedding_model,
        "chunk_size": resolved_chunk_size,
        "chunk_overlap": resolved_chunk_overlap,
        "document_metadata": document_metadata,
        **result,
    }
