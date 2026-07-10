"""Helpers for YouTube ad-suitability questionnaire templates."""
from __future__ import annotations

import json
from typing import Any


def normalize_ad_suitability_template(template: Any = None) -> dict:
    """Return a safe questionnaire template.

    Shape used by the upload script:
      {"default": 1, "questions": {"question keyword": 2}}
    Choice numbering is 1-based. 0 means skip/leave default.
    """
    if isinstance(template, str):
        text = template.strip()
        if not text:
            template = {}
        else:
            try:
                template = json.loads(text)
            except Exception:
                template = {}
    if not isinstance(template, dict):
        template = {}

    try:
        default = int(template.get("default", 1) or 1)
    except Exception:
        default = 1
    questions = template.get("questions")
    if not isinstance(questions, dict):
        questions = {}

    normalized_questions = {}
    for key, value in questions.items():
        text_key = str(key or "").strip()
        if not text_key:
            continue
        try:
            normalized_questions[text_key] = int(value)
        except Exception:
            continue

    return {"default": default, "questions": normalized_questions}
