"""Fakes for this repo's own tests - same pattern hrb_chatbot_service's own
tests/conftest.py uses, trimmed to what this repo's tests actually need. No
test needs a real Postgres/Redis/OpenAI/Chroma/Pinecone call.

SAMPLE_PDF_PATH is a real KB document copied from hrb_chatbot_service's own
resources/kb_docs/ (not generated) - avoids adding a new PDF-authoring library
just to produce test fixtures, matching this project's own "no unreviewed
library" norm."""

import uuid
from pathlib import Path

import chromadb

SAMPLE_PDF_PATH = str(Path(__file__).parent / "fixtures" / "sample.pdf")


class FakeEmbeddingClient:
    """Stands in for hrb_common's OpenAIEmbeddingClient - fixed-dimension fake vectors."""

    def __init__(self, dimension: int = 4):
        self.dimension = dimension
        self.calls: list[dict] = []

    def get_embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "model": model})
        return [[float(i)] * self.dimension for i in range(len(texts))]


class FakeChatClient:
    """Stands in for hrb_common's OpenAIChatClient - returns a canned answer."""

    def __init__(self, answer: str = "{}"):
        self.answer = answer
        self.calls: list[dict] = []

    def ask(self, question, context=None, system_prompt=None, temperature=0.0, max_tokens=None, model_name_override=None):
        self.calls.append({"question": question, "context": context})
        return self.answer


class FakeEmbeddingCache:
    """In-memory stand-in for the real Redis-backed EmbeddingCache."""

    def __init__(self):
        self._store: dict[str, list[float]] = {}

    async def get_many(self, content_hashes, embedding_model):
        return {h: self._store[(embedding_model, h)] for h in content_hashes if (embedding_model, h) in self._store}

    async def set_many(self, entries, embedding_model):
        for content_hash, embedding in entries:
            self._store[(embedding_model, content_hash)] = embedding


class FakeVectorStore:
    """A real, in-memory chromadb.EphemeralClient() standing in for ChromaDBClient -
    same approach hrb_chatbot_service's own test_vector_indexer.py uses. LlamaIndex's
    ChromaVectorStore needs a real chromadb.Collection object, which a plain-dict
    fake can't satisfy - no disk, no network, so still real test isolation."""

    PROVIDER_NAME = "chromadb"

    def __init__(self):
        self._client = chromadb.EphemeralClient()
        # Ephemeral mode still shares collection storage by name across
        # separate client instances in the same test process - a random
        # suffix per instance is what actually isolates one test from another.
        self._suffix = uuid.uuid4().hex
        self.delete_calls: list[dict] = []

    def get_collection(self, collection_name: str):
        return self._client.get_or_create_collection(f"{collection_name}_{self._suffix}")

    def delete(self, collection_name: str, ids: list[str]) -> None:
        self.delete_calls.append({"collection_name": collection_name, "ids": list(ids)})
        self.get_collection(collection_name).delete(ids=ids)

    def update_metadata(self, collection_name: str, ids: list[str], metadatas: list[dict]) -> None:
        self.get_collection(collection_name).update(ids=ids, metadatas=metadatas)

    def health_check(self) -> dict:
        return {"provider": self.PROVIDER_NAME, "status": "healthy"}

    def chunk_ids_present(self, collection_name: str) -> set[str]:
        """Test helper only - the real ids currently stored."""
        return set(self.get_collection(collection_name).get()["ids"])

    def metadata_for(self, collection_name: str, chunk_id: str) -> dict:
        """Test helper only - one chunk's real, current metadata."""
        result = self.get_collection(collection_name).get(ids=[chunk_id])
        return result["metadatas"][0]


class FakeUploadFile:
    """A minimal stand-in for FastAPI's UploadFile - just what documents/service.py's
    own unit tests need (filename, content_type, async read())."""

    def __init__(self, filename: str, content: bytes, content_type: str = "application/pdf"):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self) -> bytes:
        return self._content


