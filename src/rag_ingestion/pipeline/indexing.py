"""Writes chunks into a vector index built with LlamaIndex's VectorStoreIndex -
ported from hrb_chatbot_service's ai/doc_processing/indexing/vector_indexer.py."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.vector_stores.pinecone import PineconeVectorStore

from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret
from rag_ingestion.documents.store import DocumentMetadataStore
from rag_ingestion.gateway import COLLECTION_NAME, get_vector_store

logger = get_logger("pipeline.indexing")


def build_chunk_ids(document_id: str, chunk_count: int) -> list[str]:
    return [f"{document_id}:{i}" for i in range(chunk_count)]


def storage_chunk_ids(provider_name: str, document_id: str, chunk_ids: list[str]) -> list[str]:
    """Pinecone prefixes stored ids with f"{document_id}#" (LlamaIndex's doing) - Chroma does not."""
    if provider_name == "pinecone":
        return [f"{document_id}#{chunk_id}" for chunk_id in chunk_ids]
    return chunk_ids


def _llama_vector_index(vector_store_client) -> VectorStoreIndex:
    if vector_store_client.PROVIDER_NAME == "pinecone":
        pinecone_index = vector_store_client.get_index()
        llama_vector_store = PineconeVectorStore(pinecone_index=pinecone_index, namespace=COLLECTION_NAME)
    else:
        chroma_collection = vector_store_client.get_collection(COLLECTION_NAME)
        llama_vector_store = ChromaVectorStore(chroma_collection=chroma_collection)

    # Passed explicitly, not left to LlamaIndex's own default resolution -
    # that reads OPENAI_API_KEY straight from os.environ, bypassing
    # read_secret()'s Secrets Manager fallback. Works fine in a container
    # where the real key is already a plain env var, but not in this
    # Lambda, which deliberately keeps it Secrets-Manager-only.
    embed_model = OpenAIEmbedding(
        api_key=read_secret("OPENAI_API_KEY", ""),
        api_base=read_secret("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=read_secret("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
    )
    return VectorStoreIndex.from_vector_store(llama_vector_store, embed_model=embed_model)


def _extracted_fields(doc_category: str | None, department: str | None, doc_description: str | None) -> dict:
    # Only include fields with a value - Pinecone's update_metadata() rejects None outright.
    fields = {"doc_category": doc_category, "department": department, "doc_description": doc_description}
    return {key: value for key, value in fields.items() if value is not None}


def _build_nodes(
    document_id: str, chunk_ids: list[str], chunks: list[str], embeddings: list[list[float]], metadatas: list[dict]
) -> list[TextNode]:
    nodes = []
    for chunk_id, text, embedding, metadata in zip(chunk_ids, chunks, embeddings, metadatas, strict=True):
        node = TextNode(id_=chunk_id, text=text, embedding=embedding, metadata=metadata)
        node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(node_id=document_id)
        nodes.append(node)
    return nodes


async def _lookup_existing_chunks(metadata_store: DocumentMetadataStore, document_id: str) -> tuple[str, list[str], dict | None]:
    existing_document = await metadata_store.get_document(document_id)
    if existing_document and existing_document.get("chunk_ids"):
        return "update", json.loads(existing_document["chunk_ids"]), existing_document
    return "insert", [], existing_document


def _build_chunk_metadatas(
    existing_document: dict | None, chunk_count: int, now: str, section_ids: list[str | None] | None = None
) -> list[dict]:
    extracted_fields = _extracted_fields(
        existing_document.get("doc_category") if existing_document else None,
        existing_document.get("department") if existing_document else None,
        existing_document.get("doc_description") if existing_document else None,
    )
    resolved_section_ids = section_ids if section_ids is not None else [None] * chunk_count
    metadatas = []
    for i in range(chunk_count):
        metadata = {"chunk_index": i, "is_current": True, "indexed_at": now, **extracted_fields}
        if resolved_section_ids[i] is not None:
            metadata["section_id"] = resolved_section_ids[i]
        metadatas.append(metadata)
    return metadatas


def _write_new_vector_chunks(
    vector_store_client,
    resolved_vector_db: str,
    document_id: str,
    old_chunk_ids: list[str],
    new_chunk_ids: list[str],
    chunks: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
) -> None:
    if old_chunk_ids:
        vector_store_client.delete(
            collection_name=COLLECTION_NAME,
            ids=storage_chunk_ids(resolved_vector_db, document_id, old_chunk_ids),
        )

    lock = getattr(vector_store_client, "collection_lock", None) or nullcontext()
    with lock:
        llama_index = _llama_vector_index(vector_store_client)
        nodes = _build_nodes(document_id, new_chunk_ids, chunks, embeddings, metadatas)
        llama_index.insert_nodes(nodes)


async def _record_index_success(
    metadata_store: DocumentMetadataStore,
    document_id: str,
    new_chunk_ids: list[str],
    embedding_model: str | None,
    embedding_dimension: int,
    resolved_vector_db: str,
    chunking_strategy: str | None,
    chunk_size: int | None,
    chunk_overlap: int | None,
) -> int:
    return await metadata_store.record_successful_index(
        document_id,
        new_chunk_ids,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        vector_db=resolved_vector_db,
        chunking_strategy=chunking_strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


async def _apply_supersede(
    metadata_store: DocumentMetadataStore,
    vector_store_client,
    resolved_vector_db: str,
    document_id: str,
    supersedes: str | None,
    now: str,
) -> None:
    if not supersedes:
        return

    superseded_chunk_ids = await metadata_store.mark_superseded(supersedes, superseded_by=document_id)
    if not superseded_chunk_ids:
        return

    old_metadatas = [
        {"chunk_index": int(chunk_id.split(":")[1]), "is_current": False, "indexed_at": now}
        for chunk_id in superseded_chunk_ids
    ]
    vector_store_client.update_metadata(
        collection_name=COLLECTION_NAME,
        ids=storage_chunk_ids(resolved_vector_db, supersedes, superseded_chunk_ids),
        metadatas=old_metadatas,
    )
    logger.info(
        "Document %s superseded by %s - %d old chunk(s) marked is_current=false",
        supersedes,
        document_id,
        len(superseded_chunk_ids),
    )


async def write_chunks(
    metadata_store: DocumentMetadataStore,
    document_id: str,
    chunks: list[str],
    embeddings: list[list[float]],
    vector_db: str | None = None,
    embedding_model: str | None = None,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    section_ids: list[str | None] | None = None,
) -> dict:
    vector_store_client = get_vector_store(provider=vector_db)
    resolved_vector_db = vector_store_client.PROVIDER_NAME

    action, old_chunk_ids, existing_document = await _lookup_existing_chunks(metadata_store, document_id)

    now = datetime.now(UTC).isoformat()
    new_chunk_ids = build_chunk_ids(document_id, len(chunks))
    metadatas = _build_chunk_metadatas(existing_document, len(chunks), now, section_ids)

    _write_new_vector_chunks(
        vector_store_client, resolved_vector_db, document_id, old_chunk_ids, new_chunk_ids, chunks, embeddings, metadatas
    )

    stale_ids = [chunk_id for chunk_id in old_chunk_ids if chunk_id not in new_chunk_ids]
    if stale_ids:
        logger.info("Removed %d stale chunk(s) for document %s", len(stale_ids), document_id)

    embedding_dimension = len(embeddings[0]) if embeddings else 0

    document_version = await _record_index_success(
        metadata_store, document_id, new_chunk_ids, embedding_model, embedding_dimension,
        resolved_vector_db, chunking_strategy, chunk_size, chunk_overlap,
    )

    supersedes = existing_document.get("supersedes") if existing_document else None
    if action == "insert":
        await _apply_supersede(metadata_store, vector_store_client, resolved_vector_db, document_id, supersedes, now)

    logger.info(
        "indexing: wrote %d chunk(s) for document %s via LlamaIndex VectorStoreIndex (vector_db=%s)",
        len(chunks),
        document_id,
        resolved_vector_db,
    )

    return {
        "action": action,
        "chunks_indexed": len(chunks),
        "chunks_removed": len(stale_ids),
        "embedding_dimension": embedding_dimension,
        "document_version": document_version,
        "chunk_ids": new_chunk_ids,
    }


async def apply_extracted_chunk_metadata(
    document_id: str,
    chunk_ids: list[str],
    vector_db: str | None,
    doc_category: str | None,
    department: str | None,
    doc_description: str | None,
) -> None:
    """Patch extracted fields onto just-written chunks - only needed after a first index."""
    if not chunk_ids:
        return

    now = datetime.now(UTC).isoformat()
    extracted_fields = _extracted_fields(doc_category, department, doc_description)
    metadatas = [
        {"chunk_index": int(chunk_id.split(":")[1]), "is_current": True, "indexed_at": now, **extracted_fields}
        for chunk_id in chunk_ids
    ]

    vector_store_client = get_vector_store(provider=vector_db)
    vector_store_client.update_metadata(
        collection_name=COLLECTION_NAME,
        ids=storage_chunk_ids(vector_store_client.PROVIDER_NAME, document_id, chunk_ids),
        metadatas=metadatas,
    )
    logger.info(
        "Document %s: patched doc_category/department/doc_description onto %d chunk(s) after first-index extraction",
        document_id,
        len(chunk_ids),
    )
