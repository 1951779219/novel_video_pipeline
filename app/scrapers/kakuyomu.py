"""カクヨム public search and reader scraper."""
from __future__ import annotations

import re
import time
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from .base import BookSearchResult, Novel, NovelChapter


BASE_URL = "https://kakuyomu.jp"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}


class KakuyomuScraper:
    site_name = "kakuyomu"

    def __init__(self, timeout: float = 30.0, delay: float = 0.25):
        self.delay = max(0.0, delay)
        self._client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)

    def close(self):
        self._client.close()

    def search(self, keyword: str, limit: int = 20) -> list[BookSearchResult]:
        key = (keyword or "").strip()
        if not key:
            raise ValueError("搜索关键词不能为空")
        html = self._get(f"{BASE_URL}/search?q={quote(key)}")
        tree = HTMLParser(html)
        seen: set[str] = set()
        results: list[BookSearchResult] = []
        for a in tree.css("a"):
            href = a.attributes.get("href", "")
            if not re.match(r"^/works/\d+$", href):
                continue
            title = a.text(strip=True)
            if not title or title.startswith("★") or href in seen:
                continue
            seen.add(href)
            results.append(
                BookSearchResult(
                    title=title,
                    ref=f"{BASE_URL}{href}",
                    source="カクヨム",
                    content_type="カクヨム / 原创网络小说",
                    paid_label="免费",
                    region="japanese",
                    raw={"book_url": f"{BASE_URL}{href}", "title": title},
                )
            )
            if len(results) >= max(1, int(limit or 20)):
                break
        return results

    def fetch(self, url_or_id: str, max_chars: int = 0) -> Novel:
        work_id, episode_id = self._parse_ref(url_or_id)
        work_url = f"{BASE_URL}/works/{work_id}"
        if episode_id:
            return self._fetch_single_episode(work_id, episode_id, max_chars=max_chars)

        html = self._get(work_url)
        tree = HTMLParser(html)
        title_el = tree.css_first("h1")
        title = title_el.text(strip=True) if title_el else work_id
        author_el = tree.css_first('a[href^="/users/"]')
        author = author_el.text(strip=True) if author_el else ""
        desc = self._first_text(tree, ['p[class*="introduction"]', 'div[class*="Introduction"]', "main p"])

        episode_links: list[str] = []
        seen: set[str] = set()
        for href in re.findall(rf"/works/{work_id}/episodes/(\d+)", html):
            if href in seen:
                continue
            seen.add(href)
            episode_links.append(href)

        novel = Novel(site=self.site_name, novel_id=work_id, title=title, author=author, description=desc)
        total = 0
        for index, ep_id in enumerate(episode_links, start=1):
            chapter = self._fetch_episode(work_id, ep_id, index=index)
            if chapter.text:
                novel.chapters.append(chapter)
                total += len(chapter.text)
            if max_chars and total >= max_chars:
                break
            if self.delay:
                time.sleep(self.delay)
        return novel

    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]:
        return []

    def _fetch_single_episode(self, work_id: str, episode_id: str, max_chars: int = 0) -> Novel:
        chapter = self._fetch_episode(work_id, episode_id, index=1)
        novel = Novel(site=self.site_name, novel_id=work_id, title=chapter.title)
        if max_chars and max_chars > 0:
            chapter.text = chapter.text[: int(max_chars)]
        if chapter.text:
            novel.chapters.append(chapter)
        return novel

    def _fetch_episode(self, work_id: str, episode_id: str, index: int) -> NovelChapter:
        html = self._get(f"{BASE_URL}/works/{work_id}/episodes/{episode_id}")
        tree = HTMLParser(html)
        title_el = tree.css_first(".widget-episodeTitle, h2")
        title = title_el.text(strip=True) if title_el else f"第{index}話"
        body = tree.css_first(".widget-episodeBody, div[class*=EpisodeBody], div[class*=episodeBody]")
        text = ""
        if body:
            paras = body.css("p")
            if paras:
                text = "\n".join(p.text(strip=False).strip() for p in paras if p.text(strip=True))
            else:
                text = body.text(strip=False).strip()
        return NovelChapter(index=index, title=title, text=text)

    def _parse_ref(self, value: str) -> tuple[str, str | None]:
        text = str(value or "").strip()
        m = re.search(r"kakuyomu\.jp/works/(\d+)(?:/episodes/(\d+))?", text)
        if m:
            return m.group(1), m.group(2)
        m = re.match(r"^(\d+)(?:/(\d+))?$", text)
        if m:
            return m.group(1), m.group(2)
        raise ValueError(f"无法识别的 Kakuyomu URL/ID: {value}")

    def _get(self, url: str) -> str:
        r = self._client.get(url)
        r.raise_for_status()
        return r.text

    def _first_text(self, tree: HTMLParser, selectors: list[str]) -> str:
        for sel in selectors:
            node = tree.css_first(sel)
            if node:
                text = node.text(strip=True)
                if text:
                    return text
        return ""
