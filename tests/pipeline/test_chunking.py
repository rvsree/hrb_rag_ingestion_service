"""Representative subset of hrb_chatbot_service's own test_text_chunker.py -
not an exhaustive 1:1 port (that file is 358 lines covering every strategy's
edge cases); covers the behaviors this phase's own spec calls out as needing
parity: strategy auto-selection, table extraction into the chunk stream, and
the chunk_size safety cap."""

from rag_ingestion.pipeline.chunking import (
    chunk_fixed,
    chunk_recursive,
    chunk_text,
    chunk_text_with_sections,
    decide_chunking_strategy,
    extract_text_from_pdf,
)
from tests.conftest import SAMPLE_PDF_PATH


def test_extract_text_from_pdf_returns_real_text():
    text = extract_text_from_pdf(SAMPLE_PDF_PATH)
    assert len(text) > 100


def test_decide_chunking_strategy_picks_none_for_short_text():
    assert decide_chunking_strategy("short text") == "none"


def test_decide_chunking_strategy_picks_markdown_for_heading_lines():
    text = "# Heading\n\n" + ("word " * 500)
    assert decide_chunking_strategy(text) == "markdown"


def test_decide_chunking_strategy_picks_recursive_for_long_plain_text():
    text = "word " * 500
    assert decide_chunking_strategy(text) == "recursive"


def test_chunk_fixed_respects_chunk_size():
    # chunk_fixed only splits on "\n" (CharacterTextSplitter's single separator) -
    # needs newlines to split on, unlike chunk_recursive's multi-separator fallback.
    text = "\n".join(["aaaaa"] * 10)
    chunks = chunk_fixed(text, chunk_size=10, chunk_overlap=0)
    assert all(len(chunk) <= 10 for chunk in chunks)


def test_chunk_recursive_splits_on_paragraph_breaks():
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
    chunks = chunk_recursive(text, chunk_size=20, chunk_overlap=0)
    assert len(chunks) >= 2


def test_table_blocks_are_kept_whole_not_split_across_chunks():
    table = "[TABLE]\n| a | b |\n| --- | --- |\n| 1 | 2 |\n[/TABLE]"
    text = f"Some prose before.\n\n{table}\n\nSome prose after."
    chunks = chunk_text(text, chunking_strategy="recursive", chunk_size=10, chunk_overlap=0)
    assert table in chunks


def test_chunk_text_with_sections_returns_none_section_ids_for_non_structure_strategy():
    chunks, section_ids = chunk_text_with_sections("word " * 50, chunking_strategy="recursive", chunk_size=50, chunk_overlap=0)
    assert section_ids == [None] * len(chunks)


def test_document_structure_strategy_tags_chunks_with_section_ids():
    text = "Section 1: Eligibility\nSome eligibility text that is reasonably long to force a chunk.\n"
    chunks, section_ids = chunk_text_with_sections(text, chunking_strategy="document_structure", chunk_size=50, chunk_overlap=0)
    assert any(section_id == "eligibility" for section_id in section_ids)
