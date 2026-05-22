"""GitHub Issue auto triage for user feedback."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request


LABELS = {
    "priority:P0": ("b60205", "立即处理：无法启动、数据损坏、安全或大面积不可用"),
    "priority:P1": ("d93f0b", "高优先级：核心生产流程失败或稳定复现"),
    "priority:P2": ("fbca04", "中优先级：影响明确但有绕过方案"),
    "priority:P3": ("0e8a16", "低优先级：体验、文案、轻微改进"),
    "type:bug": ("d73a4a", "缺陷报告"),
    "type:feature": ("a2eeef", "功能建议"),
    "type:question": ("d4c5f9", "问题或咨询"),
    "needs:repro": ("ededed", "需要复现步骤、日志或截图"),
    "needs:triage": ("cfd3d7", "等待人工确认"),
    "area:scraper": ("bfdadc", "小说搜索、抓取、书源"),
    "area:tts": ("bfd4f2", "TTS 配音"),
    "area:image": ("d7c7ff", "分镜、生图、封面"),
    "area:video": ("c2e0c6", "视频合成、字幕、FFmpeg"),
    "area:upload": ("fef2c0", "YouTube 上传"),
    "area:update": ("d4c5f9", "在线更新、发布包"),
    "area:ui": ("f9d0c4", "界面、批量操作、配置"),
}


def _load_issue() -> dict:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8-sig") as handle:
            event = json.load(handle)
        if "issue" in event:
            return event["issue"]
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    raise SystemExit("No issue payload found")


def classify(issue: dict) -> list[str]:
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    text = f"{title}\n{body}".lower()
    labels: list[str] = []

    if re.search(r"bug|错误|报错|失败|崩溃|闪退|无法|不能|exception|traceback|crash|error|failed|卡住", text):
        labels.append("type:bug")
    elif re.search(r"建议|希望|增加|优化|改进|feature|enhancement|request|能不能|可不可以", text):
        labels.append("type:feature")
    else:
        labels.append("type:question")

    if re.search(r"无法启动|打不开|启动失败|数据丢失|文件损坏|安全|泄露|所有人|全量|crash on startup|data loss|security", text):
        labels.append("priority:P0")
    elif re.search(r"抓取失败|搜索失败|tts|配音失败|生图失败|合成失败|字幕失败|ffmpeg|上传失败|更新失败|稳定复现|blocking|blocker|核心流程", text):
        labels.append("priority:P1")
    elif "type:bug" in labels:
        labels.append("priority:P2")
    else:
        labels.append("priority:P3")

    area_patterns = [
        ("area:scraper", r"抓取|搜索|书源|章节|番茄|七猫|legado|syosetu|qingtian"),
        ("area:tts", r"tts|配音|语音|音色|edge-tts|voicevox|azure|elevenlabs"),
        ("area:image", r"生图|图片|封面|comfyui|sdwebui|prompt|分镜"),
        ("area:video", r"合成|视频|字幕|ffmpeg|ffprobe|ass|srt|final\.mp4"),
        ("area:upload", r"上传|youtube|studio|浏览器|chrome|playwright"),
        ("area:update", r"在线更新|更新失败|update|latest\.json|release|下载更新包"),
        ("area:ui", r"界面|按钮|配置|批量|gui|webui|gradio"),
    ]
    for label, pattern in area_patterns:
        if re.search(pattern, text):
            labels.append(label)

    if "type:bug" in labels and not re.search(r"步骤|复现|日志|截图|版本|traceback|log|status\.json|job_id", text):
        labels.append("needs:repro")
    return _dedupe(labels)


def _dedupe(labels: list[str]) -> list[str]:
    seen = set()
    result = []
    for label in labels:
        if label not in seen:
            seen.add(label)
            result.append(label)
    return result


def _api(method: str, path: str, payload: dict | None = None):
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GITHUB_TOKEN/GITHUB_REPOSITORY missing, dry-run only")
        return None
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 422}:
            return None
        raise


def ensure_label(name: str) -> None:
    color, description = LABELS.get(name, ("ededed", ""))
    _api("POST", "/labels", {"name": name, "color": color, "description": description})


def apply_labels(issue_number: int, labels: list[str]) -> None:
    for label in labels:
        ensure_label(label)
    _api("POST", f"/issues/{issue_number}/labels", {"labels": labels})


def main() -> int:
    issue = _load_issue()
    labels = classify(issue)
    issue_number = int(issue.get("number") or os.getenv("ISSUE_NUMBER") or 0)
    print("labels=" + ",".join(labels))
    if issue_number:
        apply_labels(issue_number, labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
