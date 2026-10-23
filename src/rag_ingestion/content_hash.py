"""Ported from hrb_chatbot_service's common/utils/content_hash.py, unchanged."""

import hashlib


def compute_content_hash(content: bytes) -> str:
    """SHA-256 of raw file bytes - the dedup key used by find_by_content_hash()/create_document()."""
    return hashlib.sha256(content).hexdigest()
