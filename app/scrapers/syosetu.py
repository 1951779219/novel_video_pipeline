"""小説家になろう (syosetu.com) 爬虫。

支持：
- 单本小说 URL  https://ncode.syosetu.com/n5728kt/
- 单章 URL      https://ncode.syosetu.com/n5728kt/1/
- 排行榜 (走 syosetu API): https://api.syosetu.com/rank/rankget/
"""
import re
import time
import httpx
from selectolax.parser import HTMLParser

from .base import BookSearchResult, Novel, NovelChapter

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}


class SyosetuScraper:
    site_name = "syosetu"

    def __init__(self, timeout: float = 30.0):
        self._client = httpx.Client(headers=_HEADERS, timeout=timeout, follow_redirects=True)

    def close(self):
        self._client.close()

    # ─────────────────────────────────────────────
    # 单本/单章
    # ─────────────────────────────────────────────
    def fetch(self, url_or_id: str, max_chars: int = 0) -> Novel:
        ncode, chapter_no = self._parse_url(url_or_id)
        if chapter_no is not None:
            return self._fetch_single_chapter(ncode, chapter_no)
        return self._fetch_full(ncode, max_chars=max_chars)

    def _parse_url(self, s: str):
        s = s.strip()
        m = re.match(r"https?://ncode\.syosetu\.com/(n[0-9a-zA-Z]+)/?(?:(\d+)/?)?", s)
        if m:
            return m.group(1), int(m.group(2)) if m.group(2) else None
        if re.match(r"^n[0-9a-zA-Z]+$", s):
            return s, None
        raise ValueError(f"无法识别的 syosetu URL/ID: {s}")

    def _fetch_full(self, ncode: str, max_chars: int = 0) -> Novel:
        index_url = f"https://ncode.syosetu.com/{ncode}/"
        html = self._get(index_url)
        tree = HTMLParser(html)

        title_el = tree.css_first("h1.p-novel__title, .p-novel__title, h1")
        title = title_el.text(strip=True) if title_el else ncode
        author_el = tree.css_first(".p-novel__author, .novel_writername")
        author = author_el.text(strip=True).replace("作者：", "").strip() if author_el else ""
        desc_el = tree.css_first("#novel_ex, .p-novel__summary")
        description = desc_el.text(strip=True) if desc_el else ""

        # 收集章节链接（短篇：无目录则直接抓正文）
        chapter_links = []
        for a in tree.css("a.p-eplist__subtitle, .index_box dd.subtitle a"):
            href = a.attributes.get("href", "")
            m = re.search(rf"/{ncode}/(\d+)/?", href)
            if m:
                chapter_links.append(int(m.group(1)))

        novel = Novel(site=self.site_name, novel_id=ncode, title=title,
                      author=author, description=description)

        if not chapter_links:
            # 短篇
            text = self._extract_chapter_text(tree)
            if text:
                novel.chapters.append(NovelChapter(index=1, title=title, text=text))
            return novel

        total = 0
        for no in chapter_links:
            ch = self._fetch_single_chapter(ncode, no)
            if ch.chapters:
                novel.chapters.append(ch.chapters[0])
                total += len(ch.chapters[0].text)
                if max_chars and total >= max_chars:
                    break
            time.sleep(0.5)
        return novel

    def _fetch_single_chapter(self, ncode: str, chapter_no: int) -> Novel:
        url = f"https://ncode.syosetu.com/{ncode}/{chapter_no}/"
        html = self._get(url)
        tree = HTMLParser(html)

        novel_title_el = tree.css_first(".p-novel__title--rensai, h1.p-novel__title, h1")
        novel_title = novel_title_el.text(strip=True) if novel_title_el else ncode
        sub_el = tree.css_first(".p-novel__title--secondary, .p-novel__subtitle, .novel_subtitle")
        sub_title = sub_el.text(strip=True) if sub_el else f"第{chapter_no}話"

        text = self._extract_chapter_text(tree)

        novel = Novel(site=self.site_name, novel_id=ncode, title=novel_title)
        if text:
            novel.chapters.append(NovelChapter(index=chapter_no, title=sub_title, text=text))
        return novel

    def _extract_chapter_text(self, tree: HTMLParser) -> str:
        # 新版结构 .p-novel__body .js-novel-text，老版 #novel_honbun
        body = tree.css_first(".p-novel__body, #novel_honbun, .js-novel-text")
        if body is None:
            for sel in (".p-novel__text", "#novel_view"):
                body = tree.css_first(sel)
                if body:
                    break
        if body is None:
            return ""
        # 把段落 <p> 用换行连接
        paras = body.css("p")
        if paras:
            return "\n".join(p.text(strip=False) for p in paras)
        return body.text(strip=False)

    def _get(self, url: str) -> str:
        r = self._client.get(url)
        r.raise_for_status()
        return r.text

    # ─────────────────────────────────────────────
    # 关键词搜索（官方 novelapi）
    # ─────────────────────────────────────────────
    def search(self, keyword: str, limit: int = 20) -> list[BookSearchResult]:
        key = (keyword or "").strip()
        if not key:
            raise ValueError("搜索关键词不能为空")
        params = {
            "out": "json",
            "word": key,
            "lim": str(max(1, min(int(limit or 20), 50))),
            "of": "t-n-w-s-gp-gl-nu",
        }
        r = self._client.get("https://api.syosetu.com/novelapi/api/", params=params)
        r.raise_for_status()
        data = r.json()
        results: list[BookSearchResult] = []
        for item in data[1:]:
            if not isinstance(item, dict):
                continue
            ncode = str(item.get("ncode") or "").lower().strip()
            title = str(item.get("title") or "").strip()
            if not ncode or not title:
                continue
            genre = str(item.get("genre") or "").strip()
            points = str(item.get("global_point") or "").strip()
            updated = str(item.get("novelupdated_at") or "").strip()
            latest = f"更新 {updated}" if updated else ""
            if points:
                latest = f"{latest} / {points}pt" if latest else f"{points}pt"
            results.append(
                BookSearchResult(
                    title=title,
                    author=str(item.get("writer") or ""),
                    source="小説家になろう",
                    content_type=f"ジャンル {genre}" if genre else "原创网络小说",
                    paid_label="免费",
                    region="japanese",
                    intro=str(item.get("story") or ""),
                    latest_chapter=latest,
                    word_count=str(item.get("length") or ""),
                    ref=f"https://ncode.syosetu.com/{ncode}/",
                    raw=item,
                )
            )
        return results

    # ─────────────────────────────────────────────
    # 排行榜（官方 API，免认证）
    # ─────────────────────────────────────────────
    def fetch_ranking(self, ranking_type: str = "daily", limit: int = 10) -> list[dict]:
        """
        ranking_type: daily / weekly / monthly / quarter / yearly / total
        返回 [{ncode, title, url}]
        """
        from datetime import datetime
        rt_map = {
            "daily": "d", "weekly": "w", "monthly": "m",
            "quarter": "q", "yearly": "y", "total": "t",
        }
        suffix = rt_map.get(ranking_type, "d")
        # 取本周一作为基准（API 要求当周/月起始日）
        today = datetime.now()
        if suffix == "d":
            date_str = today.strftime("%Y%m%d")
        elif suffix == "w":
            mon = today - __import__("datetime").timedelta(days=today.weekday())
            date_str = mon.strftime("%Y%m%d")
        else:
            date_str = today.replace(day=1).strftime("%Y%m%d")
        url = f"https://api.syosetu.com/rank/rankget/?rtype={date_str}-{suffix}&out=json"
        r = self._client.get(url)
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data[:limit]:
            ncode = item.get("ncode", "").lower()
            results.append({
                "ncode": ncode,
                "rank": item.get("rank"),
                "pt": item.get("pt"),
                "url": f"https://ncode.syosetu.com/{ncode}/",
            })
        # 拉取每本的标题（rankget 不返回标题，需要走 novelapi）
        if results:
            ncodes = "-".join(r["ncode"] for r in results)
            api = f"https://api.syosetu.com/novelapi/api/?ncode={ncodes}&out=json&of=t-n-w"
            try:
                r2 = self._client.get(api).json()
                # r2[0] 是 allcount，[1:] 是数据
                meta = {x["ncode"].lower(): x for x in r2[1:] if isinstance(x, dict) and "ncode" in x}
                for item in results:
                    info = meta.get(item["ncode"], {})
                    item["title"] = info.get("title", item["ncode"])
                    item["author"] = info.get("writer", "")
            except Exception:
                for item in results:
                    item["title"] = item["ncode"]
                    item["author"] = ""
        return results
