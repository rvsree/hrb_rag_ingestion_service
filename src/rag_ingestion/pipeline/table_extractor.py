"""Table-aware PDF parsing via pdfplumber - ported unmodified from
hrb_chatbot_service's ai/doc_processing/tables/table_extractor.py, import paths adjusted only."""

import logging

import pdfplumber

from hrb_common.observability.logger import get_logger

logger = get_logger("pipeline.tables")

# pdfminer warns "Could not get FontBBox..." on some real PDFs - a known, harmless quirk, silenced here.
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def extract_tables_from_pdf(file_path: str) -> list[str]:
    """Return one markdown-table string per table found - never raises, an unparseable page is skipped."""
    tables_as_text = []

    with pdfplumber.open(file_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            try:
                page_tables = page.extract_tables()
            except Exception as error:
                logger.warning("Table extraction failed on page %d: %s: %s", page_number, type(error).__name__, error)
                continue

            for table in page_tables:
                formatted = _format_table_as_markdown(table)
                if formatted:
                    tables_as_text.append(formatted)

    logger.info("Extracted %d table(s) from %s", len(tables_as_text), file_path)
    return tables_as_text


def _format_table_as_markdown(table: list[list[str | None]]) -> str | None:
    """Turn pdfplumber's row/cell grid into a markdown table - embeds better than a flattened wall of numbers."""
    rows = [row for row in table if any(cell not in (None, "") for cell in row)]
    if not rows:
        return None

    def clean_row(row: list[str | None]) -> list[str]:
        return [(cell or "").replace("\n", " ").strip() for cell in row]

    header, *body_rows = [clean_row(row) for row in rows]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for row in body_rows:
        lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines)
