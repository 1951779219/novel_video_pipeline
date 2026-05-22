"""Small static Legado book-source adapter.

This is intentionally a conservative subset of Legado. It supports ordinary
JSON/CSS rules, relative URLs and simple template rules. JavaScript-heavy rules
remain out of scope here and should be handled by a source-specific adapter such
as qingtian.py.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from selectolax.parser import HTMLParser, Node

from .base import BookSearchResult, Novel, NovelChapter


def load_source(source_ref: str) -> dict[str, Any]:
    ref = (source_ref or "").strip()
    if not ref:
        raise ValueError("Legado 书源路径/URL 不能为空")
    if ref.startswith("http://") or ref.startswith("https://"):
        resp = httpx.get(ref, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    else:
        data = json.loads(Path(ref).read_text(encoding="utf-8"))
    if isinstance(data, list):
        if not data:
            raise ValueError("Legado 书源列表为空")
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Legado 书源格式不正确")
    return data


class LegadoStaticScraper:
    site_name = "legado_static"

    def __init__(self, source_ref: str, timeout: float = 30.0):
        self.source = load_source(source_ref)
        self.base_url = str(self.source.get("bookSourceUrl") or "").rstrip("/")
        self.name = str(self.source.get("bookSourceName") or self.base_url or "Legado")
        header = self._load_headers(self.source.get("header"))
        self._client = httpx.Client(headers=header, timeout=timeout, follow_redirects=True)

    def close(self):
        self._client.close()

    def search(self, keyword: str, limit: int = 20, page: int = 1) -> list[BookSearchResult]:
        search_url = str(self.source.get("searchUrl") or "")
        if _is_js(search_url):
            raise ValueError(f"{self.name} 是 JS 书源，通用静态解析器无法直接执行")
        url = self._render_url(search_url, keyword=keyword, page=page)
        body = self._get_text(url)
        data = self._parse_body(body)
        rule = self.source.get("ruleSearch") or {}
        items = self._extract_many(data, rule.get("bookList"))
        results: list[BookSearchResult] = []
        for item in items[: max(1, int(limit))]:
            title = self._extract_one(item, rule.get("name"))
            book_url = self._absolute(self._extract_one(item, rule.get("bookUrl")), url)
            results.append(
                BookSearchResult(
                    title=title,
                    author=self._extract_one(item, rule.get("author")),
                    source=self.name,
                    intro=self._extract_one(item, rule.get("intro")),
                    latest_chapter=self._extract_one(item, rule.get("lastChapter")),
                    word_count=self._extract_one(item, rule.get("wordCount")),
                    cover_url=self._absolute(self._extract_one(item, rule.get("coverUrl")), url),
                    ref=book_url,
                    raw={"book_url": book_url, "title": title},
                )
            )
        return [r for r in results if r.title and r.ref]

    def fetch(self, url_or_id: str, max_chars: int = 0) -> Novel:
        detail_url = self._absolute(url_or_id, self.base_url)
        detail_body = self._get_text(detail_url)
        detail = self._parse_body(detail_body)
        info_rule = self.source.get("ruleBookInfo") or {}
        toc_rule = self.source.get("ruleToc") or {}
        content_rule = self.source.get("ruleContent") or {}

        title = self._extract_one(detail, info_rule.get("name")) or detail_url
        author = self._extract_one(detail, info_rule.get("author"))
        intro = self._extract_one(detail, info_rule.get("intro"))
        toc_url = self._absolute(self._extract_one(detail, info_rule.get("tocUrl")) or detail_url, detail_url)

        toc_body = detail_body if toc_url == detail_url else self._get_text(toc_url)
        toc = self._parse_body(toc_body)
        chapter_nodes = self._extract_many(toc, toc_rule.get("chapterList"))
        novel = Novel(site=self.site_name, novel_id=detail_url, title=title, author=author, description=intro)
        total_chars = 0
        for index, ch in enumerate(chapter_nodes, start=1):
            chapter_title = self._extract_one(ch, toc_rule.get("chapterName")) or f"第{index}章"
            chapter_url = self._absolute(self._extract_one(ch, toc_rule.get("chapterUrl")), toc_url)
            if not chapter_url:
                continue
            text = self._fetch_chapter(chapter_url, content_rule)
            if not text:
                continue
            novel.chapters.append(NovelChapter(index=index, title=chapter_title, text=text))
            total_chars += len(text)
            if max_chars and total_chars >= max_chars:
                break
        return novel

    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]:
        return []

    def _fetch_chapter(self, url: str, content_rule: dict[str, Any]) -> str:
        body = self._get_text(url)
        data = self._parse_body(body)
        text = self._extract_one(data, content_rule.get("content"), html_mode=True)
        next_url = self._absolute(self._extract_one(data, content_rule.get("nextContentUrl")), url)
        # A small number of sources split one chapter across "next content" pages.
        seen = {url}
        while next_url and next_url not in seen:
            seen.add(next_url)
            body = self._get_text(next_url)
            data = self._parse_body(body)
            text += "\n" + self._extract_one(data, content_rule.get("content"), html_mode=True)
            next_url = self._absolute(self._extract_one(data, content_rule.get("nextContentUrl")), next_url)
        return self._clean_text(text)

    def _render_url(self, rule_url: str, keyword: str, page: int) -> str:
        url = rule_url.replace("{{key}}", quote(keyword)).replace("{{page}}", str(page))
        if "," in url and url.lstrip().startswith("http"):
            # Legado appends request options after a comma. Keep only the URL for this subset.
            url = url.split(",", 1)[0]
        return self._absolute(url, self.base_url)

    def _get_text(self, url: str) -> str:
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.text

    def _parse_body(self, body: str) -> Any:
        stripped = body.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(body)
            except Exception:
                pass
        return HTMLParser(body)

    def _extract_many(self, ctx: Any, rule: Any) -> list[Any]:
        if not rule:
            return []
        value = self._extract(ctx, str(rule), many=True)
        if isinstance(value, list):
            return value
        return [value] if value is not None else []

    def _extract_one(self, ctx: Any, rule: Any, html_mode: bool = False) -> str:
        if not rule:
            return ""
        if _is_js(str(rule)):
            return ""
        value = self._extract(ctx, str(rule), many=False, html_mode=html_mode)
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    def _extract(self, ctx: Any, rule: str, many: bool = False, html_mode: bool = False) -> Any:
        if "{{" in rule:
            return _render_template(rule, lambda path: self._extract(ctx, path, many=False))
        if isinstance(ctx, (dict, list)) or rule.startswith("$."):
            return _json_path(ctx, rule, many=many)
        return _html_rule(ctx, rule, many=many, html_mode=html_mode)

    def _absolute(self, url: str, base: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        return urljoin((base or self.base_url).rstrip("/") + "/", url)

    def _load_headers(self, raw: Any) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            )
        }
        if isinstance(raw, str) and raw.strip():
            try:
                headers.update(json.loads(raw))
            except Exception:
                pass
        elif isinstance(raw, dict):
            headers.update({str(k): str(v) for k, v in raw.items()})
        return headers

    def _clean_text(self, text: str) -> str:
        text = html.unescape(text or "")
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _is_js(rule: str) -> bool:
    return "<js>" in rule or rule.strip().startswith("@js") or "java.ajax" in rule


def _json_path(ctx: Any, path: str, many: bool = False) -> Any:
    path = path.strip()
    if path.startswith("$"):
        path = path[1:]
    path = path.lstrip(".")
    cur = ctx
    if not path:
        return cur
    for part in path.split("."):
        if not part:
            continue
        if isinstance(cur, list):
            if part.isdigit():
                cur = cur[int(part)] if int(part) < len(cur) else None
            else:
                cur = [x.get(part) for x in cur if isinstance(x, dict)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return [] if many else ""
    if many and not isinstance(cur, list):
        return [cur] if cur is not None else []
    return cur


def _html_rule(ctx: HTMLParser | Node, rule: str, many: bool = False, html_mode: bool = False) -> Any:
    current: list[Node]
    if isinstance(ctx, HTMLParser):
        root = ctx.root
    else:
        root = ctx
    current = [root]
    attr: str | None = None
    want_text = False
    want_html = html_mode

    parts = [p for p in rule.split("@") if p != ""]
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        if part == "text":
            want_text = True
            continue
        if part == "html":
            want_html = True
            continue
        if part in {"href", "src", "content", "value"}:
            attr = part
            continue
        selector = _legado_selector_to_css(part)
        next_nodes: list[Node] = []
        for node in current:
            next_nodes.extend(node.css(selector))
        current = next_nodes

    if many:
        return current
    if not current:
        return ""
    node = current[0]
    if attr:
        return node.attributes.get(attr, "")
    if want_html:
        return node.html or ""
    return node.text(strip=True) if want_text or not want_html else node.html


def _legado_selector_to_css(token: str) -> str:
    token = token.strip()
    m = re.match(r"^(id|class|tag)\.([A-Za-z0-9_\-:.#\[\]=]+)$", token)
    if m:
        kind, value = m.groups()
        if kind == "id":
            return f"#{value}"
        if kind == "class":
            return f".{value}"
        return value
    token = re.sub(r"\.(\d+)$", r":eq(\1)", token)
    return token


def _render_template(template: str, resolver) -> str:
    def repl(match: re.Match[str]) -> str:
        value = resolver(match.group(1).strip())
        return "" if value is None else str(value)

    return re.sub(r"\{\{(.+?)\}\}", repl, template)
