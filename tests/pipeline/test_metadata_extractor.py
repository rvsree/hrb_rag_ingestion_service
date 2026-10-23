"""Ported subset of hrb_chatbot_service's own test_document_metadata_extractor.py."""

from rag_ingestion.pipeline import metadata_extractor
from tests.conftest import FakeChatClient


def test_empty_text_returns_empty_result_without_calling_the_llm(monkeypatch):
    fake_client = FakeChatClient()
    monkeypatch.setattr(metadata_extractor, "get_openai_chat_client", lambda: fake_client)

    result = metadata_extractor.extract_document_metadata("   ")

    assert result == metadata_extractor.EMPTY_RESULT
    assert fake_client.calls == []


def test_extracts_fields_from_a_well_formed_json_response(monkeypatch):
    fake_client = FakeChatClient(answer='{"doc_category": "benefits", "department": "HR"}')
    monkeypatch.setattr(metadata_extractor, "get_openai_chat_client", lambda: fake_client)

    result = metadata_extractor.extract_document_metadata("Some real document text about benefits.")

    assert result["doc_category"] == "benefits"
    assert result["department"] == "HR"
    assert result["owner"] is None


def test_malformed_llm_response_degrades_to_empty_result_not_raise(monkeypatch):
    fake_client = FakeChatClient(answer="not json at all")
    monkeypatch.setattr(metadata_extractor, "get_openai_chat_client", lambda: fake_client)

    result = metadata_extractor.extract_document_metadata("Some real document text.")

    assert result == metadata_extractor.EMPTY_RESULT


def test_llm_failure_degrades_to_empty_result_not_raise(monkeypatch):
    def _raises(*args, **kwargs):
        raise RuntimeError("no API key configured")

    monkeypatch.setattr(metadata_extractor, "get_openai_chat_client", _raises)

    result = metadata_extractor.extract_document_metadata("Some real document text.")

    assert result == metadata_extractor.EMPTY_RESULT
