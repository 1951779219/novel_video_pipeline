"""Text cleaning and segmentation for multilingual narration."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_RUBY_RE = re.compile(r"[|｜]?([^《》\n]{1,40})《[^》]+》")
_BRACKET_NOTE_RE = re.compile(r"[\[［【](?:本章|作者|求票|推荐|月票|订阅|打赏|PS|ps).*?[\]］】]", re.S)
_FULL_SPACE_HEAD_RE = re.compile(r"^[\u3000\s]+", re.MULTILINE)
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])\s*|(?<=\.)\s+(?=[A-Z\"“])")


@dataclass
class Segment:
    index: int
    text: str


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RUBY_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _BRACKET_NOTE_RE.sub("", text)
    text = text.replace("\u3000", " ")
    text = _FULL_SPACE_HEAD_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def split_segments(text: str, target_min: int = 80, target_max: int = 150) -> list[Segment]:
    text = clean_text(text)
    segments: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        pieces = [p.strip() for p in _SENTENCE_SPLIT_RE.split(para) if p and p.strip()]
        _pack_sentences(pieces or [para], segments, target_min=target_min, target_max=target_max)
    return [Segment(index=i, text=s) for i, s in enumerate(segments) if s.strip()]


def _pack_sentences(
    pieces: list[str],
    out: list[str],
    target_min: int,
    target_max: int,
):
    buf = ""
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > target_max:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_hard_split(piece, target_max))
            continue
        candidate = buf + piece if _is_cjk(buf + piece) else (buf + " " + piece).strip()
        if not buf:
            buf = piece
        elif len(candidate) <= target_max:
            buf = candidate
        else:
            out.append(buf)
            buf = piece
        if len(buf) >= target_min and _ends_sentence(buf):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)


def _hard_split(text: str, size: int) -> list[str]:
    chunks = []
    current = ""
    for ch in text:
        current += ch
        if len(current) >= size and (ch in "，,、 " or len(current) >= size + 20):
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _ends_sentence(text: str) -> bool:
    return text[-1:] in "。！？!?；;…."


def _is_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))
