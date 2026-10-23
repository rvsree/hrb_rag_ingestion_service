"""Ported from hrb_chatbot_service's own test_table_extractor.py - extract_tables_from_pdf()
never raises on a real PDF, even one with no tables."""

from rag_ingestion.pipeline.table_extractor import extract_tables_from_pdf
from tests.conftest import SAMPLE_PDF_PATH


def test_extract_tables_from_pdf_never_raises_on_a_real_pdf():
    tables = extract_tables_from_pdf(SAMPLE_PDF_PATH)
    assert isinstance(tables, list)
