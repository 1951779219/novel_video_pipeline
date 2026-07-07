"""Pipeline orchestration.

Stages write durable artifacts into data/jobs/{job_id}.  Re-running the same
job can reuse existing audio/images, which matters for very long videos.
"""
from __future__ import annotations

import json
import hashlib
import html
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.config import JOBS_DIR, ROOT, config
from app.concurrency import external_api_slot, ffmpeg_slot, pid_alive
from app.scrapers.base import BookSearchResult, Novel, NovelChapter
from app.scrapers.legado import LegadoStaticScraper
from app.scrapers.qingtian import QingtianAggregateScraper, parse_host_list
from app.scrapers.syosetu import SyosetuScraper
from app.stages.stage2_clean import Segment, split_segments
from app.stages.stage6_compose import build_ass, build_srt, build_video, concat_audios
from app.stages.stage_pacing import ImagePlan, plan_images
from app.backends.image import ImageBackend
from app.backends.llm import LLMBackend
from app.backends.tts import TTSBackend, _audio_duration
from app.character_analysis import (
    DEFAULT_CHARACTER_ANALYSIS_PROMPT,
    analyze_characters,
    can_call_analysis_llm,
    character_context_for_text,
    character_reference_prompt,
    visual_theme_context,
)
from app.utils.ffmpeg import ffmpeg_path


LogFn = Callable[[str], None]
ProgFn = Callable[[float], None]


def _parallel_limit(config_key: str, default: int, total: int, hard_cap: int = 16) -> int:
    try:
        raw = int(config.get(config_key, default) or default)
    except Exception:
        raw = default
    return max(1, min(int(total or 1), hard_cap, raw))


def _bounded_timeout(value, default: float, minimum: float = 30.0, maximum: float = 1800.0) -> float:
    try:
        seconds = float(value if value not in (None, "") else default)
    except Exception:
        seconds = float(default)
    return max(minimum, min(maximum, seconds))


def _noop(*_args, **_kwargs):
    pass


_JOB_ID_LOCK = threading.Lock()
_JOB_ID_LAST_STAMP = ""
_JOB_ID_SEQUENCE = 0


def new_job_id(prefix: str = "job") -> str:
    global _JOB_ID_LAST_STAMP, _JOB_ID_SEQUENCE

    with _JOB_ID_LOCK:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        if stamp == _JOB_ID_LAST_STAMP:
            _JOB_ID_SEQUENCE += 1
        else:
            _JOB_ID_LAST_STAMP = stamp
            _JOB_ID_SEQUENCE = 0

        suffix = "" if _JOB_ID_SEQUENCE == 0 else f"_{_JOB_ID_SEQUENCE:03d}"
        candidate = f"{prefix}_{stamp}{suffix}"
        while job_dir_for(candidate).exists():
            _JOB_ID_SEQUENCE += 1
            candidate = f"{prefix}_{stamp}_{_JOB_ID_SEQUENCE:03d}"
        return candidate


def job_dir_for(job_id: str) -> Path:
    return JOBS_DIR / job_id


def append_log(job_dir: Path, message: str):
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / "log.txt").open("a", encoding="utf-8") as f:
        f.write(str(message).rstrip() + "\n")


def tail_log(job_id: str, lines: int = 240) -> str:
    path = job_dir_for(job_id) / "log.txt"
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(data[-lines:])


