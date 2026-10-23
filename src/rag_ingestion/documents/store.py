"""Document-metadata store - ported from hrb_chatbot_service's
common/clients/db_client/postgres_client.py. Real SQL/table DDL unchanged; the one
real adaptation is connection plumbing - the original builds its own psycopg
connection from read_setting(); this repo has no db_gateway, so it goes through
hrb_common.db.connection.get_connection() instead (same swap
hrb_platform_components' own interview_qa/store.py already proved out once).

No BaseMetadataClient parent here - the original's SQLite alternative doesn't
exist in this repo's scope, so there's nothing to share an interface with."""

import asyncio
import json

import psycopg

from hrb_common.observability.call_logger import log_backend_call
from hrb_common.observability.logger import get_logger
from rag_ingestion.gateway import get_postgres_connection

logger = get_logger("documents.store")

CREATE_DOCUMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

ADD_COLUMNS = [
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_ids TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding_model TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding_dimension INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS vector_db TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_size INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_overlap INTEGER",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunking_strategy TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_indexed_at TIMESTAMPTZ",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_size_bytes INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT true",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS supersedes TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS superseded_by TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS owner TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS department TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_category TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS purpose TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_description TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS effective_date TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS audience TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS confidentiality_level TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS restricted_roles TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS uploaded_by TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS author TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_date TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_version TEXT",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS pending_overrides TEXT",
]

CREATE_CONTENT_HASH_INDEX = "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash)"

