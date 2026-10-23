"""Extracts PDF text and splits it into chunks via LangChain's own splitters - ported
unmodified (same functions, same behavior) from hrb_chatbot_service's
ai/doc_processing/chunking/text_chunker.py, import paths adjusted only."""

import re

from langchain_experimental.text_splitter import SemanticChunker
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import (
    CharacterTextSplitter,
    HTMLHeaderTextSplitter,
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pypdf import PdfReader

from hrb_common.observability.logger import get_logger
from hrb_common.observability.secrets import read_secret
from rag_ingestion.pipeline.table_extractor import extract_tables_from_pdf

logger = get_logger("pipeline.chunking")

DEFAULT_CHUNK_SIZE = int(read_secret("CHUNK_DEFAULT_SIZE", "1000"))
DEFAULT_CHUNK_OVERLAP = int(read_secret("CHUNK_DEFAULT_OVERLAP", "150"))
WHOLE_DOCUMENT_MAX_LENGTH = DEFAULT_CHUNK_SIZE

TABLE_BLOCK_PATTERN = re.compile(r"\[TABLE\](.*?)\[/TABLE\]", re.DOTALL)

SECTION_HEADING_PATTERN = re.compile(r"^Section \d+:\s*(.+)$", re.MULTILINE)
SUBSECTION_HEADING_PATTERN = re.compile(r"^\d+\.\d+ ([A-Z].+)$", re.MULTILINE)

LARGE_DOCUMENT_MIN_LENGTH = int(read_secret("CHUNK_LARGE_DOCUMENT_MIN_LENGTH", "10000"))
LARGE_DOCUMENT_CHUNK_SIZE = int(read_secret("CHUNK_LARGE_DOCUMENT_CHUNK_SIZE", "1500"))


def _split_out_tables(text: str) -> tuple[str, list[str]]:
    table_blocks = [f"[TABLE]{match}[/TABLE]" for match in TABLE_BLOCK_PATTERN.findall(text)]
    text_without_tables = TABLE_BLOCK_PATTERN.sub("", text).strip()
    return text_without_tables, table_blocks


def extract_text_from_pdf(file_path: str) -> str:
    """Return every page's text plus any tables, appended as markdown blocks after the page text."""
    reader = PdfReader(file_path)
    pages_text = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(pages_text)

    tables = extract_tables_from_pdf(file_path)
    if tables:
        tables_section = "\n\n".join(f"[TABLE]\n{table}\n[/TABLE]" for table in tables)
        text = f"{text}\n\n{tables_section}"

    return text


def chunk_fixed(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    splitter = CharacterTextSplitter(separator="\n", chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.split_text(text)


def chunk_recursive(
    text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " "],
    )
    return splitter.split_text(text)


def _split_oversized_chunks(chunks: list[str], max_chunk_size: int, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    result = []
    for chunk in chunks:
        if len(chunk) > max_chunk_size:
            result.extend(chunk_recursive(chunk, chunk_size=max_chunk_size, chunk_overlap=chunk_overlap))
        else:
            result.append(chunk)
    return result


def chunk_semantic(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    api_key = read_secret("OPENAI_API_KEY", "")
    base_url = (read_secret("OPENAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
    embedding_model = read_secret("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    embeddings = OpenAIEmbeddings(api_key=api_key, base_url=base_url, model=embedding_model)
    splitter = SemanticChunker(embeddings, breakpoint_threshold_type="standard_deviation")
    raw_chunks = splitter.split_text(text)
    return _split_oversized_chunks(raw_chunks, chunk_size, chunk_overlap)


def chunk_markdown(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
    )
    documents = splitter.split_text(text)
    chunks = [document.page_content for document in documents if document.page_content.strip()]
    return _split_oversized_chunks(chunks, chunk_size, chunk_overlap)


def chunk_html(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    splitter = HTMLHeaderTextSplitter(headers_to_split_on=[("h1", "Header 1"), ("h2", "Header 2"), ("h3", "Header 3")])
    documents = splitter.split_text(text)
    chunks = [document.page_content for document in documents if document.page_content.strip()]
    return _split_oversized_chunks(chunks, chunk_size, chunk_overlap)


def chunk_none(text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    return _split_oversized_chunks([stripped], chunk_size, chunk_overlap)


def _slugify(heading_text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading_text.strip().lower())
    return slug.strip("-")


def chunk_by_document_structure(
    text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[dict]:
    section_matches = list(SECTION_HEADING_PATTERN.finditer(text))
    if not section_matches:
        return [{"text": chunk, "section_id": None} for chunk in chunk_recursive(text, chunk_size, chunk_overlap)]

    results: list[dict] = []

    preamble = text[: section_matches[0].start()].strip()
    if preamble:
        results += [{"text": chunk, "section_id": None} for chunk in chunk_recursive(preamble, chunk_size, chunk_overlap)]

    for index, match in enumerate(section_matches):
        section_end = section_matches[index + 1].start() if index + 1 < len(section_matches) else len(text)
        section_text = text[match.start() : section_end]
        section_slug = _slugify(match.group(1))

        subsection_matches = list(SUBSECTION_HEADING_PATTERN.finditer(section_text))
        if not subsection_matches:
            results += [
                {"text": chunk, "section_id": section_slug}
                for chunk in chunk_recursive(section_text, chunk_size, chunk_overlap)
            ]
            continue

        section_intro = section_text[: subsection_matches[0].start()].strip()
        if section_intro:
            results += [
                {"text": chunk, "section_id": section_slug}
                for chunk in chunk_recursive(section_intro, chunk_size, chunk_overlap)
            ]

        for sub_index, sub_match in enumerate(subsection_matches):
            sub_end = subsection_matches[sub_index + 1].start() if sub_index + 1 < len(subsection_matches) else len(section_text)
            subsection_text = section_text[sub_match.start() : sub_end]
            compound_id = f"{section_slug}/{_slugify(sub_match.group(1))}"
            results += [
                {"text": chunk, "section_id": compound_id}
                for chunk in chunk_recursive(subsection_text, chunk_size, chunk_overlap)
            ]

    return results


def chunk_document_structure(
    text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
) -> list[str]:
    return [chunk["text"] for chunk in chunk_by_document_structure(text, chunk_size, chunk_overlap)]


CHUNKING_STRATEGIES = {
    "fixed": chunk_fixed,
    "recursive": chunk_recursive,
    "semantic": chunk_semantic,
    "markdown": chunk_markdown,
    "html": chunk_html,
    "none": chunk_none,
    "document_structure": chunk_document_structure,
}


def decide_chunking_strategy(text: str) -> str:
    text_without_tables, _ = _split_out_tables(text)
    stripped = text_without_tables.strip()

    heading_lines = [line for line in stripped.splitlines() if line.strip().startswith("#")]
    if heading_lines:
        return "markdown"

    lowered = stripped.lower()
    html_heading_tags_found = [tag for tag in ("<h1", "<h2", "<h3") if tag in lowered]
    if html_heading_tags_found:
        return "html"

    character_count = len(stripped)
    if character_count <= WHOLE_DOCUMENT_MAX_LENGTH:
        return "none"

    return "recursive"


def decide_chunk_size(text: str) -> int:
    text_without_tables, _ = _split_out_tables(text)
    stripped = text_without_tables.strip()

    if len(stripped) > LARGE_DOCUMENT_MIN_LENGTH:
        return LARGE_DOCUMENT_CHUNK_SIZE

    return DEFAULT_CHUNK_SIZE


def chunk_text(
    text: str,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    if chunking_strategy:
        strategy = chunking_strategy
    else:
        strategy = decide_chunking_strategy(text)

    if strategy not in CHUNKING_STRATEGIES:
        raise ValueError(f"Unknown chunking_strategy {strategy!r} - choose one of {list(CHUNKING_STRATEGIES)}")

    text_without_tables, table_blocks = _split_out_tables(text)

    chunk_function = CHUNKING_STRATEGIES[strategy]
    if not text_without_tables:
        prose_chunks = []
    else:
        resolved_chunk_size = chunk_size if chunk_size is not None else decide_chunk_size(text)
        prose_chunks = chunk_function(text_without_tables, chunk_size=resolved_chunk_size, chunk_overlap=chunk_overlap)

    chunks = prose_chunks + table_blocks

    logger.info(
        "Split %d characters of text into %d chunks (strategy=%s, %d table block(s) kept whole)",
        len(text),
        len(chunks),
        strategy,
        len(table_blocks),
    )
    return chunks


def chunk_text_with_sections(
    text: str,
    chunking_strategy: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[list[str], list[str | None]]:
    if chunking_strategy != "document_structure":
        chunks = chunk_text(text, chunking_strategy, chunk_size, chunk_overlap)
        return chunks, [None] * len(chunks)

    text_without_tables, table_blocks = _split_out_tables(text)
    resolved_chunk_size = chunk_size if chunk_size is not None else decide_chunk_size(text)

    structured_chunks = chunk_by_document_structure(text_without_tables, resolved_chunk_size, chunk_overlap)
    chunks = [chunk["text"] for chunk in structured_chunks] + table_blocks
    section_ids: list[str | None] = [chunk["section_id"] for chunk in structured_chunks] + [None] * len(table_blocks)
    return chunks, section_ids