def write_status(job_dir: Path, **updates):
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "status.json"
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(updates)
    current["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def load_status(job_id: str) -> dict:
    path = job_dir_for(job_id) / "status.json"
    if not path.exists():
        return {"job_id": job_id, "stage": "missing", "progress": 0}
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        status["worker_alive"] = pid_alive(status.get("worker_pid"))
        status["stage_display"] = stage_display(status)
        return status
    except Exception as exc:
        return {"job_id": job_id, "stage": "broken", "progress": 0, "error": str(exc)}


_STAGE_LABELS = {
    "queued": "等待启动",
    "starting": "启动中",
    "scrape": "1/6 读取文本",
    "clean": "2/6 清洗切片",
    "tts": "3/6 配音",
    "images_prefetch": "4/6 预生成图片",
    "pacing": "4/6 规划画面",
    "images": "4/6 生成图片",
    "cover": "5/6 生成封面",
    "compose": "5/6 合成视频",
    "upload": "6/6 上传",
    "completed": "完成",
    "failed": "失败",
    "stopping": "停止中",
    "stopped": "已停止",
    "stop_failed": "停止失败",
    "missing": "目录缺失",
    "broken": "状态损坏",
}


def stage_display(status: dict) -> str:
    stage = str(status.get("stage") or "")
    label = _STAGE_LABELS.get(stage, stage or "未知")
    try:
        progress = max(0.0, min(1.0, float(status.get("progress") or 0)))
    except Exception:
        progress = 0.0
    if stage == "upload" and status.get("upload_progress") is not None:
        try:
            upload_progress = max(0.0, min(1.0, float(status.get("upload_progress") or 0)))
            return f"{label} {upload_progress * 100:.0f}%"
        except Exception:
            pass
    return f"{label} {progress * 100:.0f}%"


def list_jobs(limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    if not JOBS_DIR.exists():
        return rows
    for d in sorted([p for p in JOBS_DIR.iterdir() if p.is_dir()], reverse=True):
        st = load_status(d.name)
        rows.append(
            {
                "job_id": d.name,
                "stage": st.get("stage", ""),
                "stage_display": stage_display(st),
                "progress": st.get("progress", 0),
                "title": st.get("title", ""),
                "video": st.get("video", ""),
                "worker_pid": st.get("worker_pid"),
                "worker_alive": st.get("worker_alive", False),
                "updated_at": st.get("updated_at", ""),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def is_worker_running(job_id: str) -> bool:
    return bool(load_status(job_id).get("worker_alive"))


def count_running_workers() -> int:
    """Count how many worker processes are currently alive across all jobs."""
    if not JOBS_DIR.exists():
        return 0
    count = 0
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        st = load_status(d.name)
        if st.get("worker_alive"):
            count += 1
    return count


def _safe_job_path(job_id: str) -> Path:
    root = JOBS_DIR.resolve()
    path = job_dir_for(job_id).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"unsafe job path: {path}")
    return path


def _safe_artifact_name(value: str, fallback: str = "artifact") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text[:80].rstrip(" ._") or fallback)


def _terminate_process_tree(pid: int, *, timeout_seconds: float = 8.0) -> bool:
    if not pid_alive(pid):
        return True
    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
        return proc.returncode == 0 and not pid_alive(pid)

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return not pid_alive(pid)


def stop_job(job_id: str) -> bool:
    path = _safe_job_path(job_id)
    st = load_status(job_id)
    pid = st.get("worker_pid")
    try:
        pid_int = int(pid or 0)
    except Exception:
        pid_int = 0

    if not path.exists():
        return False
    if not pid_int or not pid_alive(pid_int):
        write_status(
            path,
            stage="stopped" if st.get("stage") not in {"completed", "failed"} else st.get("stage"),
            worker_pid=None,
            stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        append_log(path, "stop requested: worker was not running")
        return False

    append_log(path, f"stop requested: terminating worker pid={pid_int}")
    write_status(path, stage="stopping", worker_pid=pid_int, stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    ok = _terminate_process_tree(pid_int)
    if not ok:
        write_status(path, stage="stop_failed", worker_pid=pid_int, error=f"无法停止 worker pid={pid_int}")
        append_log(path, f"stop failed: worker pid={pid_int} is still alive")
        raise RuntimeError(f"{job_id} 停止失败，worker pid={pid_int} 仍在运行")
    write_status(path, stage="stopped", worker_pid=None, stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    append_log(path, f"worker pid={pid_int} stopped by user")
    return True


def delete_job(job_id: str, *, stop_running: bool = False) -> bool:
    if is_worker_running(job_id):
        if not stop_running:
            raise RuntimeError(f"{job_id} 仍在运行，不能删除")
        stop_job(job_id)
    path = _safe_job_path(job_id)
    if not path.exists():
        return False
    last_error: Exception | None = None
    for _ in range(8):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error:
        raise last_error
    return True


def clear_job_media_cache(job_id: str) -> None:
    """Clear generated media so rerun will call image/cover/compose stages again."""
    if is_worker_running(job_id):
        raise RuntimeError(f"{job_id} 仍在运行，不能清理缓存")
    path = _safe_job_path(job_id)
    if not path.exists():
        return
    for name in ("images", "cover", "_clips"):
        target = path / name
        if target.exists():
            shutil.rmtree(target)
    for name in (
        "prompts.json",
        "compose_manifest.json",
        "final.mp4",
        "final.tmp.mp4",
        "audio_full.mp3",
        "subtitle.ass",
        "subtitle.srt",
        "result.json",
        "upload_result.json",
    ):
        (path / name).unlink(missing_ok=True)
    write_status(path, stage="queued", progress=0.0, video="", youtube_url="", cover="")
    append_log(path, "media cache cleared: images/cover/final video will regenerate")


def start_worker(input_text: str, job_id: str | None = None, resume: bool = False) -> tuple[str, int]:
    """Start a detached worker process and return (job_id, pid)."""
    job_id = job_id or new_job_id("pipeline")
    job_dir = job_dir_for(job_id)
    if job_dir.exists() and not resume:
        raise FileExistsError(f"job directory already exists: {job_dir}")

    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    running = count_running_workers()
    if running >= max_jobs:
        raise RuntimeError(
            f"已达到最大并行任务数限制 ({running}/{max_jobs})，请等待当前任务完成后再启动新任务。"
            "可在配置中调整 max_concurrent_jobs 提高上限。"
        )
    job_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-B",
        "-m",
        "app.worker",
        "--job-id",
        job_id,
        "--input",
        input_text,
    ]
    if resume:
        command.append("--resume")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    env.setdefault("no_proxy", "localhost,127.0.0.1,::1")

    kwargs = {}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if bool(config.worker_detached):
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    elif bool(config.worker_detached):
        kwargs["start_new_session"] = True

    worker_log = job_dir / "worker.log"
    append_log(job_dir, f"dispatch worker: {' '.join(command)}")
    write_status(job_dir, job_id=job_id, input=input_text, stage="queued", progress=0.0, worker_pid=None)
    with worker_log.open("a", encoding="utf-8", buffering=1) as log_handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=False,
            **kwargs,
        )
    write_status(job_dir, job_id=job_id, input=input_text, stage="queued", progress=0.0, worker_pid=process.pid)
    append_log(job_dir, f"worker pid={process.pid}")
    return job_id, int(process.pid)


def start_next_queued_job(*, exclude_job_id: str | None = None, on_log: LogFn = _noop) -> tuple[str, int] | None:
    """Start the next waiting job when worker capacity is available."""
    max_jobs = max(1, int(config.get("max_concurrent_jobs", 2)))
    if count_running_workers() >= max_jobs:
        return None
    for row in list_jobs(limit=500):
        job_id = str(row.get("job_id") or "")
        if not job_id or job_id == exclude_job_id:
            continue
        st = load_status(job_id)
        if st.get("worker_alive") or st.get("stage") not in {"queued", "stopped"}:
            continue
        input_text = str(st.get("input") or "")
        if not input_text:
            continue
        try:
            started = start_worker(input_text, job_id=job_id, resume=True)
            on_log(f"  auto-started waiting job {job_id} pid={started[1]}")
            return started
        except RuntimeError:
            return None
        except Exception as exc:
            append_log(job_dir_for(job_id), f"auto-start failed: {exc}")
            on_log(f"  WARN auto-start waiting job {job_id} failed: {exc}")
            return None
    return None


def _build_scraper(site: str):
    if site == "syosetu":
        return SyosetuScraper()
    if site in {"qingtian", "fanqie", "aggregate"}:
        return QingtianAggregateScraper(
            base_url=config.source_base_url,
            source=config.source_platform,
            media=config.source_media,
            hosts=parse_host_list(config.source_hosts),
            delay=float(config.source_delay),
        )
    if site in {"legado", "legado_static"}:
        return LegadoStaticScraper(config.legado_source_ref)
    raise ValueError(f"暂不支持 site={site}")


def search_books(
    keyword: str,
    site: str | None = None,
    source: str | None = None,
    media: str | None = None,
    limit: int = 20,
    enrich_latest: bool = False,
) -> list[BookSearchResult]:
    site = site or config.scraper_site
    sc = _build_scraper(site)
    try:
        if isinstance(sc, QingtianAggregateScraper):
            return sc.search(keyword, limit=limit, source=source, media=media, enrich_latest=enrich_latest)
        if isinstance(sc, LegadoStaticScraper):
            return sc.search(keyword, limit=limit)
        raise ValueError(f"{site} 暂不支持关键词搜索，请直接输入 URL/ID")
    finally:
        sc.close()


def _looks_like_text_source(value: str, site: str) -> bool:
    text = str(value or "")
    if site in {"text", "local_text", "manual"}:
        return True
    stripped = text.strip()
    lowered = stripped.lower()
    if re.match(r"^[a-z][a-z0-9+.-]*://", lowered) or lowered.startswith("data:;base64,"):
        return False
    if lowered.startswith(("http:", "https:", "qingtian:", "legado:", "syosetu:")):
        return False
    if stripped.startswith("{") and ("book_id" in stripped or "bookid" in stripped):
        return False
    if "\n" in text or len(text) > 500:
        return True
    try:
        return Path(text.strip().strip('"')).exists()
    except OSError:
        return False


def _novel_from_text_source(value: str, max_chars: int = 0) -> Novel:
    raw = str(value or "")
    source = raw.strip().strip('"')
    path: Path | None = None
    try:
        candidate = Path(source)
        if candidate.exists() and candidate.is_file():
            path = candidate
    except OSError:
        path = None
    if path is not None:
        text = path.read_text(encoding="utf-8", errors="ignore")
        title = path.stem
        novel_id = str(path)
    else:
        text = raw
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", first_line[:60]).strip(" .") or "manual_text"
        novel_id = "manual"
    if max_chars and max_chars > 0:
        text = text[: int(max_chars)]
    text = text.strip()
    return Novel(
        site="text",
        novel_id=novel_id,
        title=title,
        author="",
        description="local text input",
        chapters=[NovelChapter(index=1, title=title, text=text)] if text else [],
    )


def stage_scrape(
    url_or_id: str,
    site: str = "syosetu",
    max_chars: int = 0,
    on_log: LogFn = _noop,
) -> Novel:
    def warn_if_limited(novel: Novel) -> None:
        try:
            limit = int(max_chars or 0)
        except Exception:
            limit = 0
        if limit > 0 and len(novel.full_text) >= limit:
            on_log(f"  WARN text reached scraper_max_chars={limit}; set it to 0 to process the full text")

    if _looks_like_text_source(url_or_id, site):
        novel = _novel_from_text_source(url_or_id, max_chars=max_chars)
        on_log(f"[1/6] Local text: {novel.title}")
        on_log(f"  OK title={novel.title} chars={len(novel.full_text)}")
        warn_if_limited(novel)
        if not novel.full_text.strip():
            raise RuntimeError("local text is empty")
        return novel
    on_log(f"[1/6] 抓取 {site}: {url_or_id}")
    sc = _build_scraper(site)
    try:
        if isinstance(sc, QingtianAggregateScraper):
            novel = sc.fetch(url_or_id, max_chars=max_chars, chapter_limit=int(config.scraper_chapter_limit))
        else:
            novel = sc.fetch(url_or_id, max_chars=max_chars)
    finally:
        sc.close()
    on_log(f"  OK 标题: {novel.title}  章节数: {len(novel.chapters)}  字数: {len(novel.full_text)}")
    if not novel.chapters or not novel.full_text.strip():
        raise RuntimeError("抓取成功但没有正文内容")
    return novel


def stage_clean(novel: Novel, on_log: LogFn = _noop, job_dir: Path | None = None) -> list[Segment]:
    on_log("[2/6] 清洗 + 切片")
    text = _strip_story_scaffold(novel.full_text, novel, on_log=on_log, job_dir=job_dir)
    text = _clean_non_story_text(text, on_log=on_log, job_dir=job_dir)
    segs = split_segments(text)
    on_log(f"  OK 切成 {len(segs)} 段")
    if not segs:
        raise RuntimeError("正文切片结果为空")
    return segs


_CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[\d零〇一二三四五六七八九十百千万两]+[章节章回话話卷集部篇].{0,40}|"
    r"chapter\s*\d+.{0,40}|"
    r"(?:序章|楔子|引子|尾声|番外).{0,30}"
    r")\s*$",
    re.I,
)
_INTRO_HEADING_RE = re.compile(r"^\s*(?:简介|内容简介|小说简介|作品简介|书籍简介|文案|作者|作者名|书名|小说名)\s*[:：]", re.I)


def _strip_story_scaffold(text: str, novel: Novel, on_log: LogFn = _noop, job_dir: Path | None = None) -> str:
    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip():
        return source
    title_values = {
        re.sub(r"\s+", "", str(value or ""))
        for value in (novel.title, _clean_display_title(novel.title))
        if str(value or "").strip()
    }
    author_value = re.sub(r"\s+", "", str(novel.author or ""))
    description_value = re.sub(r"\s+", "", str(novel.description or ""))
    cleaned: list[str] = []
    removed_leading = 0
    removed_chapters = 0
    leading_window_open = True
    for raw_line in source.split("\n"):
        stripped = raw_line.strip()
        compact = re.sub(r"\s+", "", stripped)
        if leading_window_open and len(cleaned) < 24:
            remove_leading = False
            if not stripped:
                remove_leading = removed_leading > 0
            elif compact in title_values:
                remove_leading = True
            elif author_value and (compact == author_value or compact in {f"作者:{author_value}", f"作者：{author_value}"}):
                remove_leading = True
            elif description_value and compact and compact in description_value and len(compact) <= 120:
                remove_leading = True
            elif _INTRO_HEADING_RE.search(stripped):
                remove_leading = True
            if remove_leading:
                removed_leading += 1
                continue
            if stripped:
                leading_window_open = False
        if stripped and _CHAPTER_HEADING_RE.match(stripped):
            removed_chapters += 1
            continue
        cleaned.append(raw_line)
    result = "\n".join(cleaned).strip()
    if not result:
        return source
    if removed_leading or removed_chapters:
        if job_dir is not None:
            _write_json(
                job_dir / "story_scaffold_clean_report.json",
                {
                    "removed_leading_lines": removed_leading,
                    "removed_chapter_headings": removed_chapters,
                    "chars_before": len(source),
                    "chars_after": len(result),
                },
            )
        on_log(f"  story scaffold clean: removed {removed_leading} leading lines and {removed_chapters} chapter headings")
    return result


_NON_STORY_PATTERNS = [
    r"作者(有话说|的话|ps|PS|留言|声明)",
    r"^(PS|P\.S\.|ps)[:：]",
    r"请假|断更|补更|更新通知|更新时间|恢复更新",
    r"上架(感言|通知|说明)|架感言|首订|订阅|求首订",
    r"求(月票|推荐票|收藏|追读|打赏|订阅|五星|好评|催更)",
    r"感谢.*?(打赏|月票|推荐票|盟主|白银|黄金|支持)",
    r"(月票|推荐票|打赏|催更|收藏|追读).{0,12}(加更|名单|感谢|支持)",
    r"读者群|书友群|QQ群|q群|V群|微信|公众号",
    r"免责声明|版权声明|本章说|章节感言|章节说明|作品相关",
    r"人物(设定|介绍|小传)|角色(设定|介绍)|人设|设定集|资料卡",
]
_NON_STORY_RE = re.compile("|".join(f"(?:{p})" for p in _NON_STORY_PATTERNS), re.I)


def _clean_non_story_text(text: str, on_log: LogFn = _noop, job_dir: Path | None = None) -> str:
    source = str(text or "")
    if not bool(config.get("ai_text_clean_enabled", False)):
        return source
    paragraphs = _split_clean_paragraphs(source)
    if not paragraphs:
        return source

    removed: set[int] = set()
    for i, para in enumerate(paragraphs):
        if _looks_non_story_paragraph(para):
            removed.add(i)

    rule_removed = len(removed)
    ai_removed: set[int] = set()
    if config.llm_api_key or config.llm_provider == "ollama":
        try:
            ai_removed = _ai_classify_non_story_paragraphs(paragraphs, removed, on_log)
            removed.update(ai_removed)
        except Exception as exc:
            on_log(f"  WARN AI 扫文清理失败，保留规则清理结果: {exc}")
    else:
        on_log("  AI 扫文清理已开启，但未配置 LLM API Key；仅使用规则清理")

    cleaned_paragraphs = [p for i, p in enumerate(paragraphs) if i not in removed]
    cleaned = "\n\n".join(cleaned_paragraphs).strip()
    if not cleaned:
        on_log("  WARN 扫文清理结果为空，已回退原文")
        return source

    report = {
        "enabled": True,
        "paragraphs": len(paragraphs),
        "removed_total": len(removed),
        "removed_by_rules": rule_removed,
        "removed_by_ai": len(ai_removed),
        "removed_indices": sorted(removed)[:1000],
        "chars_before": len(source),
        "chars_after": len(cleaned),
    }
    if job_dir is not None:
        _write_json(job_dir / "text_clean_report.json", report)
        (job_dir / "text_cleaned.txt").write_text(cleaned, encoding="utf-8")
    on_log(
        f"  AI扫文清理: 段落 {len(paragraphs)} -> {len(cleaned_paragraphs)}，"
        f"删除 {len(removed)} 段，字数 {len(source)} -> {len(cleaned)}"
    )
    return cleaned


def _split_clean_paragraphs(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.split(r"\n\s*\n|(?<=。)\s*(?=作者有话说|作者的话|PS[:：]|ps[:：])", normalized)
    paragraphs = []
    for para in raw:
        para = re.sub(r"[ \t]+\n", "\n", para).strip()
        if para:
            paragraphs.append(para)
    return paragraphs


def _looks_non_story_paragraph(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return True
    if len(compact) > 420:
        return False
    if _NON_STORY_RE.search(compact):
        dialogue_marks = compact.count("“") + compact.count("”") + compact.count('"')
        story_punc = sum(compact.count(ch) for ch in "。！？")
        return dialogue_marks <= 2 or story_punc <= 4
    return False


def _ai_classify_non_story_paragraphs(paragraphs: list[str], already_removed: set[int], on_log: LogFn) -> set[int]:
    batch_chars = max(1200, int(config.get("ai_text_clean_batch_chars", 6000) or 6000))
    llm = LLMBackend(
        provider=config.llm_provider,
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
        system_prompt=str(config.get("ai_text_clean_prompt", "") or ""),
        style_suffix="",
    )
    removed: set[int] = set()
    batch: list[tuple[int, str]] = []
    size = 0

    def flush(current: list[tuple[int, str]]) -> None:
        if not current:
            return
        request = "请判断以下编号段落中哪些应删除，只返回 JSON。\n\n" + "\n\n".join(
            f"[{idx}] {para[:1200]}" for idx, para in current
        )
        with external_api_slot(action="ai text clean"):
            reply = llm.storyboard(request)
        for idx in _parse_remove_indices(reply):
            if any(idx == item[0] for item in current):
                removed.add(idx)

    for idx, para in enumerate(paragraphs):
        if idx in already_removed:
            continue
        projected = size + len(para)
        if batch and projected > batch_chars:
            flush(batch)
            on_log(f"  AI扫文批次完成，累计 AI 删除 {len(removed)} 段")
            batch = []
            size = 0
        batch.append((idx, para))
        size += len(para)
    flush(batch)
    return removed


def _parse_remove_indices(reply: str) -> set[int]:
    text = str(reply or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            raw = data.get("remove", [])
            return {int(x) for x in raw if str(x).strip().lstrip("-").isdigit() and int(x) >= 0}
        except Exception:
            pass
    nums = re.findall(r"\d+", text)
    return {int(x) for x in nums}


def _stable_hash(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _file_signature(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {}
    return {"size": int(st.st_size), "mtime": round(float(st.st_mtime), 3)}


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tts_config_hash() -> str:
    return _stable_hash(
        {
            "provider": config.tts_provider,
            "voice": config.tts_voice,
            "rate": config.tts_rate,
            "volume": config.get("tts_volume", 1.0),
            "model": config.get("tts_model", "tts-1"),
            "emotion": config.get("tts_emotion", ""),
            "base_url": config.tts_base_url,
            "extra": config.tts_extra or {},
            "api_key_hash": _text_hash(config.tts_api_key or "")[:16],
        }
    )


def _load_tts_manifest(path: Path) -> dict:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("segments"), list):
        data["segments"] = []
    data["schema_version"] = 1
    return data


def _set_tts_manifest_entry(manifest_path: Path, manifest: dict, entry: dict) -> None:
    segments = manifest.setdefault("segments", [])
    index = int(entry.get("index", -1))
    for pos, current in enumerate(segments):
        if int(current.get("index", -2)) == index:
            segments[pos] = entry
            break
    else:
        segments.append(entry)
    segments.sort(key=lambda item: int(item.get("index", 0)))
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(manifest_path, manifest)


def _tts_manifest_entry(manifest: dict, index: int) -> dict | None:
    for entry in manifest.get("segments") or []:
        if int(entry.get("index", -1)) == int(index):
            return entry
    return None


def _tts_cache_matches(entry: dict | None, expected: dict, out: Path) -> bool:
    if not entry or not out.exists():
        return False
    if str(entry.get("status")) not in {"ready", "fallback_silence"}:
        return False
    for key in ("text_hash", "config_hash", "provider", "voice", "rate"):
        if str(entry.get(key, "")) != str(expected.get(key, "")):
            return False
    if str(entry.get("volume", "1.0")) != str(expected.get("volume", "1.0")):
        return False
    sig = _file_signature(out)
    if not sig:
        return False
    if int(entry.get("file_size") or -1) != sig["size"]:
        return False
    try:
        if abs(float(entry.get("file_mtime") or 0) - float(sig["mtime"])) > 1.0:
            return False
    except Exception:
        return False
    return float(entry.get("duration") or 0) > 0.05


def _ready_tts_entry(index: int, seg: Segment, out: Path, duration: float, expected: dict, status: str = "ready") -> dict:
    sig = _file_signature(out)
    return {
        "index": index,
        "status": status,
        "path": str(out),
        "duration": round(float(duration), 3),
        "text_hash": expected["text_hash"],
        "config_hash": expected["config_hash"],
        "provider": expected["provider"],
        "voice": expected["voice"],
        "rate": expected["rate"],
        "volume": expected.get("volume", 1.0),
        "chars": len(seg.text),
        "file_size": sig.get("size", 0),
        "file_mtime": sig.get("mtime", 0),
    }


def _stage_tts_manifest_impl(
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn,
    on_prog: ProgFn,
) -> tuple[list[Path], list[float]]:
    workers = _parallel_limit("max_parallel_tts", 2, len(segments), hard_cap=12)
    segment_timeout_seconds = _bounded_timeout(config.get("tts_segment_timeout_seconds", 180), 180.0)
    on_log(
        f"[3/6] TTS provider={config.tts_provider} voice={config.tts_voice} "
        f"volume={config.get('tts_volume', 1.0)} parallel={workers} timeout={segment_timeout_seconds:.0f}s"
    )

    def make_tts() -> TTSBackend:
        return TTSBackend(
            provider=config.tts_provider,
            voice=config.tts_voice,
            rate=config.tts_rate,
            api_key=config.tts_api_key,
            base_url=config.tts_base_url,
            extra={
                **(config.tts_extra or {}),
                "emotion": config.tts_emotion,
                "model": config.get("tts_model", "tts-1"),
                "timeout_seconds": segment_timeout_seconds,
            },
            volume=config.get("tts_volume", 1.0),
        )

    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = audio_dir / "tts_manifest.json"
    manifest = _load_tts_manifest(manifest_path)
    manifest_lock = threading.Lock()
    manifest.update(
        {
            "provider": config.tts_provider,
            "voice": config.tts_voice,
            "rate": config.tts_rate,
            "volume": config.get("tts_volume", 1.0),
            "config_hash": _tts_config_hash(),
            "total": len(segments),
            "parallel": workers,
        }
    )
    _write_json(manifest_path, manifest)

    audios: list[Path | None] = [None] * len(segments)
    durations: list[float] = [0.0] * len(segments)
    retries = max(1, int(config.tts_retries))
    try:
        heartbeat_seconds = float(config.get("tts_heartbeat_seconds", 30) or 30)
    except Exception:
        heartbeat_seconds = 30.0
    heartbeat_seconds = max(10.0, min(300.0, heartbeat_seconds))
    running_lock = threading.Lock()
    running_segments: dict[int, dict[str, float | int]] = {}

    def set_manifest(entry: dict) -> None:
        with manifest_lock:
            _set_tts_manifest_entry(manifest_path, manifest, entry)

    def mark_running(index: int, attempt: int) -> None:
        with running_lock:
            running_segments[index] = {"attempt": attempt, "started": time.monotonic()}

    def clear_running(index: int) -> None:
        with running_lock:
            running_segments.pop(index, None)

    def running_summary() -> str:
        with running_lock:
            items = sorted(running_segments.items())
        if not items:
            return "waiting for an API slot or finishing cached audio"
        parts = []
        now = time.monotonic()
        for index, info in items[:4]:
            elapsed = max(0.0, now - float(info.get("started") or now))
            parts.append(f"seg{index} attempt {int(info.get('attempt') or 1)} {elapsed:.0f}s")
        extra = "" if len(items) <= 4 else f", +{len(items) - 4} more"
        return ", ".join(parts) + extra

    def process_segment(i: int, seg: Segment) -> tuple[int, Path, float]:
        out = audio_dir / f"seg_{i:05d}.mp3"
        expected = {
            "text_hash": _text_hash(seg.text),
            "config_hash": manifest["config_hash"],
            "provider": config.tts_provider,
            "voice": config.tts_voice,
            "rate": config.tts_rate,
            "volume": config.get("tts_volume", 1.0),
        }
        with manifest_lock:
            entry = _tts_manifest_entry(manifest, i)
        d = 0.0
        if _tts_cache_matches(entry, expected, out):
            d = float(entry.get("duration") or 0.0)
        elif out.exists():
            d = _audio_duration(out)
            if d > 0.05:
                set_manifest(_ready_tts_entry(i, seg, out, d, expected))

        if d <= 0.05:
            tts = make_tts()
            for attempt in range(1, retries + 1):
                tmp = out.with_suffix(".tmp.mp3")
                tmp.unlink(missing_ok=True)
                try:
                    set_manifest(
                        {
                            **expected,
                            "index": i,
                            "status": "waiting_slot",
                            "path": str(out),
                            "attempt": attempt,
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                    try:
                        with external_api_slot(
                            action=f"tts {i + 1}/{len(segments)}",
                            timeout_seconds=segment_timeout_seconds,
                        ):
                            set_manifest(
                                {
                                    **expected,
                                    "index": i,
                                    "status": "running",
                                    "path": str(out),
                                    "attempt": attempt,
                                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                },
                            )
                            mark_running(i, attempt)
                            d = tts.synth(seg.text, tmp)
                        if d <= 0.05:
                            raise RuntimeError("audio duration is zero")
                        tmp.replace(out)
                        set_manifest(_ready_tts_entry(i, seg, out, d, expected))
                        break
                    finally:
                        clear_running(i)
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    set_manifest(
                        {
                            **expected,
                            "index": i,
                            "status": "failed",
                            "path": str(out),
                            "attempt": attempt,
                            "error": str(exc),
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                    on_log(f"  WARN seg{i} TTS failed {attempt}/{retries}: {exc}")
                    if attempt < retries:
                        on_log(f"    TTS seg{i} retrying ({attempt + 1}/{retries})...")
                        time.sleep(min(8, attempt * 1.5))
            if d <= 0.05 and config.tts_fail_silence:
                d = _estimated_speech_duration(seg.text)
                _create_silence(out, d)
                set_manifest(_ready_tts_entry(i, seg, out, d, expected, status="fallback_silence"))
                on_log(f"  WARN seg{i} used {d:.1f}s silent placeholder")
            elif d <= 0.05:
                raise RuntimeError(f"seg{i} TTS failed after retries")

        return i, out, d

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts") as pool:
        pending = {pool.submit(process_segment, i, seg) for i, seg in enumerate(segments)}
        while pending:
            done, pending = wait(pending, timeout=heartbeat_seconds, return_when=FIRST_COMPLETED)
            if not done:
                on_log(f"  TTS still running: {running_summary()} ({completed}/{len(segments)} done)")
                on_prog(completed / max(1, len(segments)))
                continue
            for future in done:
                i, out, d = future.result()
                audios[i] = out
                durations[i] = d
                completed += 1
                _write_json(job_dir / "durations.json", durations)
                on_log(f"  TTS progress {completed}/{len(segments)}: seg{i} {d:.1f}s, total {sum(durations):.1f}s")
                on_prog(completed / max(1, len(segments)))

    final_audios = [p for p in audios if p is not None]
    if len(final_audios) != len(segments):
        raise RuntimeError("TTS did not produce all audio segments")
    on_log(f"  OK {len(final_audios)} audio segments, total {sum(durations):.1f}s")
    return final_audios, durations


def stage_tts(
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
) -> tuple[list[Path], list[float]]:
    return _stage_tts_manifest_impl(segments, job_dir, on_log, on_prog)
    on_log(f"[3/6] TTS: provider={config.tts_provider} voice={config.tts_voice}")
    tts = TTSBackend(
        provider=config.tts_provider,
        voice=config.tts_voice,
        rate=config.tts_rate,
        api_key=config.tts_api_key,
        base_url=config.tts_base_url,
        extra=config.tts_extra,
        volume=config.get("tts_volume", 1.0),
    )
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audios: list[Path] = []
    durations: list[float] = []
    retries = max(1, int(config.tts_retries))

    for i, seg in enumerate(segments):
        out = audio_dir / f"seg_{i:05d}.mp3"
        d = _audio_duration(out) if out.exists() else 0.0
        if d <= 0.05:
            for attempt in range(1, retries + 1):
                tmp = out.with_suffix(".tmp.mp3")
                tmp.unlink(missing_ok=True)
                try:
                    d = tts.synth(seg.text, tmp)
                    if d <= 0.05:
                        raise RuntimeError("音频时长为 0")
                    tmp.replace(out)
                    break
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    on_log(f"  WARN seg{i} TTS 失败 {attempt}/{retries}: {exc}")
                    if attempt < retries:
                        time.sleep(min(8, attempt * 1.5))
            if d <= 0.05 and config.tts_fail_silence:
                d = _estimated_speech_duration(seg.text)
                _create_silence(out, d)
                on_log(f"  WARN seg{i} 已用 {d:.1f}s 静音占位，字幕时间轴保持连续")
            elif d <= 0.05:
                raise RuntimeError(f"seg{i} TTS 多次失败")

        audios.append(out)
        durations.append(d)
        if (i + 1) % 5 == 0 or i == len(segments) - 1:
            _write_json(job_dir / "durations.json", durations)
            on_log(f"  [{i + 1}/{len(segments)}] 累计 {sum(durations):.1f}s")
            on_prog((i + 1) / max(1, len(segments)))
    on_log(f"  OK {len(audios)} 段音频，总时长 {sum(durations):.1f}s")
    return audios, durations


def stage_pacing(
    segments: list[Segment],
    durations: list[float],
    on_log: LogFn = _noop,
) -> list[ImagePlan]:
    on_log(f"[4/6] 图音配比: mode={config.pacing_mode}")
    plans = plan_images(
        segments,
        durations,
        mode=config.pacing_mode,
        seconds_per_image=config.pacing_seconds_per_image,
        sentences_per_image=config.pacing_sentences_per_image,
        fixed_count=config.pacing_fixed_count,
    )
    avg = sum(p.duration for p in plans) / max(1, len(plans))
    on_log(f"  OK 需要 {len(plans)} 张图，平均 {avg:.1f}s/张")
    return plans


def stage_character_analysis(
    novel: Novel,
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> dict | None:
    if not bool(config.get("character_analysis_enabled", False)):
        return None
    out = job_dir / "character_profiles.json"
    if out.exists():
        try:
            cached = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("enabled"):
                on_log(f"  character analysis reuse cached profiles: {out.name}")
                return cached
        except Exception:
            pass

    if not can_call_analysis_llm(config.llm_provider, config.llm_api_key):
        payload = {"enabled": False, "reason": "LLM API key is empty and provider is not ollama/custom"}
        _write_json(out, payload)
        on_log("  WARN character analysis skipped: no usable LLM credentials")
        return payload

    on_log("[4/6] character analysis: extracting protagonists, supporting roles, and visual locks")
    llm = LLMBackend(
        provider=config.llm_provider,
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
        system_prompt=config.llm_storyboard_prompt,
        style_suffix="",
    )
    try:
        payload = analyze_characters(
            novel,
            segments,
            llm,
            max_chars=int(config.get("character_analysis_max_chars", 12000) or 12000),
            max_tokens=int(config.get("character_analysis_max_tokens", 1800) or 1800),
            system_prompt=str(config.get("character_analysis_prompt", "") or DEFAULT_CHARACTER_ANALYSIS_PROMPT),
        )
    except Exception as exc:
        payload = {"enabled": False, "reason": f"character analysis failed: {exc}"}
        _write_json(out, payload)
        on_log(f"  WARN character analysis failed: {exc}")
        return payload

    _write_json(out, payload)
    characters = payload.get("characters") if isinstance(payload, dict) else []
    on_log(f"  OK character profiles: {len(characters) if isinstance(characters, list) else 0} characters -> {out.name}")
    return payload


def _image_route_settings(role: str) -> dict:
    role = str(role or "scene")
    if role == "scene":
        return {
            "provider": str(config.image_provider or "placeholder"),
            "base_url": str(config.image_base_url or ""),
            "api_key": str(config.image_api_key or ""),
            "model": str(config.image_model or ""),
            "workflow_path": str(config.image_workflow or ""),
        }

    if role == "cover":
        provider = str(config.get("cover_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        return {
            "provider": provider,
            "base_url": str(config.get("cover_base_url", "") or config.image_base_url or ""),
            "api_key": str(config.get("cover_api_key", "") or config.image_api_key or ""),
            "model": str(config.get("cover_model", "") or config.image_model or ""),
            "workflow_path": str(config.image_workflow or ""),
        }

    if role == "character_reference":
        provider = str(config.get("character_reference_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        return {
            "provider": provider,
            "base_url": str(config.get("character_reference_base_url", "") or config.image_base_url or ""),
            "api_key": str(config.get("character_reference_api_key", "") or config.image_api_key or ""),
            "model": str(config.get("character_reference_model", "") or config.image_model or ""),
            "workflow_path": str(config.get("character_reference_workflow", "") or config.image_workflow or ""),
        }

    if role == "scene_reference":
        provider = str(config.get("scene_reference_provider", "same_as_image") or "").strip()
        if provider in {"", "same_as_image", "同配图", "同图片"}:
            return _image_route_settings("scene")
        return {
            "provider": provider,
            "base_url": str(config.get("scene_reference_base_url", "") or config.image_base_url or ""),
            "api_key": str(config.get("scene_reference_api_key", "") or config.image_api_key or ""),
            "model": str(config.get("scene_reference_model", "") or config.image_model or ""),
            "workflow_path": str(config.get("scene_reference_workflow", "") or config.image_workflow or ""),
        }

    return _image_route_settings("scene")


def _make_image_backend(route: dict, *, provider: str | None = None) -> ImageBackend:
    return ImageBackend(
        provider=provider or route["provider"],
        base_url=route.get("base_url", ""),
        api_key=route.get("api_key", ""),
        model=route.get("model", ""),
        steps=config.image_steps,
        cfg=config.image_cfg,
        workflow_path=route.get("workflow_path", ""),
        timeout_seconds=config.get("image_api_timeout_seconds", 300),
    )


def _reference_images_for_text(analysis: dict | None, text: str, *, max_count: int = 2) -> list[Path]:
    if not analysis or not analysis.get("enabled"):
        return []
    characters = analysis.get("characters")
    if not isinstance(characters, list):
        return []
    haystack = str(text or "")
    protagonist_names = set(str(x).strip() for x in (analysis.get("protagonists") or []) if str(x).strip())
    matched: list[dict] = []
    for item in characters:
        if not isinstance(item, dict):
            continue
        names = [str(item.get("name") or "").strip()]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            names.extend(str(x).strip() for x in aliases)
        if any(name and name in haystack for name in names):
            matched.append(item)
    if bool(config.get("character_analysis_always_include_protagonists", True)):
        for item in characters:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() in protagonist_names and item not in matched:
                matched.append(item)
            if len(matched) >= max_count:
                break

    paths: list[Path] = []
    for item in matched[: max(1, int(max_count or 1))]:
        ref = str(item.get("reference_image") or "").strip()
        if ref:
            path = Path(ref)
            if path.exists():
                paths.append(path)
    return paths


def stage_character_references(
    character_analysis: dict | None,
    job_dir: Path,
    on_log: LogFn = _noop,
) -> dict | None:
    if not character_analysis or not character_analysis.get("enabled"):
        return character_analysis
    if not bool(config.get("character_reference_enabled", False)):
        return character_analysis

    characters = character_analysis.get("characters")
    if not isinstance(characters, list) or not characters:
        return character_analysis

    route = _image_route_settings("character_reference")
    provider = route["provider"]
    width = int(config.get("character_reference_width", 768) or 768)
    height = int(config.get("character_reference_height", 1024) or 1024)
    max_count = max(1, int(config.get("character_reference_max_count", 6) or 6))
    timeout_seconds = _bounded_timeout(config.get("image_api_timeout_seconds", 300), 300.0)
    char_dir = job_dir / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)

    backend = _make_image_backend(route)
    manifest: list[dict] = []
    selected = [
        c for c in characters
        if isinstance(c, dict) and str(c.get("importance") or "") in {"protagonist", "supporting"}
    ][:max_count]
    if not selected:
        selected = [c for c in characters if isinstance(c, dict)][:max_count]

    on_log(f"[4/6] character references: provider={provider} count={len(selected)} size={width}x{height}")
    for i, character in enumerate(selected):
        name = str(character.get("name") or f"character_{i + 1}").strip()
        trigger = str(character.get("trigger") or f"char_{i + 1:02d}").strip()
        out = char_dir / f"{_safe_artifact_name(trigger or name, f'char_{i + 1:02d}')}.png"
        prompt = character_reference_prompt(character, character_analysis)
        suffix = str(config.get("character_reference_prompt_suffix", "") or "").strip()
        if suffix:
            prompt = f"{prompt}. {suffix}"
        prompt = _policy_safe_image_prompt(prompt, fallback_text=name)
        character["reference_image"] = str(out)
        character["reference_prompt"] = prompt
        manifest.append({"name": name, "trigger": trigger, "path": str(out), "prompt": prompt})
        if out.exists() and out.stat().st_size > 100:
            on_log(f"  character reference reuse {name}: {out.name}")
            continue
        try:
            if provider == "placeholder":
                backend.generate(prompt, config.llm_negative_prompt, out, width=width, height=height)
            else:
                with external_api_slot(action=f"character reference {name}", timeout_seconds=timeout_seconds):
                    backend.generate(prompt, config.llm_negative_prompt, out, width=width, height=height)
            on_log(f"  character reference {i + 1}/{len(selected)} OK: {name} -> {out.name}")
        except Exception as exc:
            on_log(f"  WARN character reference failed for {name}: {exc}")

    _write_json(job_dir / "character_reference_manifest.json", manifest)
    _write_json(job_dir / "character_profiles.json", character_analysis)
    return character_analysis


_POLICY_REPLACEMENTS = [
    (r"\b(severed limbs?|dismember(?:ed|ment)?|guts?|corpse|corpses|dead bod(?:y|ies)|rivers? of blood)\b", "dramatic aftermath implied by scattered shadows"),
    (r"\b(bloody|blood-soaked|blood splatter|gore|graphic violence|massacre)\b", "tense dramatic atmosphere"),
    (r"\b(kill(?:ing)?|murder(?:ing)?|slaughter(?:ing)?|stab(?:bing|bed)?|tortur(?:e|ing)|behead(?:ing|ed)?)\b", "confronting in a tense scene"),
    (r"\b(nude|naked|sexual|erotic|seductive|intimate bed scene|rape|assault)\b", "emotional conflict"),
    (r"\b(teenage boy|teenage girl|schoolboy|schoolgirl|minor child|underage)\b", "young adult"),
    (r"\b(sword through|pinned by a sword|knife at|gun at|weapon close-up)\b", "symbolic confrontation"),
    (r"\b(victim|body|bodies)\b", "character"),
]


def _policy_safe_image_prompt(prompt: str, *, fallback_text: str = "") -> str:
    value = " ".join(str(prompt or fallback_text or "").split()).strip()
    if not value:
        value = "Cinematic anime illustration of a tense emotional novel scene, expressive faces, soft lighting"
    for pattern, replacement in _POLICY_REPLACEMENTS:
        value = re.sub(pattern, replacement, value, flags=re.I)
    value = re.sub(r"(?i)\b(blood|gore|corpse|nude|naked|sexual|erotic|severed)\b", "", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" ,.;")
    guard = (
        "Family-friendly cinematic anime illustration, adult-looking characters, symbolic emotional drama, "
        "fully clothed characters, clean composition, soft lighting, readable faces, text-free artwork"
    )
    if "family-friendly" not in value.lower():
        value = f"{value}. {guard}"
    return value[:1400]


def _is_image_policy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        item in text
        for item in (
            "content policy",
            "policy",
            "safety",
            "防护",
            "违反",
            "裸露",
            "色情",
            "情色",
            "暴力",
            "no b64_json/url",
            "can't generate",
            "cannot generate",
        )
    )


def stage_storyboard_and_image(
    plans: list[ImagePlan],
    job_dir: Path,
    character_analysis: dict | None = None,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
) -> list[Path]:
    workers = _parallel_limit("max_parallel_images", 2, len(plans), hard_cap=8)
    image_timeout_seconds = _bounded_timeout(config.get("image_api_timeout_seconds", 300), 300.0)
    scene_route = _image_route_settings("scene")
    scene_reference_route = _image_route_settings("scene_reference")
    on_log(
        f"[5/6] 分镜+出图: llm={config.llm_provider} image={scene_route['provider']} "
        f"parallel={workers} timeout={image_timeout_seconds:.0f}s"
    )

    def make_llm() -> LLMBackend:
        return LLMBackend(
            provider=config.llm_provider,
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            system_prompt=config.llm_storyboard_prompt,
            style_suffix=config.llm_image_style_suffix,
        )

    def make_image_backend(provider: str | None = None) -> ImageBackend:
        return _make_image_backend(scene_route, provider=provider)

    def make_scene_reference_backend() -> ImageBackend:
        return _make_image_backend(scene_reference_route)

    img_dir = job_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    prompts_log: list[dict | None] = [None] * len(plans)
    paths: list[Path | None] = [None] * len(plans)
    write_lock = threading.Lock()

    def process_image(i: int, plan: ImagePlan) -> tuple[int, Path, str]:
        out = img_dir / f"img_{i:05d}.png"
        base_prompt = _prompt_for_plan(make_llm(), plan, on_log, i)
        theme_context = visual_theme_context(character_analysis) if bool(config.get("scene_inject_visual_theme", True)) else ""
        character_context = character_context_for_text(
            character_analysis,
            plan.text,
            max_characters=int(config.get("character_analysis_max_characters_per_prompt", 4) or 4),
            always_include_protagonists=bool(config.get("character_analysis_always_include_protagonists", True)),
        ) if bool(config.get("scene_inject_character_triggers", True)) else ""
        if theme_context and theme_context not in character_context:
            base_prompt = f"{base_prompt}. {theme_context}"
        if character_context:
            base_prompt = f"{base_prompt}. {character_context}"
        prompt = _policy_safe_image_prompt(base_prompt, fallback_text=plan.text)
        reference_paths = _reference_images_for_text(
            character_analysis,
            plan.text,
            max_count=int(config.get("scene_reference_max_images", 2) or 2),
        ) if bool(config.get("scene_reference_enabled", False)) else []
        prompts_log[i] = {
            "index": i,
            "text": plan.text,
            "prompt": prompt,
            "theme_context": theme_context,
            "character_context": character_context,
            "reference_images": [str(p) for p in reference_paths],
        }
        if not out.exists() or out.stat().st_size < 100:
            last_exc: Exception | None = None
            for attempt in range(1, 3):
                try:
                    on_log(
                        f"  image {i + 1}/{len(plans)} call provider={scene_route['provider']} "
                        f"base={scene_route.get('base_url') or '-'} model={scene_route.get('model') or '-'} "
                        f"size={config.image_width}x{config.image_height} attempt={attempt}"
                    )
                    attempt_prompt = prompt
                    if attempt > 1 and last_exc is not None and _is_image_policy_error(last_exc):
                        attempt_prompt = _policy_safe_image_prompt(
                            "A calm symbolic scene showing the same story mood through facial expressions, "
                            "distance, lighting, and environment; no direct harm or explicit content.",
                            fallback_text=plan.text,
                        )
                        prompts_log[i] = {"index": i, "text": plan.text, "prompt": attempt_prompt, "safe_retry": True}
                        on_log(f"  image {i + 1}/{len(plans)} retry with safer prompt")
                    if scene_route["provider"] == "placeholder":
                        make_image_backend("placeholder").generate(
                            attempt_prompt,
                            config.llm_negative_prompt,
                            out,
                            width=config.image_width,
                            height=config.image_height,
                        )
                    else:
                        with external_api_slot(
                            action=f"image {i + 1}/{len(plans)}",
                            timeout_seconds=image_timeout_seconds,
                        ):
                            if reference_paths:
                                make_scene_reference_backend().generate_with_references(
                                    attempt_prompt,
                                    config.llm_negative_prompt,
                                    reference_paths,
                                    out,
                                    width=config.image_width,
                                    height=config.image_height,
                                )
                            else:
                                make_image_backend().generate(
                                    attempt_prompt,
                                    config.llm_negative_prompt,
                                    out,
                                    width=config.image_width,
                                    height=config.image_height,
                                )
                    break
                except Exception as exc:
                    last_exc = exc
                    on_log(f"  WARN 图{i} 生成失败 {attempt}/2: {exc}")
                    time.sleep(attempt)
            if not out.exists() or out.stat().st_size < 100:
                on_log(f"  WARN 图{i} 改用占位图: {last_exc}")
                make_image_backend("placeholder").generate(
                    _policy_safe_image_prompt(prompt, fallback_text=plan.text),
                    "",
                    out,
                    width=config.image_width,
                    height=config.image_height,
                )
        else:
            on_log(f"  image {i + 1}/{len(plans)} reuse cached file: {out}")
        return i, out, prompt

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image") as pool:
        future_labels = {pool.submit(process_image, i, plan): i for i, plan in enumerate(plans)}
        pending = set(future_labels)
        while pending:
            done, pending = wait(pending, timeout=30.0, return_when=FIRST_COMPLETED)
            if not done:
                waiting = ", ".join(f"image {future_labels[f] + 1}" for f in list(pending)[:4])
                more = "" if len(pending) <= 4 else f", +{len(pending) - 4} more"
                on_log(f"  image still running: {waiting}{more} ({completed}/{len(plans)} done)")
                on_prog(completed / max(1, len(plans)))
                continue
            for future in done:
                i, out, prompt = future.result()
                paths[i] = out
                completed += 1
                with write_lock:
                    _write_json(job_dir / "prompts.json", [p for p in prompts_log if p is not None])
                on_log(f"  image progress {completed}/{len(plans)}: {prompt[:80]}...")
                on_prog(completed / max(1, len(plans)))

    final_paths = [p for p in paths if p is not None]
    if len(final_paths) != len(plans):
        raise RuntimeError("Image stage did not produce all images")
    on_log(f"  OK {len(final_paths)} 张图")
    return final_paths


def _metadata_intro(novel: Novel, limit: int = 700) -> str:
    desc = re.sub(r"\s+", " ", str(novel.description or "")).strip()
    if desc and desc.lower() != "local text input":
        return desc[:limit]
    text = str(novel.full_text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].strip() == str(novel.title or "").strip():
        lines = lines[1:]
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined[:limit]


_AUTHOR_SUFFIX_RE = re.compile(r"\s*[\(（【\[][^()（）【】\[\]]{1,24}[\)）】\]]\s*$")


def _clean_display_title(title: str) -> str:
    """Remove trailing author/source hints such as `Title(author)` from public titles."""
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _AUTHOR_SUFFIX_RE.sub("", value).strip()
    return value or str(title or "").strip() or "Untitled"


def _clean_short_title(text: str, min_chars: int, max_chars: int) -> str:
    value = html.unescape(str(text or ""))
    value = re.sub(r"[《》“”\"'`#【】\[\]（）()]", "", value)
    value = re.sub(r"(?i)^(短说明标题|标题|short title)[:：]\s*", "", value.strip())
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[，。！？；、,.!?;：:]+$", "", value)
    if len(value) > max_chars:
        value = value[:max_chars]
        value = re.sub(r"[，。！？；、,.!?;：:]+$", "", value)
    return value[:max_chars]


def _fallback_short_title(novel: Novel, intro: str, min_chars: int, max_chars: int) -> str:
    text = re.sub(r"\s+", "", intro or novel.full_text or novel.title or "")
    text = re.sub(r"^(简介|内容简介|小说简介|作品简介|书籍简介)[:：]?", "", text)
    text = re.sub(r"[《》“”\"'`#【】\[\]（）()]", "", text)
    title = re.sub(r"\s+", "", _clean_display_title(novel.title))
    if title and text.startswith(title):
        text = text[len(title):]
    parts = re.split(r"[。！？!?；;]", text)
    value = ""
    for part in parts:
        part = re.sub(r"[，、,:：]+", "", part.strip())
        if not part:
            continue
        value += part
        if len(value) >= min_chars:
            break
    if not value:
        value = title or "精彩小说推文剧情解说"
    if len(value) < min_chars and title and title not in value:
        value = f"{title}{value}"
    return _clean_short_title(value, min_chars, max_chars) or (title[:max_chars] if title else "精彩小说推文剧情解说")


def stage_metadata(novel: Novel, job_dir: Path, on_log: LogFn = _noop) -> dict:
    intro = _metadata_intro(novel)
    clean_title = _clean_display_title(novel.title)
    min_chars = max(6, int(config.get("short_title_min_chars", 20) or 20))
    max_chars = max(min_chars, int(config.get("short_title_max_chars", 30) or 30))
    short_title = ""
    if bool(config.get("short_title_enabled", True)):
        if config.llm_api_key or config.llm_provider == "ollama":
            try:
                llm = LLMBackend(
                    provider=config.llm_provider,
                    base_url=config.llm_base_url,
                    api_key=config.llm_api_key,
                    model=config.llm_model,
                    system_prompt=str(config.get("short_title_prompt", "") or ""),
                    style_suffix="",
                )
                request = f"书名：{clean_title}\n简介：{intro[:900]}\n只输出短说明标题。"
                with external_api_slot(action="short title"):
                    short_title = _clean_short_title(llm.storyboard(request), min_chars, max_chars)
            except Exception as exc:
                on_log(f"  WARN short title LLM failed: {exc}; use intro fallback")
        if not short_title:
            short_title = _fallback_short_title(novel, intro, min_chars, max_chars)
    metadata = {
        "title": novel.title,
        "clean_title": clean_title,
        "short_title": short_title,
        "intro": intro,
        "author": novel.author,
        "tags": str(config.youtube_tags or ""),
    }
    _write_json(job_dir / "metadata.json", metadata)
    if short_title:
        on_log(f"  short_title: {short_title}")
    return metadata


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + str(key) + "}"


def _format_template(template: str, context: dict, fallback: str = "") -> str:
    text = str(template or "")
    if not text:
        return fallback
    try:
        return text.format_map(_SafeFormatDict({k: "" if v is None else v for k, v in context.items()}))
    except Exception:
        return text or fallback


def _limit_upload_title(text: str, max_chars: int = 100) -> str:
    title = re.sub(r"\s+", " ", str(text or "")).strip()
    limit = max(0, int(max_chars or 0))
    if not limit or len(title) <= limit:
        return title
    for suffix in (" #小说 #推文", "#小说 #推文"):
        if title.endswith(suffix) and limit > len(suffix) + 8:
            body = title[: limit - len(suffix)].rstrip(" ，。！？、,.!?;；:")
            return (body + suffix).strip()
    return title[:limit].rstrip(" ，。！？、,.!?;；:")


def _upload_lock_path() -> Path:
    return JOBS_DIR.parent / "upload.lock"


def _read_upload_lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _acquire_upload_lock(job_dir: Path, on_log: LogFn = _noop) -> Path:
    path = _upload_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_dir.name,
        "pid": os.getpid(),
        "created_at": time.time(),
        "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    last_log = 0.0
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            on_log(f"  upload queue acquired by {job_dir.name}")
            return path
        except FileExistsError:
            info = _read_upload_lock(path)
            pid = int(info.get("pid") or 0)
            age = time.time() - float(info.get("created_at") or 0)
            if (pid and not pid_alive(pid)) or age > 12 * 3600:
                try:
                    path.unlink()
                    on_log("  WARN removed stale upload queue lock")
                    continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
            now = time.monotonic()
            if now - last_log >= 60:
                owner = info.get("job_id") or "another job"
                on_log(f"  upload queue busy: waiting for {owner} to finish")
                last_log = now
            time.sleep(5)


def _release_upload_lock(path: Path, job_id: str) -> None:
    info = _read_upload_lock(path)
    if info.get("job_id") not in {"", None, job_id}:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _ensure_upload_dependencies(on_log: LogFn = _noop) -> None:
    if not bool(config.get("upload_dependency_check_before_upload", True)):
        return
    from app.dependency_manager import ensure_dependencies

    on_log("  checking upload dependencies before YouTube upload")
    report = ensure_dependencies(scope="full", on_log=lambda line: on_log(f"  {line}"))
    if not bool(report.get("ok")):
        summary = str(report.get("summary") or "upload dependencies are not ready")
        raise RuntimeError(f"YouTube upload dependencies are not ready: {summary}")


def _cover_provider() -> str:
    return _image_route_settings("cover")["provider"]


def _cover_backend_settings() -> dict:
    return _image_route_settings("cover")


def _cover_excerpt(novel: Novel, segments: list[Segment], limit: int = 900) -> str:
    text = novel.full_text or "\n".join(seg.text for seg in segments[:8])
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(80, int(limit))]


def _build_cover_prompt(novel: Novel, segments: list[Segment], on_log: LogFn = _noop) -> str:
    title = _clean_display_title(novel.title)
    excerpt = _cover_excerpt(novel, segments)
    style = str(config.llm_image_style_suffix or "").strip()
    fallback_template = str(config.get("cover_prompt_template", "") or "")
    try:
        fallback = fallback_template.format(title=title, excerpt=excerpt, style=style)
    except Exception:
        fallback = (
            f'Create a cinematic YouTube cover illustration for a novel video. '
            f'Exact story title context: "{title}". Story excerpt: {excerpt}. '
            f'Do not render text in the artwork. Leave natural uncluttered lower space for title overlay. {style}'
        )

    if config.llm_api_key or config.llm_provider == "ollama":
        try:
            cover_system = (
                "You write one concise English image-generation prompt for a long-form novel recap cover. "
                "Use the title as fixed story context. Do not ask the image model to render any title, words, "
                "logos, captions, UI, or watermarks. Keep the scene policy-safe: no gore, blood, corpse, "
                "nudity, sexual content, weapon close-ups, or explicit violence. Ask for clean uncluttered visual space at the bottom where "
                "the video editor will overlay the exact title."
            )
            llm = LLMBackend(
                provider=config.llm_provider,
                base_url=config.llm_base_url,
                api_key=config.llm_api_key,
                model=config.llm_model,
                system_prompt=cover_system,
                style_suffix=style,
            )
            request = (
                f'Exact title: "{title}"\n'
                f"Excerpt: {excerpt}\n\n"
                "Return one prompt only. The prompt must be based on the excerpt and must keep the exact title "
                "as story context, but the artwork itself must contain no readable text."
            )
            with external_api_slot(action="cover prompt"):
                prompt = llm.storyboard(request).strip()
            if title not in prompt:
                prompt = f'Title context: "{title}". {prompt}'
            return _policy_safe_image_prompt(
                f'{prompt}. Do not render any words or title text in the image; '
                f'leave a natural uncluttered lower area for the overlaid title "{title}".',
                fallback_text=excerpt,
            )
        except Exception as exc:
            on_log(f"  WARN cover prompt LLM failed: {exc}; use template prompt")
    return _policy_safe_image_prompt(fallback, fallback_text=excerpt)


def _hex_rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return fallback
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return fallback


def _load_title_font(font_name: str, size: int) -> ImageFont.ImageFont:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    fonts_dir = windir / "Fonts"
    candidates: list[str | Path] = []
    name = str(font_name or "").strip()
    if name:
        candidates.append(name)
    if "yahei" in name.lower() or "雅黑" in name:
        candidates.extend(["msyhbd.ttc", "msyh.ttc"])
    candidates.extend(
        [
            fonts_dir / "msyhbd.ttc",
            fonts_dir / "msyh.ttc",
            fonts_dir / "simhei.ttf",
            fonts_dir / "arialbd.ttf",
            fonts_dir / "arial.ttf",
            "msyhbd.ttc",
            "msyh.ttc",
            "simhei.ttf",
            "arialbd.ttf",
            "arial.ttf",
        ]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def _line_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text or "Ag", font=font)
    return int(box[3] - box[1])


def _wrap_pixels(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in [part.strip() for part in str(text or "").splitlines() if part.strip()] or [str(text or "").strip()]:
        current = ""
        for ch in paragraph:
            test = current + ch
            if not current or _text_width(draw, test, font) <= max_width:
                current = test
            else:
                lines.append(current)
                current = ch
        if current:
            lines.append(current)
    return lines or [str(text or "").strip()]


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    suffix = "..."
    text = str(text or "")
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return (text + suffix) if text else suffix


def _fit_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    font_name: str,
    start_size: int,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    min_size = 26
    for size in range(max(min_size, int(start_size)), min_size - 1, -2):
        font = _load_title_font(font_name, size)
        lines = _wrap_pixels(draw, title, font, max_width)
        spacing = max(6, int(size * 0.16))
        line_h = _line_height(draw, title, font)
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
        if len(lines) <= 2 and total_h <= max_height and all(_text_width(draw, line, font) <= max_width for line in lines):
            return font, lines, spacing

    font = _load_title_font(font_name, min_size)
    lines = _wrap_pixels(draw, title, font, max_width)
    if len(lines) > 2:
        merged_tail = "".join(lines[1:])
        lines = [lines[0], _ellipsize(draw, merged_tail, font, max_width)]
    return font, lines[:2], max(5, int(min_size * 0.16))


def _render_cover_title(raw_path: Path, out_path: Path, title: str) -> Path:
    width = max(320, int(config.get("cover_width", 1280) or 1280))
    height = max(180, int(config.get("cover_height", 720) or 720))
    ratio = float(config.get("cover_title_area_ratio", 0.24) or 0.24)
    band_h = max(110, min(height // 2, int(height * ratio)))
    img = Image.open(raw_path).convert("RGB")
    img = ImageOps.fit(img, (width, height), method=Image.LANCZOS, centering=(0.5, 0.45)).convert("RGBA")

    fg = _hex_rgb(str(config.get("cover_title_color", "#FFFFFF")), (255, 255, 255))

    draw = ImageDraw.Draw(img)
    title = (title or "Untitled").strip()
    max_width = int(width * 0.88)
    max_height = max(60, band_h - 34)
    font, lines, spacing = _fit_title(
        draw,
        title,
        str(config.get("cover_title_font", "Microsoft YaHei") or "Microsoft YaHei"),
        int(config.get("cover_title_size", 72) or 72),
        max_width,
        max_height,
    )
    line_h = _line_height(draw, title, font)
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing
    y = height - band_h + (band_h - total_h) / 2
    shadow = max(2, int(getattr(font, "size", 40) * 0.04))
    stroke = max(2, int(getattr(font, "size", 40) * 0.055))
    for line in lines:
        line_w = _text_width(draw, line, font)
        x = (width - line_w) / 2
        draw.text((x + shadow, y + shadow), line, fill=(0, 0, 0, 165), font=font)
        draw.text((x, y), line, fill=(*fg, 255), font=font, stroke_width=stroke, stroke_fill=(0, 0, 0, 205))
        y += line_h + spacing

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "JPEG", quality=93, optimize=True)
    return out_path


def _create_cover_placeholder(prompt: str, title: str, out_path: Path, width: int, height: int) -> Path:
    seed = int(_text_hash(f"{title}\n{prompt}")[:12], 16)
    palettes = [
        ((18, 24, 34), (94, 132, 148), (222, 189, 96)),
        ((28, 24, 40), (116, 86, 139), (218, 174, 126)),
        ((18, 36, 35), (76, 126, 103), (204, 210, 173)),
        ((38, 28, 26), (138, 76, 61), (231, 173, 91)),
    ]
    base, mid, accent = palettes[seed % len(palettes)]
    img = Image.new("RGB", (width, height), base)
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        mix = t * 0.78
        row = tuple(int(base[i] * (1 - mix) + mid[i] * mix) for i in range(3))
        for x in range(width):
            px[x, y] = row

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    shift = seed % max(1, width // 3)
    d.polygon(
        [
            (width * 0.08 + shift // 3, height * 0.20),
            (width * 0.74, height * 0.06),
            (width * 0.92, height * 0.62),
            (width * 0.20, height * 0.78),
        ],
        fill=(*accent, 44),
    )
    d.polygon(
        [
            (-width * 0.08, height * 0.62),
            (width * 0.42, height * 0.32),
            (width * 0.70, height * 0.86),
            (width * 0.06, height * 1.04),
        ],
        fill=(255, 255, 255, 20),
    )
    d.rectangle((0, int(height * 0.70), width, height), fill=(0, 0, 0, 46))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def stage_cover(
    novel: Novel,
    segments: list[Segment],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> Path | None:
    if not bool(config.get("cover_enabled", True)):
        return None

    title = _clean_display_title(novel.title)
    cover_dir = job_dir / "cover"
    cover_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cover_dir / "cover_raw.png"
    out_path = cover_dir / "cover.jpg"
    manifest_path = cover_dir / "cover_manifest.json"
    excerpt = _cover_excerpt(novel, segments)
    cover_backend = _cover_backend_settings()
    provider = cover_backend["provider"]
    expected_hash = _stable_hash(
        {
            "schema": "cover_v1",
            "title": title,
            "excerpt_hash": _text_hash(excerpt),
            "provider": provider,
            "base_url": cover_backend["base_url"],
            "model": cover_backend["model"],
            "style": config.llm_image_style_suffix,
            "prompt_template": config.get("cover_prompt_template", ""),
            "width": int(config.get("cover_width", 1280) or 1280),
            "height": int(config.get("cover_height", 720) or 720),
            "title_size": int(config.get("cover_title_size", 72) or 72),
            "title_font": config.get("cover_title_font", ""),
            "title_color": config.get("cover_title_color", ""),
            "title_bg": config.get("cover_title_bg", ""),
            "title_area_ratio": float(config.get("cover_title_area_ratio", 0.24) or 0.24),
            "api_key_hash": _text_hash(cover_backend["api_key"])[:16],
        }
    )
    manifest = _read_json(manifest_path, {})
    if (
        isinstance(manifest, dict)
        and manifest.get("input_hash") == expected_hash
        and out_path.exists()
        and out_path.stat().st_size > 1000
    ):
        on_log(f"  reuse cover: {out_path}")
        return out_path

    on_log(f"[cover] Generate cover from title/text: {title}")
    prompt = _build_cover_prompt(novel, segments, on_log=on_log)
    (cover_dir / "cover_prompt.txt").write_text(prompt, encoding="utf-8")
    width = int(config.get("cover_width", 1280) or 1280)
    height = int(config.get("cover_height", 720) or 720)
    backend = ImageBackend(
        provider=provider,
        base_url=cover_backend["base_url"],
        api_key=cover_backend["api_key"],
        model=cover_backend["model"],
        steps=config.image_steps,
        cfg=config.image_cfg,
        workflow_path=cover_backend.get("workflow_path", config.image_workflow),
        timeout_seconds=config.get("image_api_timeout_seconds", 300),
    )
    raw_tmp = raw_path.with_suffix(".tmp.png")
    raw_tmp.unlink(missing_ok=True)
    try:
        if provider == "placeholder":
            _create_cover_placeholder(prompt, title, raw_tmp, width, height)
        else:
            with external_api_slot(action="cover image"):
                backend.generate(prompt, config.llm_negative_prompt, raw_tmp, width=width, height=height)
        raw_tmp.replace(raw_path)
    except Exception as exc:
        raw_tmp.unlink(missing_ok=True)
        on_log(f"  WARN cover image generation failed: {exc}; use generated video image fallback")
        if not _create_cover_from_video_image(job_dir, raw_path, width, height):
            on_log("  WARN no generated image available for cover fallback; use placeholder")
            _create_cover_placeholder(prompt, title, raw_path, width, height)

    _render_cover_title(raw_path, out_path, title)
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "ready",
            "input_hash": expected_hash,
            "title": title,
            "provider": provider,
            "prompt": prompt,
            "raw_path": str(raw_path),
            "path": str(out_path),
            "file": _file_signature(out_path),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    on_log(f"  OK cover: {out_path}")
    return out_path


def _create_cover_from_video_image(job_dir: Path, out_path: Path, width: int, height: int) -> bool:
    image_dir = job_dir / "images"
    candidates = sorted(image_dir.glob("img_*.png")) + sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.jpeg"))
    for src in candidates:
        try:
            with Image.open(src) as img:
                fitted = ImageOps.fit(img.convert("RGB"), (max(1, int(width)), max(1, int(height))), method=Image.LANCZOS)
                fitted.save(out_path, "PNG")
                return True
        except Exception:
            continue
    return False


def _load_compose_manifest(path: Path) -> dict:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("steps"), dict):
        data["steps"] = {}
    data["schema_version"] = 1
    return data


def _compose_step_ready(manifest: dict, step: str, input_hash: str, out_path: Path) -> bool:
    data = (manifest.get("steps") or {}).get(step) or {}
    if str(data.get("input_hash") or "") != input_hash:
        return False
    if not out_path.exists():
        return False
    sig = _file_signature(out_path)
    if int(data.get("file_size") or -1) != sig.get("size"):
        return False
    try:
        if abs(float(data.get("file_mtime") or 0) - float(sig.get("mtime") or 0)) > 1.0:
            return False
    except Exception:
        return False
    return _audio_duration(out_path) > 0.05


def _record_compose_step(manifest_path: Path, manifest: dict, step: str, input_hash: str, out_path: Path) -> None:
    sig = _file_signature(out_path)
    steps = manifest.setdefault("steps", {})
    steps[step] = {
        "status": "ready",
        "input_hash": input_hash,
        "path": str(out_path),
        "duration": round(_audio_duration(out_path), 3),
        "file_size": sig.get("size", 0),
        "file_mtime": sig.get("mtime", 0),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(manifest_path, manifest)


def _stage_compose_manifest_impl(
    audios: list[Path],
    durations: list[float],
    segments: list[Segment],
    plans: list[ImagePlan],
    images: list[Path],
    job_dir: Path,
    on_log: LogFn,
) -> Path:
    on_log("[6/6] Video compose")
    manifest_path = job_dir / "compose_manifest.json"
    manifest = _load_compose_manifest(manifest_path)

    audio_full = job_dir / "audio_full.mp3"
    audio_hash = _stable_hash(
        {
            "kind": "audio_concat_v1",
            "audios": [{"path": str(p), **_file_signature(p)} for p in audios],
        }
    )
    if _compose_step_ready(manifest, "audio_concat", audio_hash, audio_full):
        on_log("  reuse audio_full.mp3 from compose manifest")
    else:
        concat_audios(audios, audio_full, on_log)
        _record_compose_step(manifest_path, manifest, "audio_concat", audio_hash, audio_full)

    subs = []
    t = 0.0
    for seg, dur in zip(segments, durations):
        end = t + max(0.05, dur)
        subs.append((t, end, seg.text))
        t = end

    ass_path = job_dir / "subtitle.ass"
    build_ass(
        subs,
        ass_path,
        font=config.video_subtitle_font,
        size=config.video_subtitle_size,
        video_w=config.video_width,
        video_h=config.video_height,
        position=config.get("video_subtitle_position", "下边"),
        primary_color=config.get("video_subtitle_color", "#FFFFFF"),
        outline_color=config.get("video_subtitle_outline_color", "#000000"),
        back_color=config.get("video_subtitle_back_color", "#000000"),
        outline=float(config.get("video_subtitle_outline", 4.0) or 4.0),
        shadow=float(config.get("video_subtitle_shadow", 1.0) or 1.0),
        margin_v=int(config.get("video_subtitle_margin_v", 60) or 60),
        margin_lr=int(config.get("video_subtitle_margin_lr", 56) or 56),
        spacing=float(config.get("video_subtitle_spacing", 0.0) or 0.0),
        bold=bool(config.get("video_subtitle_bold", False)),
        italic=bool(config.get("video_subtitle_italic", False)),
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )
    if config.video_external_subtitle:
        build_srt(
            subs,
            job_dir / "subtitle.srt",
            chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
            max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
        )

    bgm = Path(config.video_bgm_path) if config.video_bgm_path else None
    image_durations = [p.duration for p in plans]
    out = job_dir / "final.mp4"
    final_hash = _stable_hash(
        {
            "kind": "final_video_v2",
            "audio": {"path": str(audio_full), **_file_signature(audio_full)},
            "images": [{"path": str(p), **_file_signature(p)} for p in images],
            "image_durations": [round(float(v), 3) for v in image_durations],
            "segments": [_text_hash(s.text) for s in segments],
            "video": {
                "width": config.video_width,
                "height": config.video_height,
                "fps": config.video_fps,
                "encoder": config.get("video_encoder", "libx264"),
                "encoder_preset": config.get("video_encoder_preset", "veryfast"),
                "encoder_quality": config.get("video_encoder_quality", 20),
                "ken_burns": bool(config.ken_burns),
                "long_mode": bool(config.video_long_mode),
                "motion": str(config.video_motion or "none"),
                "motion_curve": str(config.get("video_motion_curve", "ease") or "ease"),
                "motion_cycle_seconds": float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
                "transition": str(config.video_transition or "none"),
                "transition_duration": float(config.video_transition_duration or 0.4),
                "subtitle": True,
                "subtitle_font": config.video_subtitle_font,
                "subtitle_size": config.video_subtitle_size,
                "subtitle_style": {
                    "position": config.get("video_subtitle_position", "下边"),
                    "color": config.get("video_subtitle_color", "#FFFFFF"),
                    "outline_color": config.get("video_subtitle_outline_color", "#000000"),
                    "back_color": config.get("video_subtitle_back_color", "#000000"),
                    "outline": config.get("video_subtitle_outline", 4.0),
                    "shadow": config.get("video_subtitle_shadow", 1.0),
                    "margin_v": config.get("video_subtitle_margin_v", 60),
                    "margin_lr": config.get("video_subtitle_margin_lr", 56),
                    "spacing": config.get("video_subtitle_spacing", 0.0),
                    "bold": config.get("video_subtitle_bold", False),
                    "italic": config.get("video_subtitle_italic", False),
                    "chars_per_line": config.get("video_subtitle_chars_per_line", 24),
                    "max_lines": config.get("video_subtitle_max_lines", 2),
                },
                "bgm": str(bgm) if bgm else "",
                "bgm_sig": _file_signature(bgm) if bgm and bgm.exists() else {},
                "bgm_volume": config.video_bgm_volume,
            },
        }
    )
    if _compose_step_ready(manifest, "final_video", final_hash, out):
        on_log(f"  reuse final video: {out}")
        return out

    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    tmp.unlink(missing_ok=True)
    try:
        build_video(
            images=images,
            image_durations=image_durations,
            audio_path=audio_full,
            out_path=tmp,
            on_log=on_log,
            width=config.video_width,
            height=config.video_height,
            fps=config.video_fps,
            ken_burns=bool(config.ken_burns),
            subtitle_ass=ass_path,
            bgm_path=bgm,
            bgm_volume=config.video_bgm_volume,
            long_mode=config.video_long_mode,
            cleanup_temp=config.video_cleanup_temp,
            motion=str(config.video_motion or "none"),
            motion_curve=str(config.get("video_motion_curve", "ease") or "ease"),
            motion_cycle_seconds=float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
            transition=config.video_transition,
            transition_duration=float(config.video_transition_duration or 0.4),
            video_encoder=str(config.get("video_encoder", "libx264") or "libx264"),
            video_encoder_preset=str(config.get("video_encoder_preset", "veryfast") or "veryfast"),
            video_encoder_quality=int(config.get("video_encoder_quality", 20) or 20),
        )
        video_duration = _audio_duration(tmp)
        audio_duration = _audio_duration(audio_full)
        if video_duration <= 0.05:
            raise RuntimeError("final video duration is zero")
        if audio_duration > 60 and video_duration < audio_duration * 0.75:
            raise RuntimeError(f"final video too short: video={video_duration:.1f}s audio={audio_duration:.1f}s")
        if audio_duration > 60 and video_duration < audio_duration * 0.95:
            on_log(f"  WARN final video shorter than audio: video={video_duration:.1f}s audio={audio_duration:.1f}s")
        tmp.replace(out)
        _record_compose_step(manifest_path, manifest, "final_video", final_hash, out)
    finally:
        tmp.unlink(missing_ok=True)
    on_log(f"  OK video completed: {out}")
    return out


def stage_upload(video: Path, title: str, cover: Path | None, job_dir: Path, metadata: dict | None = None, on_log: LogFn = _noop) -> str:
    if not bool(config.upload_enabled):
        return ""
    if str(config.youtube_upload_method or "browser").lower() != "browser":
        on_log("  WARN only browser upload is wired in this build")
        return ""
    metadata = metadata or {}
    clean_title = str(metadata.get("clean_title") or _clean_display_title(title))
    context = {
        "title": title,
        "clean_title": clean_title,
        "short_title": metadata.get("short_title", ""),
        "intro": metadata.get("intro", ""),
        "author": metadata.get("author", ""),
        "tags": str(config.youtube_tags or metadata.get("tags", "")),
        "job_id": job_dir.name,
    }
    profile = _active_upload_profile()
    title_template = str(profile.get("title_template") or config.youtube_title_template or "{clean_title}")
    description_template = str(profile.get("description") or config.youtube_description or "")
    flow = _normalize_upload_flow(profile.get("flow") or config.browser_flow or "simple")
    visibility = str(profile.get("visibility") or config.youtube_visibility or "PRIVATE").strip().upper()
    upload_policy = str(profile.get("upload_policy") or config.browser_upload_policy or "BTRA")
    ad_interval = int(profile.get("ad_interval") or config.browser_ad_interval or 60)
    ad_start = int(profile.get("ad_start") or config.browser_ad_start or 0)

    upload_title = _limit_upload_title(
        _format_template(title_template, context, fallback=clean_title),
        int(config.get("youtube_title_max_chars", 100) or 100),
    ) or clean_title
    upload_description = _format_template(description_template, context, fallback="")
    on_log(f"[7/7] YouTube browser upload: {upload_title}")
    on_log(f"  upload profile={profile.get('name', '-') or '-'} flow={'精简' if flow == 'simple' else '完整'}")
    lock_path = _acquire_upload_lock(job_dir, on_log)
    try:
        _ensure_upload_dependencies(on_log)
        from app.upload import upload_to_youtube

        video_id = upload_to_youtube(
            video,
            upload_title,
            description=upload_description,
            visibility=visibility,
            cover_path=cover if cover and cover.exists() else None,
            flow=flow,
            upload_policy=upload_policy,
            ad_interval=ad_interval,
            ad_start=ad_start,
            auto_restart=bool(config.browser_auto_restart),
            stall_timeout_min=int(config.browser_stall_timeout_min or 10),
            op_speed=str(config.browser_op_speed or "normal"),
            on_log=on_log,
            on_progress=lambda p: write_status(job_dir, stage="upload", upload_progress=float(p or 0)),
        )
    finally:
        _release_upload_lock(lock_path, job_dir.name)
    if not video_id:
        raise RuntimeError("YouTube upload failed")
    url = f"https://www.youtube.com/watch?v={video_id}"
    _write_json(job_dir / "upload_result.json", {"video_id": video_id, "url": url, "title": upload_title, "description": upload_description})
    on_log(f"  OK uploaded: {url}")
    return url


def _upload_profiles() -> list[dict]:
    try:
        raw = json.loads(str(config.get("browser_profiles", "[]") or "[]"))
    except Exception:
        raw = []
    profiles = [p for p in raw if isinstance(p, dict)]
    if profiles:
        return profiles
    return [
        {
            "name": "无创收精简流程",
            "flow": "simple",
            "upload_policy": "BTRA",
            "ad_interval": 60,
            "ad_start": 0,
            "visibility": str(config.youtube_visibility or "PUBLIC"),
            "title_template": "",
            "description": "",
        },
        {
            "name": "完整创收流程",
            "flow": "full",
            "upload_policy": str(config.browser_upload_policy or "BTRA"),
            "ad_interval": int(config.browser_ad_interval or 60),
            "ad_start": int(config.browser_ad_start or 0),
            "visibility": str(config.youtube_visibility or "PUBLIC"),
            "title_template": "",
            "description": "",
        },
    ]


def _active_upload_profile() -> dict:
    profiles = _upload_profiles()
    active = str(config.get("browser_active_profile", "") or "").strip()
    for profile in profiles:
        if str(profile.get("name", "")).strip() == active:
            return dict(profile)
    return dict(profiles[0]) if profiles else {}


def _normalize_upload_flow(value: str) -> str:
    text = str(value or "").strip().lower()
    if "full" in text or "完整" in text or "创收" in text:
        return "full"
    return "simple"


def stage_compose(
    audios: list[Path],
    durations: list[float],
    segments: list[Segment],
    plans: list[ImagePlan],
    images: list[Path],
    job_dir: Path,
    on_log: LogFn = _noop,
) -> Path:
    return _stage_compose_manifest_impl(audios, durations, segments, plans, images, job_dir, on_log)
    audio_full = job_dir / "audio_full.mp3"
    if not audio_full.exists() or _audio_duration(audio_full) <= 0.05:
        concat_audios(audios, audio_full, on_log)

    subs = []
    t = 0.0
    for seg, dur in zip(segments, durations):
        end = t + max(0.05, dur)
        subs.append((t, end, seg.text))
        t = end

    ass_path = job_dir / "subtitle.ass"
    build_ass(
        subs,
        ass_path,
        font=config.video_subtitle_font,
        size=config.video_subtitle_size,
        video_w=config.video_width,
        video_h=config.video_height,
        position=config.get("video_subtitle_position", "下边"),
        primary_color=config.get("video_subtitle_color", "#FFFFFF"),
        outline_color=config.get("video_subtitle_outline_color", "#000000"),
        back_color=config.get("video_subtitle_back_color", "#000000"),
        outline=float(config.get("video_subtitle_outline", 4.0) or 4.0),
        shadow=float(config.get("video_subtitle_shadow", 1.0) or 1.0),
        margin_v=int(config.get("video_subtitle_margin_v", 60) or 60),
        margin_lr=int(config.get("video_subtitle_margin_lr", 56) or 56),
        spacing=float(config.get("video_subtitle_spacing", 0.0) or 0.0),
        bold=bool(config.get("video_subtitle_bold", False)),
        italic=bool(config.get("video_subtitle_italic", False)),
        chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
        max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
    )
    if config.video_external_subtitle:
        build_srt(
            subs,
            job_dir / "subtitle.srt",
            chars_per_line=int(config.get("video_subtitle_chars_per_line", 24) or 24),
            max_lines=int(config.get("video_subtitle_max_lines", 2) or 2),
        )

    bgm = Path(config.video_bgm_path) if config.video_bgm_path else None
    out = job_dir / "final.mp4"
    build_video(
        images=images,
        image_durations=[p.duration for p in plans],
        audio_path=audio_full,
        out_path=out,
        on_log=on_log,
        width=config.video_width,
        height=config.video_height,
        fps=config.video_fps,
        ken_burns=bool(config.ken_burns),
        subtitle_ass=ass_path,
        bgm_path=bgm,
        bgm_volume=config.video_bgm_volume,
        long_mode=config.video_long_mode,
        cleanup_temp=config.video_cleanup_temp,
        motion=str(config.video_motion or "none"),
        motion_curve=str(config.get("video_motion_curve", "ease") or "ease"),
        motion_cycle_seconds=float(config.get("video_motion_cycle_seconds", 18.0) or 18.0),
        transition=config.video_transition,
        transition_duration=float(config.video_transition_duration or 0.4),
        video_encoder=str(config.get("video_encoder", "libx264") or "libx264"),
        video_encoder_preset=str(config.get("video_encoder_preset", "veryfast") or "veryfast"),
        video_encoder_quality=int(config.get("video_encoder_quality", 20) or 20),
    )
    on_log(f"  OK 视频完成: {out}")
    return out


def run_full(
    url_or_id: str,
    on_log: LogFn = _noop,
    on_prog: ProgFn = _noop,
    job_id: str | None = None,
    resume: bool = False,
) -> dict:
    job_id = job_id or new_job_id()
    job_dir = job_dir_for(job_id)
    if job_dir.exists() and not resume:
        raise FileExistsError(f"任务目录已存在: {job_dir}")
    job_dir.mkdir(parents=True, exist_ok=True)

    def log(message: str):
        append_log(job_dir, message)
        on_log(message)

    def progress(stage: str, value: float, **extra):
        write_status(job_dir, job_id=job_id, stage=stage, progress=max(0.0, min(1.0, float(value))), **extra)
        on_prog(value)

    job_started = time.monotonic()
    timings: dict[str, float] = {}
    timings_lock = threading.Lock()

    def timed(label: str, fn, *args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            with timings_lock:
                timings[label] = timings.get(label, 0.0) + elapsed
            log(f"  timing {label}: {elapsed:.1f}s")

    def save_performance() -> dict:
        with timings_lock:
            ordered = dict(sorted(timings.items(), key=lambda item: item[1], reverse=True))
        payload = {
            "job_id": job_id,
            "wall_seconds": round(time.monotonic() - job_started, 3),
            "summed_stage_seconds": round(sum(ordered.values()), 3),
            "stages": {key: round(value, 3) for key, value in ordered.items()},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _write_json(job_dir / "performance.json", payload)
        if ordered:
            summary = " | ".join(f"{name} {seconds:.1f}s" for name, seconds in list(ordered.items())[:6])
            log(f"  performance summary: {summary}")
        return payload

    log(f"== Job {job_id} ==")
    progress("starting", 0.0, input=url_or_id, site=config.scraper_site, worker_pid=os.getpid(), started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        progress("scrape", 0.03)
        novel = timed(
            "1_scrape",
            stage_scrape,
            url_or_id,
            site=config.scraper_site,
            max_chars=config.scraper_max_chars,
            on_log=log,
        )
        _write_novel(job_dir / "novel.json", novel)
        metadata = timed("2_metadata", stage_metadata, novel, job_dir, on_log=log)
        progress("clean", 0.10, title=novel.title, short_title=metadata.get("short_title", ""))

        segments = timed("3_clean", stage_clean, novel, on_log=log, job_dir=job_dir)
        _write_json(job_dir / "segments.json", [{"i": s.index, "text": s.text} for s in segments])
        character_analysis = timed("4_character_analysis", stage_character_analysis, novel, segments, job_dir, on_log=log)
        character_analysis = timed("4b_character_references", stage_character_references, character_analysis, job_dir, on_log=log)
        progress("tts", 0.15, title=novel.title)

        overlap_images = bool(config.get("pipeline_overlap_tts_images", True))
        if overlap_images:
            log("  overlap enabled: prefetch storyboard/images while TTS is running")
            estimated_durs = [_estimated_speech_duration(s.text) for s in segments]
            prefetch_plans = timed("4_pacing_prefetch", stage_pacing, segments, estimated_durs, on_log=log)
            _write_json(job_dir / "plans_prefetch.json", [_image_plan_to_dict(p) for p in prefetch_plans])
            prefetch_dir = job_dir / "_prefetch_images"
            executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline-overlap")
            tts_future = executor.submit(
                timed,
                "5_tts",
                stage_tts,
                segments,
                job_dir,
                log,
                lambda p: progress("tts", 0.15 + p * 0.30, title=novel.title),
            )
            image_future = executor.submit(
                timed,
                "6_images_prefetch",
                stage_storyboard_and_image,
                prefetch_plans,
                prefetch_dir,
                character_analysis,
                log,
                lambda p: progress("images_prefetch", 0.18 + p * 0.28, title=novel.title),
            )
            try:
                audios, durs = tts_future.result()
                progress("pacing", 0.45, title=novel.title)
                plans = timed("4_pacing_final", stage_pacing, segments, durs, on_log=log)
                _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
                if _plan_signature(plans) == _plan_signature(prefetch_plans):
                    progress("images", 0.50, title=novel.title)
                    try:
                        prefetch_images = image_future.result()
                        images = _copy_prefetched_images(prefetch_images, prefetch_dir, job_dir)
                        log("  reuse prefetched storyboard/images")
                    except Exception as exc:
                        log(f"  WARN prefetched images failed: {exc}; regenerating images after TTS")
                        images = timed(
                            "6_images_final",
                            stage_storyboard_and_image,
                            plans,
                            job_dir,
                            character_analysis,
                            on_log=log,
                            on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
                        )
                else:
                    log("  prefetch plan differs from real TTS pacing; regenerating images with final pacing")
                    try:
                        image_future.result()
                    except Exception as exc:
                        log(f"  WARN discarded prefetch image task failed: {exc}")
                    images = timed(
                        "6_images_final",
                        stage_storyboard_and_image,
                        plans,
                        job_dir,
                        character_analysis,
                        on_log=log,
                        on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
                    )
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        else:
            audios, durs = timed(
                "5_tts",
                stage_tts,
                segments,
                job_dir,
                on_log=log,
                on_prog=lambda p: progress("tts", 0.15 + p * 0.30, title=novel.title),
            )
            progress("pacing", 0.45, title=novel.title)

            plans = timed("4_pacing_final", stage_pacing, segments, durs, on_log=log)
            _write_json(job_dir / "plans.json", [_image_plan_to_dict(p) for p in plans])
            progress("images", 0.50, title=novel.title)

            images = timed(
                "6_images_final",
                stage_storyboard_and_image,
                plans,
                job_dir,
                character_analysis,
                on_log=log,
                on_prog=lambda p: progress("images", 0.50 + p * 0.38, title=novel.title),
            )
        progress("cover", 0.89, title=novel.title)
        cover = timed("7_cover", stage_cover, novel, segments, job_dir, on_log=log)
        progress("compose", 0.90, title=novel.title, cover=str(cover or ""))

        video = timed("8_compose", stage_compose, audios, durs, segments, plans, images, job_dir, on_log=log)
        youtube_url = ""
        if bool(config.upload_enabled):
            progress("upload", 0.96, title=novel.title, short_title=metadata.get("short_title", ""), video=str(video), cover=str(cover or ""))
            youtube_url = timed("9_upload", stage_upload, video, novel.title, cover, job_dir, metadata=metadata, on_log=log)

        performance = save_performance()

        result = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "video": str(video),
            "cover": str(cover or ""),
            "title": novel.title,
            "short_title": metadata.get("short_title", ""),
            "youtube_url": youtube_url,
            "performance": performance,
        }
        _write_json(job_dir / "result.json", result)
        progress(
            "completed",
            1.0,
            title=novel.title,
            short_title=metadata.get("short_title", ""),
            video=str(video),
            cover=str(cover or ""),
            youtube_url=youtube_url,
            worker_pid=None,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        start_next_queued_job(exclude_job_id=job_id, on_log=log)
        return result
    except Exception as exc:
        log(f"ERROR 失败: {exc}\n{traceback.format_exc()}")
        progress("failed", load_status(job_id).get("progress", 0), error=str(exc), worker_pid=None, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        raise


def _prompt_for_plan(llm: LLMBackend, plan: ImagePlan, on_log: LogFn, index: int) -> str:
    if config.llm_api_key or config.llm_provider == "ollama":
        try:
            with external_api_slot(action=f"storyboard {index}"):
                return _policy_safe_image_prompt(llm.storyboard(plan.text), fallback_text=plan.text)
        except Exception as exc:
            on_log(f"  WARN storyboard {index} LLM failed: {exc}; using safe template fallback")
    return _fallback_storyboard_prompt(plan.text)


def _fallback_storyboard_prompt(text: str) -> str:
    excerpt = re.sub(r"\s+", " ", str(text or "")).strip()
    excerpt = re.sub(r"[\"'`<>#]", " ", excerpt)[:120]
    prompt = (
        "Cinematic anime illustration for a novel recap scene. "
        "Show adult-looking characters in symbolic emotional drama, expressive faces, "
        "clean composition, soft lighting, no readable text. "
        f"Story mood reference: {excerpt}. {config.llm_image_style_suffix}"
    )
    return _policy_safe_image_prompt(prompt, fallback_text="")


def _plan_signature(plans: list[ImagePlan]) -> list[list[int]]:
    return [[int(seg.index) for seg in plan.segments] for plan in plans]


def _copy_prefetched_images(paths: list[Path], prefetch_dir: Path, job_dir: Path) -> list[Path]:
    img_dir = job_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    final_paths: list[Path] = []
    for i, src in enumerate(paths):
        suffix = src.suffix if src.suffix else ".png"
        dst = img_dir / f"img_{i:05d}{suffix}"
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        final_paths.append(dst)
    prompts = prefetch_dir / "prompts.json"
    if prompts.exists():
        shutil.copy2(prompts, job_dir / "prompts.json")
    try:
        shutil.rmtree(prefetch_dir)
    except OSError:
        pass
    return final_paths


def _estimated_speech_duration(text: str) -> float:
    compact = "".join(text.split())
    return max(1.2, min(45.0, len(compact) / 4.2))


def _create_silence(out_path: Path, duration: float):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ffmpeg_slot(action="silent audio"):
        subprocess.run(
            [
                ffmpeg_path(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(out_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_novel(path: Path, novel: Novel):
    _write_json(
        path,
        {
            "site": novel.site,
            "id": novel.novel_id,
            "title": novel.title,
            "author": novel.author,
            "description": novel.description,
            "chapters": [{"index": c.index, "title": c.title, "text": c.text} for c in novel.chapters],
        },
    )


def _image_plan_to_dict(plan: ImagePlan) -> dict:
    return {
        "image_index": plan.image_index,
        "duration": plan.duration,
        "segments": [{"index": s.index, "text": s.text} for s in plan.segments],
    }