CREATE_CHUNKS_TABLE = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT true
)
"""
CREATE_CHUNKS_DOCUMENT_ID_INDEX = "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id)"


class DocumentMetadataStore:
    """Stores document metadata in the hrb_rag_ingestion Postgres schema."""

    PROVIDER_NAME = "postgres"

    def __init__(self):
        self._table_ready = False

    def _connect(self) -> psycopg.Connection:
        """Synchronous connect - always run this through asyncio.to_thread()."""
        return get_postgres_connection()

    def _ensure_table_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(CREATE_DOCUMENTS_TABLE)
            for add_column in ADD_COLUMNS:
                conn.execute(add_column)
            conn.execute(CREATE_CONTENT_HASH_INDEX)
            conn.execute(CREATE_CHUNKS_TABLE)
            conn.execute(CREATE_CHUNKS_DOCUMENT_ID_INDEX)
            conn.commit()

    async def _ensure_table(self) -> None:
        if self._table_ready:
            return
        await asyncio.to_thread(self._ensure_table_sync)
        self._table_ready = True

    def _create_document_sync(
        self, document_id, filename, file_path, file_size_bytes, content_hash, supersedes, uploaded_by
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO documents (id, filename, file_path, status, error_message, "
                "created_at, updated_at, document_version, file_size_bytes, content_hash, "
                "is_current, supersedes, uploaded_by) "
                "VALUES (%s, %s, %s, 'uploaded', NULL, now(), now(), 0, %s, %s, true, %s, %s)",
                (document_id, filename, file_path, file_size_bytes, content_hash, supersedes, uploaded_by),
            )
            conn.commit()

    async def find_by_content_hash(self, content_hash: str) -> dict | None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.find_by_content_hash"):
            return await asyncio.to_thread(self._find_by_content_hash_sync, content_hash)

    def _find_by_content_hash_sync(self, content_hash: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row_factory) as cur:
                cur.execute(
                    "SELECT * FROM documents WHERE content_hash = %s ORDER BY created_at DESC LIMIT 1",
                    (content_hash,),
                )
                return cur.fetchone()

    async def create_document(
        self, document_id, filename, file_path, file_size_bytes, content_hash, supersedes=None, uploaded_by=None
    ) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.create_document", document_id=document_id):
            await asyncio.to_thread(
                self._create_document_sync,
                document_id, filename, file_path, file_size_bytes, content_hash, supersedes, uploaded_by,
            )

    def _update_status_sync(self, document_id: str, status: str, error_message: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET status = %s, error_message = %s, updated_at = now() WHERE id = %s",
                (status, error_message, document_id),
            )
            conn.commit()

    async def update_status(self, document_id: str, status: str, error_message: str | None = None) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.update_status", document_id=document_id, status=status):
            await asyncio.to_thread(self._update_status_sync, document_id, status, error_message)

    def _set_pending_overrides_sync(self, document_id: str, overrides_json: str | None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE documents SET pending_overrides = %s WHERE id = %s", (overrides_json, document_id))
            conn.commit()

    async def set_pending_overrides(self, document_id: str, overrides_json: str | None) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.set_pending_overrides", document_id=document_id):
            await asyncio.to_thread(self._set_pending_overrides_sync, document_id, overrides_json)

    def _record_successful_index_sync(
        self, document_id, chunk_ids, embedding_model, embedding_dimension, vector_db, chunking_strategy, chunk_size, chunk_overlap
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE documents SET chunk_ids = %s, chunk_count = %s, embedding_model = %s, "
                "embedding_dimension = %s, vector_db = %s, chunking_strategy = %s, chunk_size = %s, chunk_overlap = %s, "
                "status = 'indexed', error_message = NULL, last_indexed_at = now(), updated_at = now(), "
                "document_version = document_version + 1 WHERE id = %s RETURNING document_version",
                (
                    json.dumps(chunk_ids), len(chunk_ids), embedding_model, embedding_dimension, vector_db,
                    chunking_strategy, chunk_size, chunk_overlap, document_id,
                ),
            ).fetchone()

            conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
            if chunk_ids:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO chunks (chunk_id, document_id, chunk_index, created_at, is_current) "
                        "VALUES (%s, %s, %s, now(), true)",
                        [(chunk_id, document_id, index) for index, chunk_id in enumerate(chunk_ids)],
                    )
            conn.commit()
        return row[0]

    async def record_successful_index(
        self, document_id, chunk_ids, embedding_model, embedding_dimension, vector_db, chunking_strategy, chunk_size, chunk_overlap
    ) -> int:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.record_successful_index", document_id=document_id):
            return await asyncio.to_thread(
                self._record_successful_index_sync,
                document_id, chunk_ids, embedding_model, embedding_dimension, vector_db, chunking_strategy, chunk_size, chunk_overlap,
            )

    def _mark_superseded_sync(self, document_id: str, superseded_by: str) -> list[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT chunk_ids FROM documents WHERE id = %s", (document_id,)).fetchone()
            conn.execute(
                "UPDATE documents SET is_current = false, superseded_by = %s, updated_at = now() WHERE id = %s",
                (superseded_by, document_id),
            )
            conn.execute("UPDATE chunks SET is_current = false WHERE document_id = %s", (document_id,))
            conn.commit()

        if row is None or row[0] is None:
            return []
        return json.loads(row[0])

    async def mark_superseded(self, document_id: str, superseded_by: str) -> list[str]:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.mark_superseded", document_id=document_id, superseded_by=superseded_by):
            return await asyncio.to_thread(self._mark_superseded_sync, document_id, superseded_by)

    def _update_document_content_sync(self, document_id, filename, file_path, file_size_bytes, content_hash) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET filename = %s, file_path = %s, file_size_bytes = %s, content_hash = %s, "
                "updated_at = now() WHERE id = %s",
                (filename, file_path, file_size_bytes, content_hash, document_id),
            )
            conn.commit()

    async def update_document_content(self, document_id, filename, file_path, file_size_bytes, content_hash) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.update_document_content", document_id=document_id):
            await asyncio.to_thread(
                self._update_document_content_sync, document_id, filename, file_path, file_size_bytes, content_hash
            )

    def _record_document_metadata_sync(
        self, document_id, owner, department, doc_category, purpose, doc_description,
        restricted_roles, effective_date, audience, confidentiality_level, author, doc_date, doc_version,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET owner = %s, department = %s, doc_category = %s, purpose = %s, "
                "doc_description = %s, restricted_roles = %s, effective_date = %s, audience = %s, "
                "confidentiality_level = %s, author = %s, doc_date = %s, doc_version = %s, "
                "updated_at = now() WHERE id = %s",
                (
                    owner, department, doc_category, purpose, doc_description,
                    json.dumps(restricted_roles) if restricted_roles else None,
                    effective_date, audience, confidentiality_level, author, doc_date, doc_version, document_id,
                ),
            )
            conn.commit()

    async def record_document_metadata(
        self, document_id, owner, department, doc_category, purpose, doc_description,
        restricted_roles=None, effective_date=None, audience=None, confidentiality_level=None,
        author=None, doc_date=None, doc_version=None,
    ) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.record_document_metadata", document_id=document_id):
            await asyncio.to_thread(
                self._record_document_metadata_sync,
                document_id, owner, department, doc_category, purpose, doc_description,
                restricted_roles, effective_date, audience, confidentiality_level, author, doc_date, doc_version,
            )

    def _get_document_sync(self, document_id: str) -> dict | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row_factory) as cur:
                cur.execute("SELECT * FROM documents WHERE id = %s", (document_id,))
                return cur.fetchone()

    async def get_document(self, document_id: str) -> dict | None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.get_document", document_id=document_id):
            return await asyncio.to_thread(self._get_document_sync, document_id)

    def _list_documents_sync(self) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=self._dict_row_factory) as cur:
                cur.execute("SELECT * FROM documents ORDER BY created_at DESC")
                return cur.fetchall()

    async def list_documents(self) -> list[dict]:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.list_documents"):
            return await asyncio.to_thread(self._list_documents_sync)

    def _delete_document_sync(self, document_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))
            conn.commit()

    async def delete_document(self, document_id: str) -> None:
        await self._ensure_table()
        with log_backend_call(logger, "postgres", "metadata.delete_document", document_id=document_id):
            await asyncio.to_thread(self._delete_document_sync, document_id)

    @staticmethod
    def _dict_row_factory(cursor):
        columns = [desc.name for desc in cursor.description]

        def make_row(values):
            row = dict(zip(columns, values, strict=True))
            for key in ("created_at", "updated_at", "last_indexed_at"):
                if row.get(key) is not None:
                    row[key] = row[key].isoformat()
            return row

        return make_row

    def health_check(self) -> dict:
        result = {"provider": self.PROVIDER_NAME}
        try:
            with self._connect() as conn:
                conn.execute(CREATE_DOCUMENTS_TABLE)
                for add_column in ADD_COLUMNS:
                    conn.execute(add_column)
                conn.commit()
                document_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            result["status"] = "healthy"
            result["documents_stored"] = document_count
        except Exception as error:
            result["status"] = "unhealthy"
            result["message"] = str(error)
        return result


_metadata_store: DocumentMetadataStore | None = None


def get_metadata_store() -> DocumentMetadataStore:
    global _metadata_store
    if _metadata_store is None:
        _metadata_store = DocumentMetadataStore()
    return _metadata_store
