"""Route-level tests - the route-specific logic not already covered by
tests/documents/test_service.py (payload JSON parsing, 404 translation)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag_ingestion.documents import routes
from rag_ingestion.documents import service as documents_service


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_list_documents_returns_empty_list(monkeypatch):
    monkeypatch.setattr(documents_service, "list_documents", _async_return([]))
    client = _client()

    response = client.get("/documents")

    assert response.status_code == 200
    assert response.json() == {"count": 0, "documents": []}


def test_get_document_returns_404_for_unknown_id(monkeypatch):
    monkeypatch.setattr(documents_service, "get_document", _async_return(None))
    client = _client()

    response = client.get("/documents/unknown-id")

    assert response.status_code == 404
    assert response.json()["code"] == "DOCUMENT_NOT_FOUND"


def test_delete_document_returns_404_for_unknown_id(monkeypatch):
    monkeypatch.setattr(documents_service, "delete_document", _async_return(None))
    client = _client()

    response = client.delete("/documents/unknown-id")

    assert response.status_code == 404
    assert response.json()["code"] == "DOCUMENT_NOT_FOUND"


def test_upload_documents_rejects_malformed_payload_json():
    client = _client()

    response = client.post(
        "/documents",
        files={"files": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        data={"payload": "{not valid json"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_upload_documents_rejects_supersedes_with_a_batch():
    client = _client()
    payload = '{"document_metadata": {"supersedes_document_id": "doc-1"}}'

    response = client.post(
        "/documents",
        files=[
            ("files", ("a.pdf", b"%PDF-1.4", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4", "application/pdf")),
        ],
        data={"payload": payload},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn
