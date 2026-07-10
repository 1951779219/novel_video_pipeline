"""爬虫基类与公共模型。"""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class NovelChapter:
    index: int
    title: str
    text: str


@dataclass
class Novel:
    site: str
    novel_id: str
    title: str
    author: str = ""
    description: str = ""
    chapters: list[NovelChapter] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(c.text for c in self.chapters)


@dataclass
class BookSearchResult:
    title: str
    ref: str
    author: str = ""
    source: str = ""
    content_type: str = ""
    paid_label: str = ""
    region: str = ""
    intro: str = ""
    latest_chapter: str = ""
    word_count: str = ""
    cover_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Scraper(Protocol):
    site_name: str

    def fetch(self, url_or_id: str, max_chars: int = 0) -> Novel: ...
    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]: ...
