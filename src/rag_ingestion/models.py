"""Request/response contracts - ported from hrb_chatbot_service's models/documents.py
and models/common.py, as-is (same fields, same validation), except:
- `user_profile` is kept on request bodies for wire-shape parity only - nothing
  in this repo enforces its `role` field (no RBAC layer here yet, see ROADMAP.md
  Phase 1's own "Out of scope" note).
- `IdentityPayload`/`identity_from_query_params` are dropped entirely - those only
  existed to carry a role to check, and there's no role check here to carry it to.
"""

import json
import os

from pydantic import BaseModel, Field, model_validator

MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_FILE_SIZE_BYTES", str(20 * 1024 * 1024)))
ALLOWED_CONTENT_TYPE = "application/pdf"
PAYLOAD_MAX_LENGTH = 20000
TEST_NOISE_MAX_FILE_SIZE_BYTES = int(os.getenv("TEST_NOISE_MAX_FILE_SIZE_BYTES", "1024"))
MIN_CHUNK_SIZE = 200
MAX_CHUNK_SIZE = 4000


class UserProfile(BaseModel):
    employee_id: str = Field(..., max_length=50)
    full_name: str = Field(..., max_length=200)
    role: str = Field(..., max_length=50)
    department: str | None = Field(None, max_length=100)


class ChunkInfoInput(BaseModel):
    chunking_strategy: str | None = Field(
        None, description="Override auto-selected chunking - applies to every file in this batch."
    )
    chunk_size: int | None = Field(
        None, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE, description="Override CHUNK_DEFAULT_SIZE for this batch."
    )
    chunk_overlap: int | None = Field(None, ge=0, description="Override CHUNK_DEFAULT_OVERLAP for this batch.")

    @model_validator(mode="after")
    def _overlap_must_fit_within_half_the_chunk_size(self) -> "ChunkInfoInput":
        if self.chunk_size is not None and self.chunk_overlap is not None and self.chunk_overlap > self.chunk_size // 2:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) can be at most half of chunk_size ({self.chunk_size})."
            )
        return self


class DocumentMetadataInput(BaseModel):
    supersedes_document_id: str | None = Field(None, max_length=100)
    doc_category: str | None = Field(None, max_length=100)
    department: str | None = Field(None, max_length=100)
    doc_description: str | None = Field(None, max_length=100)
    restricted_roles: list[str] | None = None
    owner: str | None = Field(None, max_length=200)
    purpose: str | None = Field(None, max_length=500)
    effective_date: str | None = Field(None, max_length=100)
    audience: str | None = Field(None, max_length=200)
    confidentiality_level: str | None = Field(None, max_length=100)
    author: str | None = Field(None, max_length=200)
    doc_date: str | None = Field(None, max_length=100)
    doc_version: str | None = Field(None, max_length=100)


class UploadDocumentsPayload(BaseModel):
    user_profile: UserProfile | None = None
    chunk_info: ChunkInfoInput | None = None
    document_metadata: DocumentMetadataInput | None = None


class ReindexDocumentPayload(BaseModel):
    user_profile: UserProfile | None = None
    chunk_info: ChunkInfoInput | None = None


class ChunkInfoResult(BaseModel):
    chunking_strategy: str | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    action: str | None = None
    chunks_indexed: int | None = None
    chunks_removed: int | None = None


class DocumentMetadataResult(BaseModel):
    doc_category: str | None = None
    department: str | None = None
    doc_description: str | None = None
    restricted_roles: list[str] | None = None
    owner: str | None = None
    purpose: str | None = None
    effective_date: str | None = None
    audience: str | None = None
    confidentiality_level: str | None = None
    author: str | None = None
    doc_date: str | None = None
    doc_version: str | None = None


class VersioningInfo(BaseModel):
    document_version: int | None = None
    is_current: bool | None = None
    supersedes: str | None = None
    superseded_by: str | None = None


