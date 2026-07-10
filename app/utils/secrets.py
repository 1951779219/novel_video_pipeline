"""Small helpers for API-key hygiene and log redaction."""
from __future__ import annotations

import re
from typing import Any


def clean_api_key(value: Any) -> str:
    """Normalize keys copied from GUI fields, terminals, or notes."""
    return str(value or "").strip().strip('"').strip("'").strip()


def redact_secret_text(value: Any, limit: int | None = None) -> str:
    """Remove common API key shapes from user-visible logs."""
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1***", text)
    text = re.sub(r"(?i)(token\s+)[^\s\"']+", r"\1***", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", text)
    text = re.sub(r"AIza[0-9A-Za-z_-]{16,}", "AIza***", text)
    text = re.sub(r"AKIA[0-9A-Z]{16}", "AKIA***", text)
    text = re.sub(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+", r"\1***", text)
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text

