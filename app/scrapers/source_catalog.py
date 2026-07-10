"""Integrated novel source catalog and search facade."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import config

from .base import BookSearchResult, Novel
from .kakuyomu import KakuyomuScraper
from .qingtian import QingtianAggregateScraper, parse_host_list
from .syosetu import SyosetuScraper


CATALOG_PATH = Path(__file__).with_name("source_catalog.default.json")
MODE_FANQIE = "fanqie"
MODE_JAPANESE = "japanese"
MODE_LABELS = {
    MODE_FANQIE: "番茄小说",
    MODE_JAPANESE: "日本文库",
}
MODE_ALIASES = {
    "fanqie": MODE_FANQIE,
    "番茄": MODE_FANQIE,
    "番茄小说": MODE_FANQIE,
    "cn": MODE_FANQIE,
    "china": MODE_FANQIE,
    "japanese": MODE_JAPANESE,
    "日本": MODE_JAPANESE,
    "日本文库": MODE_JAPANESE,
    "jp": MODE_JAPANESE,
    "ja": MODE_JAPANESE,
}


@dataclass(frozen=True)
class SourceEntry:
    key: str
    name: str
    region: str
    content_type: str
    url: str
    engine: str
    paid_label: str
    intro: str = ""
    notes: str = ""


def normalize_mode(value: str | None) -> str:
    text = str(value or "").strip()
    return MODE_ALIASES.get(text, MODE_FANQIE)


def load_catalog() -> list[SourceEntry]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return [SourceEntry(**item) for item in raw]


def catalog_for_mode(mode: str | None) -> list[SourceEntry]:
    chosen = normalize_mode(mode)
    return [entry for entry in load_catalog() if entry.region == chosen]


def catalog_summary() -> dict[str, list[dict[str, str]]]:
    summary: dict[str, list[dict[str, str]]] = {MODE_FANQIE: [], MODE_JAPANESE: []}
    for entry in load_catalog():
        summary.setdefault(entry.region, []).append(
            {
                "name": entry.name,
                "content_type": entry.content_type,
                "paid_label": entry.paid_label,
                "url": entry.url,
                "engine": entry.engine,
            }
        )
    return summary


def _first_entry(key: str) -> SourceEntry:
    for entry in load_catalog():
        if entry.key == key:
            return entry
    raise ValueError(f"来源目录缺少 {key}")


def _compact_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(x).strip() for x in value if str(x).strip())
    return str(value or "").strip()


def _qingtian_content_type(item: dict[str, Any], fallback: str) -> str:
    category = _compact_list(item.get("category"))
    tags = _compact_list(item.get("tags"))
    if category and tags:
        return f"{category} / {tags}"
    return category or tags or fallback


def _annotate(result: BookSearchResult, entry: SourceEntry, content_type: str | None = None) -> BookSearchResult:
    result.region = entry.region
    result.content_type = content_type or result.content_type or entry.content_type
    result.paid_label = result.paid_label or entry.paid_label
    raw = dict(result.raw or {})
    raw.setdefault("catalog_source", entry.name)
    raw.setdefault("catalog_url", entry.url)
    raw.setdefault("content_type", result.content_type)
    raw.setdefault("paid_label", result.paid_label)
    result.raw = raw
    return result


class SourceCatalogScraper:
    """Search the selected source group while preserving crawlable refs."""

    site_name = "source_catalog"

    def __init__(self, timeout: float = 12.0):
        self.timeout = max(3.0, float(timeout or 12.0))
        self._children: list[Any] = []

    def close(self):
        for child in self._children:
            close = getattr(child, "close", None)
            if close:
                close()
        self._children.clear()

    def search(
        self,
        keyword: str,
        limit: int = 20,
        source: str | None = None,
        media: str | None = None,
        enrich_latest: bool = False,
    ) -> list[BookSearchResult]:
        mode = normalize_mode(source)
        if mode == MODE_JAPANESE:
            return self._search_japanese(keyword, limit=limit)
        return self._search_fanqie(keyword, limit=limit, media=media, enrich_latest=enrich_latest)

    def fetch(self, url_or_id: str, max_chars: int = 0) -> Novel:
        value = str(url_or_id or "").strip()
        lowered = value.lower()
        if lowered.startswith("qingtian://"):
            sc = self._make_qingtian()
            return sc.fetch(value, max_chars=max_chars, chapter_limit=int(config.scraper_chapter_limit))
        if _looks_like_syosetu_ref(value):
            sc = SyosetuScraper(timeout=self.timeout)
            self._children.append(sc)
            return sc.fetch(value, max_chars=max_chars)
        if _looks_like_kakuyomu_ref(value):
            sc = KakuyomuScraper(timeout=self.timeout)
            self._children.append(sc)
            return sc.fetch(value, max_chars=max_chars)
        raise ValueError("该来源目录项当前只支持搜索入口或需要登录/付费，请使用可公开抓取的作品 URL。")

    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]:
        return []

    def _make_qingtian(self) -> QingtianAggregateScraper:
        sc = QingtianAggregateScraper(
            base_url=config.source_base_url,
            source="番茄",
            media=config.source_media or "小说",
            hosts=parse_host_list(config.source_hosts),
            delay=float(config.source_delay),
            timeout=self.timeout,
        )
        self._children.append(sc)
        return sc

    def _search_fanqie(
        self,
        keyword: str,
        limit: int,
        media: str | None,
        enrich_latest: bool,
    ) -> list[BookSearchResult]:
        entry = _first_entry("fanqie")
        sc = self._make_qingtian()
        results = sc.search(
            keyword,
            limit=limit,
            source="番茄",
            media=media or "小说",
            enrich_latest=enrich_latest,
        )
        return [
            _annotate(result, entry, content_type=_qingtian_content_type(result.raw, entry.content_type))
            for result in results
        ]

    def _search_japanese(self, keyword: str, limit: int) -> list[BookSearchResult]:
        per_site_timeout = min(self.timeout, 8.0)
        overall_timeout = per_site_timeout + 1.0

        def run_one(key: str) -> list[BookSearchResult]:
            entry = _first_entry(key)
            sc = SyosetuScraper(timeout=per_site_timeout) if key == "syosetu" else KakuyomuScraper(timeout=per_site_timeout)
            try:
                return [_annotate(result, entry) for result in sc.search(keyword, limit=limit)]
            except Exception:
                return []
            finally:
                close = getattr(sc, "close", None)
                if close:
                    close()

        keys = ["syosetu", "kakuyomu"]
        groups_by_key: dict[str, list[BookSearchResult]] = {key: [] for key in keys}
        executor = ThreadPoolExecutor(max_workers=len(keys), thread_name_prefix="jp_source_search")
        futures = {executor.submit(run_one, key): key for key in keys}
        try:
            for future in as_completed(futures, timeout=overall_timeout):
                groups_by_key[futures[future]] = future.result()
        except TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        merged: list[BookSearchResult] = []
        groups = [groups_by_key[key] for key in keys]
        max_len = max((len(group) for group in groups), default=0)
        for idx in range(max_len):
            for group in groups:
                if idx < len(group):
                    merged.append(group[idx])
                    if len(merged) >= max(1, int(limit or 20)):
                        return merged
        return merged


def _looks_like_syosetu_ref(value: str) -> bool:
    lowered = value.lower().strip()
    return bool(
        re.match(r"^https?://ncode\.syosetu\.com/n[0-9a-z]+/?(?:\d+/?)?$", lowered)
        or re.match(r"^n[0-9a-z]+$", lowered)
    )


def _looks_like_kakuyomu_ref(value: str) -> bool:
    lowered = value.lower().strip()
    return bool(
        re.match(r"^https?://kakuyomu\.jp/works/\d+(?:/episodes/\d+)?/?$", lowered)
        or re.match(r"^\d+(?:/\d+)?$", lowered)
    )