class FakeMetadataStore:
    """An in-memory metadata store - standing in for DocumentMetadataStore."""

    PROVIDER_NAME = "fake"

    def __init__(self):
        self.documents: dict[str, dict] = {}

    async def create_document(self, document_id, filename, file_path, file_size_bytes=0, content_hash="", supersedes=None, uploaded_by=None):
        self.documents[document_id] = {
            "id": document_id, "filename": filename, "file_path": file_path, "status": "uploaded",
            "error_message": None, "chunk_ids": None, "document_version": 0, "file_size_bytes": file_size_bytes,
            "content_hash": content_hash, "chunk_count": 0, "embedding_model": None, "embedding_dimension": None,
            "vector_db": None, "chunk_size": None, "chunk_overlap": None, "last_indexed_at": None,
            "is_current": True, "supersedes": supersedes, "superseded_by": None, "owner": None, "department": None,
            "doc_category": None, "purpose": None, "doc_description": None, "restricted_roles": None,
            "author": None, "doc_date": None, "doc_version": None, "uploaded_by": uploaded_by,
            "created_at": "2026-10-22T00:00:00+00:00", "updated_at": "2026-10-22T00:00:00+00:00",
        }

    async def find_by_content_hash(self, content_hash):
        matches = [doc for doc in self.documents.values() if doc.get("content_hash") == content_hash]
        return matches[-1] if matches else None

    async def delete_document(self, document_id):
        self.documents.pop(document_id, None)

    async def update_status(self, document_id, status, error_message=None):
        if document_id in self.documents:
            self.documents[document_id]["status"] = status
            self.documents[document_id]["error_message"] = error_message

    async def set_pending_overrides(self, document_id, overrides_json):
        if document_id in self.documents:
            self.documents[document_id]["pending_overrides"] = overrides_json

    async def record_successful_index(self, document_id, chunk_ids, embedding_model=None, embedding_dimension=None, vector_db=None, chunking_strategy=None, chunk_size=None, chunk_overlap=None):
        import json

        document = self.documents[document_id]
        document["chunk_ids"] = json.dumps(chunk_ids)
        document["chunk_count"] = len(chunk_ids)
        document["embedding_model"] = embedding_model
        document["chunking_strategy"] = chunking_strategy
        document["embedding_dimension"] = embedding_dimension
        document["vector_db"] = vector_db
        document["chunk_size"] = chunk_size
        document["chunk_overlap"] = chunk_overlap
        document["status"] = "indexed"
        document["error_message"] = None
        document["document_version"] += 1
        return document["document_version"]

    async def mark_superseded(self, document_id, superseded_by):
        import json

        document = self.documents.get(document_id)
        if document is None:
            return []
        document["is_current"] = False
        document["superseded_by"] = superseded_by
        return json.loads(document["chunk_ids"]) if document.get("chunk_ids") else []

    async def update_document_content(self, document_id, filename, file_path, file_size_bytes, content_hash):
        document = self.documents.get(document_id)
        if document is None:
            return
        document["filename"] = filename
        document["file_path"] = file_path
        document["file_size_bytes"] = file_size_bytes
        document["content_hash"] = content_hash

    async def record_document_metadata(self, document_id, owner, department, doc_category, purpose, doc_description, restricted_roles=None, effective_date=None, audience=None, confidentiality_level=None, author=None, doc_date=None, doc_version=None):
        import json

        document = self.documents.get(document_id)
        if document is not None:
            document.update(
                owner=owner, department=department, doc_category=doc_category, purpose=purpose,
                doc_description=doc_description, restricted_roles=json.dumps(restricted_roles) if restricted_roles else None,
                effective_date=effective_date, audience=audience, confidentiality_level=confidentiality_level,
                author=author, doc_date=doc_date, doc_version=doc_version,
            )

    async def get_document(self, document_id):
        return self.documents.get(document_id)

    async def list_documents(self):
        return list(self.documents.values())

    def health_check(self):
        return {"provider": self.PROVIDER_NAME, "status": "healthy"}
