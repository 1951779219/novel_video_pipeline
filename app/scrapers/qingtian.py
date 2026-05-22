"""Qingtian/Fanqie aggregate book-source client.

The bundled Legado sources supplied by the user are JavaScript-heavy wrappers
around stable HTTP endpoints such as /search, /catalog and /content.  Running
the whole Legado JavaScript environment inside this pipeline would be brittle,
so this scraper implements the network contract directly and keeps the behavior
explicit.
"""
from __future__ import annotations

import base64
import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from .base import BookSearchResult, Novel, NovelChapter


DEFAULT_HOSTS = [
    "https://v1.gyks.cf",
    "https://v2.gyks.cf",
    "https://v3.gyks.cf",
    "https://v4.gyks.cf",
    "https://v5.gyks.cf",
    "https://v6.gyks.cf",
    "https://v7.gyks.cf",
    "http://219.154.201.122:5006",
    "https://api.langge.cf",
]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

MEDIA_PREFIX = {
    "x": "小说",
    "t": "听书",
    "m": "漫画",
    "d": "短剧",
}


@dataclass
class ParsedQuery:
    keyword: str
    media: str
    source: str


class QingtianAggregateScraper:
    site_name = "qingtian"

    def __init__(
        self,
        base_url: str = "",
        source: str = "番茄",
        media: str = "小说",
        timeout: float = 30.0,
        hosts: list[str] | None = None,
        delay: float = 0.15,
        retries: int = 2,
    ):
        host_list = [h.strip().rstrip("/") for h in (hosts or DEFAULT_HOSTS) if h.strip()]
        if base_url:
            base = base_url.strip().rstrip("/")
            host_list = [base] + [h for h in host_list if h != base]
        self.hosts = host_list
        self.base_url = host_list[0]
        self.source = source or "番茄"
        self.media = media or "小说"
        self.delay = max(0.0, float(delay))
        self.retries = max(1, int(retries))
        self._client = httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)

    def close(self):
        self._client.close()

    def search(
        self,
        keyword: str,
        limit: int = 20,
        page: int = 1,
        source: str | None = None,
        media: str | None = None,
        enrich_latest: bool = False,
    ) -> list[BookSearchResult]:
        parsed = self._parse_keyword(keyword, source=source, media=media)
        params = {
            "title": parsed.keyword,
            "tab": parsed.media,
            "source": parsed.source,
            "page": str(page),
            "disabled_sources": "0",
        }
        data = self._request_json("GET", "/search", params=params)
        items = data.get("data") or []
        results: list[BookSearchResult] = []
        for item in items[: max(1, int(limit))]:
            ref = self.make_ref(item, default_source=parsed.source, default_media=parsed.media)
            latest = self._latest_from_item(item)
            if enrich_latest and not latest:
                try:
                    latest = self._latest_from_catalog(item, default_source=parsed.source, default_media=parsed.media)
                except Exception:
                    latest = self._latest_update_from_item(item)
            results.append(
                BookSearchResult(
                    title=str(item.get("book_name") or item.get("name") or ""),
                    author=str(item.get("author") or ""),
                    source=str(item.get("source") or parsed.source),
                    intro=str(item.get("abstract") or item.get("intro") or ""),
                    latest_chapter=latest,
                    word_count=str(item.get("word_number") or item.get("wordCount") or ""),
                    cover_url=str(item.get("thumb_url") or item.get("coverUrl") or ""),
                    ref=ref,
                    raw=item,
                )
            )
        return results

    def _latest_from_item(self, item: dict[str, Any]) -> str:
        return str(
            item.get("last_chapter_title")
            or item.get("latest_chapter_title")
            or item.get("latest_chapter")
            or item.get("lastChapterTitle")
            or ""
        ).strip()

    def _latest_update_from_item(self, item: dict[str, Any]) -> str:
        updated = str(item.get("last_chapter_update_time") or item.get("update_time") or "").strip()
        return f"更新 {updated}" if updated else ""

    def _latest_from_catalog(self, item: dict[str, Any], default_source: str, default_media: str) -> str:
        book_id = str(item.get("book_id") or item.get("bookid") or item.get("id") or "")
        if not book_id:
            return self._latest_update_from_item(item)
        source = str(item.get("source") or default_source or self.source)
        media = str(item.get("tab") or default_media or self.media)
        chapters = self._fetch_catalog(book_id=book_id, source=source, media=media, detail_url=str(item.get("toc_url") or ""))
        for ch in reversed(chapters):
            if ch.get("is_volume") or ch.get("source") == "卷":
                continue
            title = str(ch.get("title") or "").strip()
            if title:
                updated = str(item.get("last_chapter_update_time") or "").strip()
                return f"{title}  {updated}" if updated else title
        return self._latest_update_from_item(item)

    def fetch(self, url_or_id: str, max_chars: int = 0, chapter_limit: int = 0) -> Novel:
        book = self._resolve_book(url_or_id)
        title = str(book.get("book_name") or book.get("title") or book.get("name") or book.get("book_id"))
        author = str(book.get("author") or "")
        source = str(book.get("source") or self.source)
        media = str(book.get("tab") or self.media)
        book_id = str(book.get("book_id") or book.get("bookid") or book.get("id") or "")
        if not book_id:
            raise ValueError(f"无法识别聚合书源 book_id: {url_or_id}")

        novel = Novel(
            site=self.site_name,
            novel_id=book_id,
            title=title,
            author=author,
            description=str(book.get("abstract") or book.get("intro") or ""),
        )
        chapters = self._fetch_catalog(book_id=book_id, source=source, media=media, detail_url=str(book.get("toc_url") or ""))
        total_chars = 0
        selected = chapters[: int(chapter_limit)] if chapter_limit else chapters
        for index, ch in enumerate(selected, start=1):
            if ch.get("is_volume") or ch.get("source") == "卷":
                continue
            text = self._fetch_content(ch, source=source, media=media)
            if not text:
                continue
            novel.chapters.append(
                NovelChapter(index=index, title=str(ch.get("title") or f"第{index}章"), text=text)
            )
            total_chars += len(text)
            if max_chars and total_chars >= max_chars:
                break
            if self.delay:
                time.sleep(self.delay)
        return novel

    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]:
        # The aggregate source does not expose a universal ranking endpoint.
        return []

    def make_ref(self, item: dict[str, Any], default_source: str = "", default_media: str = "") -> str:
        payload = {
            "book_id": item.get("book_id") or item.get("bookid") or item.get("id"),
            "book_name": item.get("book_name") or item.get("name") or item.get("title"),
            "author": item.get("author") or "",
            "source": item.get("source") or default_source or self.source,
            "tab": item.get("tab") or default_media or self.media,
            "abstract": item.get("abstract") or item.get("intro") or "",
            "toc_url": item.get("toc_url") or "",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return f"qingtian://{encoded}"

    def _parse_keyword(self, keyword: str, source: str | None = None, media: str | None = None) -> ParsedQuery:
        key = (keyword or "").strip()
        chosen_media = media or self.media
        chosen_source = source or self.source
        m = re.match(r"^([xtmd])[:：](.+)$", key, flags=re.I)
        if m:
            chosen_media = MEDIA_PREFIX.get(m.group(1).lower(), chosen_media)
            key = m.group(2).strip()
        if "@" in key:
            key, suffix_source = key.split("@", 1)
            chosen_source = suffix_source.strip() or chosen_source
        if not key:
            raise ValueError("搜索关键词不能为空")
        return ParsedQuery(keyword=key, media=chosen_media or "小说", source=chosen_source or "番茄")

    def _resolve_book(self, url_or_id: str) -> dict[str, Any]:
        value = (url_or_id or "").strip()
        if not value:
            raise ValueError("书籍引用不能为空")

        if value.startswith("qingtian://"):
            payload = value.split("://", 1)[1]
            return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))

        if value.startswith("data:;base64,"):
            payload = value.split(",", 1)[1].split(",", 1)[0]
            return json.loads(base64.b64decode(payload).decode("utf-8"))

        if value.startswith("{"):
            return json.loads(value)

        parsed = urlparse(value)
        qs = parse_qs(parsed.query)
        if qs.get("bookid") or qs.get("book_id"):
            return {
                "book_id": (qs.get("bookid") or qs.get("book_id") or [""])[0],
                "source": (qs.get("source") or [self.source])[0],
                "tab": (qs.get("tab") or [self.media])[0],
                "book_name": (qs.get("name") or [""])[0],
            }

        if re.match(r"^[A-Za-z0-9_\-=]{12,}$", value):
            return {"book_id": value, "source": self.source, "tab": self.media, "book_name": value}

        results = self.search(value, limit=1)
        if not results:
            raise ValueError(f"聚合书源未搜索到: {value}")
        return results[0].raw

    def _fetch_catalog(self, book_id: str, source: str, media: str, detail_url: str = "") -> list[dict[str, Any]]:
        params = {
            "book_id": book_id,
            "source": source,
            "tab": media,
            "variable": json.dumps({"custom": ""}, ensure_ascii=False),
        }
        body = {"html": ""}
        if detail_url:
            body["url"] = detail_url
        data = self._request_json("POST", "/catalog", params=params, json_body=body)
        return data.get("data") or []

    def _fetch_content(self, chapter: dict[str, Any], source: str, media: str) -> str:
        content_url = chapter.get("content_url")
        data: dict[str, Any]
        if content_url:
            path = str(content_url)
            if path.startswith("http://") or path.startswith("https://"):
                data = self._request_json("GET", path)
            else:
                data = self._request_json("GET", path)
        else:
            payload = {
                "html": "",
                "item_id": chapter.get("item_id"),
                "source": chapter.get("source") or source,
                "tab": chapter.get("tab") or media,
                "tone_id": "4",
                "variable": json.dumps({"custom": ""}, ensure_ascii=False),
                "version": "4.11.5.1",
            }
            data = self._request_json("POST", "/content", json_body=payload)
        content = data.get("content") if isinstance(data, dict) else ""
        return self._clean_content(str(content or ""))

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for host in self.hosts:
            url = path_or_url if path_or_url.startswith("http") else urljoin(host + "/", path_or_url.lstrip("/"))
            for _ in range(self.retries):
                try:
                    if method.upper() == "POST":
                        resp = self._client.post(url, params=params, json=json_body)
                    else:
                        resp = self._client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") not in (None, 0, "0"):
                        raise RuntimeError(str(data.get("msg") or data))
                    self.base_url = host
                    return data
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.2)
        raise RuntimeError(f"聚合书源请求失败: {last_error}")

    def _clean_content(self, content: str) -> str:
        content = html.unescape(content)
        if "<" in content and ">" in content:
            tree = HTMLParser(content)
            paras = [p.text(strip=True) for p in tree.css("p") if p.text(strip=True)]
            if paras:
                content = "\n".join(paras)
            else:
                content = tree.body.text(separator="\n", strip=True) if tree.body else tree.text(separator="\n")
        content = re.sub(r"<comment\b[^>]*?/?>", "", content)
        content = re.sub(r"<img\b[^>]*?/?>", "", content)
        content = re.sub(r"\r\n?", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = re.sub(r"[ \t]+\n", "\n", content)
        return content.strip()


def parse_host_list(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[\n,;]+", raw)
    return [p.strip().rstrip("/") for p in parts if p.strip()]