class DocumentUploadResult(BaseModel):
    filename: str
    document_id: str | None = None
    status: str
    chunk_info: ChunkInfoResult | None = None
    document_metadata: DocumentMetadataResult | None = None
    versioning_info: VersioningInfo | None = None
    uploaded_by: str | None = None
    error: str | None = None
    error_code: str | None = None
    message: str | None = None
    file_size_bytes: int | None = None


class DocumentUploadResponse(BaseModel):
    uploaded_count: int
    duplicate_count: int
    rejected_count: int
    results: list[DocumentUploadResult]


class DocumentRecord(BaseModel):
    id: str
    filename: str
    file_path: str
    status: str
    error_message: str | None = None
    created_at: str
    updated_at: str
    last_indexed_at: str | None = None
    chunk_ids: list[str] | None = None
    chunk_count: int = 0
    file_size_bytes: int = 0
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    vector_db: str | None = None
    uploaded_by: str | None = None
    chunk_info: ChunkInfoResult | None = None
    document_metadata: DocumentMetadataResult | None = None
    versioning_info: VersioningInfo | None = None

    @classmethod
    def from_row(cls, document: dict) -> "DocumentRecord":
        restricted_roles_raw = document.get("restricted_roles")
        restricted_roles = json.loads(restricted_roles_raw) if restricted_roles_raw else None
        return cls(
            id=document["id"],
            filename=document["filename"],
            file_path=document["file_path"],
            status=document["status"],
            error_message=document.get("error_message"),
            created_at=document["created_at"],
            updated_at=document["updated_at"],
            last_indexed_at=document.get("last_indexed_at"),
            chunk_ids=document.get("chunk_ids"),
            chunk_count=document.get("chunk_count", 0),
            file_size_bytes=document.get("file_size_bytes", 0),
            embedding_model=document.get("embedding_model"),
            embedding_dimension=document.get("embedding_dimension"),
            vector_db=document.get("vector_db"),
            uploaded_by=document.get("uploaded_by"),
            chunk_info=ChunkInfoResult(
                chunking_strategy=document.get("chunking_strategy"),
                chunk_size=document.get("chunk_size"),
                chunk_overlap=document.get("chunk_overlap"),
            ),
            document_metadata=DocumentMetadataResult(
                doc_category=document.get("doc_category"),
                department=document.get("department"),
                doc_description=document.get("doc_description"),
                restricted_roles=restricted_roles,
                owner=document.get("owner"),
                purpose=document.get("purpose"),
                effective_date=document.get("effective_date"),
                audience=document.get("audience"),
                confidentiality_level=document.get("confidentiality_level"),
                author=document.get("author"),
                doc_date=document.get("doc_date"),
                doc_version=document.get("doc_version"),
            ),
            versioning_info=VersioningInfo(
                document_version=document.get("document_version"),
                is_current=document.get("is_current"),
                supersedes=document.get("supersedes"),
                superseded_by=document.get("superseded_by"),
            ),
        )


class DocumentListResponse(BaseModel):
    count: int
    documents: list[DocumentRecord]


class DocumentDeleteResponse(BaseModel):
    document_id: str
    filename: str
    chunks_removed: int
    deleted_by: str | None = None


class DocumentDeleteAllResponse(BaseModel):
    documents_deleted: int
    chunks_removed: int
    deleted_by: str | None = None


class TestNoiseDocument(BaseModel):
    id: str
    filename: str
    file_size_bytes: int
    created_at: str


class TestNoisePreviewResponse(BaseModel):
    count: int
    threshold_bytes: int
    documents: list[TestNoiseDocument]


class PresignedUploadRequest(BaseModel):
    user_profile: UserProfile | None = None
    filename: str = Field(..., max_length=255)
    content_type: str = Field(..., max_length=100)
    chunk_info: ChunkInfoInput | None = None
    document_metadata: DocumentMetadataInput | None = None


class PresignedUploadResponse(BaseModel):
    document_id: str
    upload_url: str
    expires_in_seconds: int
    status: str = "pending_upload"
