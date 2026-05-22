"""Daily feedback survey for the novel video pipeline.

This script reads public feedback issues, groups the actionable ones, and
creates a concise maintenance report issue in the private source repository.
It intentionally does not edit code. Code repair is handled by the Codex
desktop automation so fixes can be reproduced, tested, committed, and released
with the same guardrails as an interactive run.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

try:
    from github_issue_triage import classify
except ImportError:
    from tools.github_issue_triage import classify  # type: ignore


FEEDBACK_REPO = os.getenv("FEEDBACK_REPO", "1951779219/novel_video_pipeline_feedback")
SOURCE_REPO = os.getenv("SOURCE_REPO") or os.getenv("GITHUB_REPOSITORY") or "1951779219/novel_video_pipeline"
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24") or "24")
CREATE_EMPTY_REPORT = os.getenv("CREATE_EMPTY_REPORT", "").lower() in {"1", "true", "yes"}
REPORT_LABEL = "automation:feedback"


LABELS = {
    REPORT_LABEL: ("5319e7", "每日反馈巡检报告"),
    "type:maintenance": ("c5def5", "自动维护任务"),
}


@dataclass
class IssueSummary:
    number: int
    title: str
    url: str
    updated_at: str
    labels: list[str]
    inferred_labels: list[str]
    priority: str
    issue_type: str
    area: str
    needs_repro: bool
    body: str


def _token() -> str:
    return os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""


def _api(method: str, path: str, payload: dict[str, Any] | None = None, *, repo: str | None = None):
    token = _token()
    target_repo = repo or SOURCE_REPO
    url = f"https://api.github.com/repos/{target_repo}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "novel-video-feedback-maintainer/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        if exc.code in {404, 422}:
            print(f"[warn] GitHub API {method} {path} returned {exc.code}: {details[:300]}")
            return None
        raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {details[:500]}") from exc


def _paged(path: str, *, repo: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 6):
        batch = _api("GET", f"{path}{separator}per_page=100&page={page}", repo=repo)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(item.get("name", "")) for item in issue.get("labels") or [] if item.get("name")]


def _best(prefix: str, labels: list[str], fallback: str) -> str:
    return next((label for label in labels if label.startswith(prefix)), fallback)


def _summarize(issue: dict[str, Any]) -> IssueSummary:
    existing = _label_names(issue)
    inferred = classify(issue)
    combined = existing + [label for label in inferred if label not in existing]
    return IssueSummary(
        number=int(issue.get("number") or 0),
        title=str(issue.get("title") or "").strip(),
        url=str(issue.get("html_url") or ""),
        updated_at=str(issue.get("updated_at") or ""),
        labels=existing,
        inferred_labels=inferred,
        priority=_best("priority:", combined, "priority:P3"),
        issue_type=_best("type:", combined, "type:question"),
        area=_best("area:", combined, "area:unknown"),
        needs_repro=("needs:repro" in combined),
        body=str(issue.get("body") or "").strip(),
    )


def _is_relevant(issue: dict[str, Any], cutoff: dt.datetime) -> bool:
    if "pull_request" in issue:
        return False
    labels = _label_names(issue)
    updated_at = _parse_time(str(issue.get("updated_at") or issue.get("created_at")))
    if updated_at >= cutoff:
        return True
    if not labels or "needs:triage" in labels or "needs:repro" in labels:
        return True
    if "priority:P0" in labels or "priority:P1" in labels:
        return True
    return False


def fetch_relevant_issues() -> list[IssueSummary]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    issues = _paged("/issues?state=open&sort=updated&direction=desc", repo=FEEDBACK_REPO)
    return [_summarize(issue) for issue in issues if _is_relevant(issue, cutoff)]


def _sort_key(issue: IssueSummary) -> tuple[int, str]:
    rank = {"priority:P0": 0, "priority:P1": 1, "priority:P2": 2, "priority:P3": 3}
    return rank.get(issue.priority, 9), issue.updated_at


def _excerpt(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:limit].rstrip() + ("..." if len(compact) > limit else "")


def build_report(issues: list[IssueSummary], *, today: str) -> str:
    lines = [
        f"# 每日反馈巡检 {today}",
        "",
        f"- 反馈仓库: `{FEEDBACK_REPO}`",
        f"- 源码仓库: `{SOURCE_REPO}`",
        f"- 扫描范围: open issues, 最近 {LOOKBACK_HOURS} 小时更新、未分诊、需复现或 P0/P1",
        f"- 发现需关注问题: {len(issues)}",
        "",
    ]
    if not issues:
        lines.extend(
            [
                "## 结论",
                "",
                "没有发现需要处理的新问题或高优先级问题。",
                "",
                "## 候选修复",
                "",
                "无。",
                "",
                "## 需要确认",
                "",
                "无。",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    issues = sorted(issues, key=_sort_key)
    lines.extend(["## 分诊智能体", ""])
    for item in issues:
        needs = "，需要复现信息" if item.needs_repro else ""
        lines.append(f"- {item.priority} {item.issue_type} {item.area}{needs}: #{item.number} {item.title}")
    lines.extend(["", "## 诊断智能体", ""])
    for item in issues:
        labels = ", ".join(item.labels or item.inferred_labels)
        lines.append(f"- #{item.number} [{item.title}]({item.url})")
        lines.append(f"  标签: {labels or '无'}")
        lines.append(f"  摘要: {_excerpt(item.body) or '无正文'}")
    lines.extend(["", "## 修复智能体", ""])
    for item in issues:
        if item.needs_repro:
            action = "暂不自动修复，先向用户索取复现步骤、日志、截图或 job_id。"
        elif item.priority in {"priority:P0", "priority:P1", "priority:P2"} and item.issue_type == "type:bug":
            action = "候选自动修复：本地复现、定位最小改动、运行 py_compile，验证后提交修复分支。"
        elif item.issue_type == "type:feature":
            action = "候选产品拆解：整理需求范围，必要时转为实现 issue，暂不直接发布。"
        else:
            action = "候选答复：补充使用说明或关闭重复问题。"
        lines.append(f"- #{item.number}: {action}")
    lines.extend(["", "## 发布智能体", ""])
    lines.append("- 只有通过本地验证的代码修复才允许 bump patch version、更新 CHANGELOG、推 `v*` tag。")
    lines.append("- 文档、配置和低风险脚本可自动发布；涉及删除数据、账号、上传、付费接口、批量文件操作时必须等待确认。")
    lines.extend(["", "## 需要确认", ""])
    pending = [item for item in issues if item.needs_repro or item.priority == "priority:P0"]
    if pending:
        for item in pending:
            lines.append(f"- #{item.number}: 请确认复现信息或影响范围。")
    else:
        lines.append("无。")
    return "\n".join(lines).rstrip() + "\n"


def ensure_label(name: str) -> None:
    color, description = LABELS.get(name, ("ededed", ""))
    _api("POST", "/labels", {"name": name, "color": color, "description": description}, repo=SOURCE_REPO)


def find_existing_report(title: str) -> int | None:
    for issue in _paged("/issues?state=open&sort=created&direction=desc", repo=SOURCE_REPO):
        if str(issue.get("title") or "") == title:
            return int(issue.get("number") or 0)
    return None


def publish_report(title: str, body: str, *, force: bool) -> None:
    if not force and "发现需关注问题: 0" in body:
        print(body)
        print("[feedback_daily] no actionable issue; skipped creating an empty report issue")
        return
    for label in (REPORT_LABEL, "type:maintenance"):
        ensure_label(label)
    existing = find_existing_report(title)
    payload = {"title": title, "body": body, "labels": [REPORT_LABEL, "type:maintenance"]}
    if existing:
        _api("PATCH", f"/issues/{existing}", {"body": body}, repo=SOURCE_REPO)
        print(f"[feedback_daily] updated report issue #{existing}")
    else:
        created = _api("POST", "/issues", payload, repo=SOURCE_REPO)
        number = created.get("number") if isinstance(created, dict) else "?"
        print(f"[feedback_daily] created report issue #{number}")


def main() -> int:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    title = f"[反馈巡检] {today}"
    issues = fetch_relevant_issues()
    body = build_report(issues, today=today)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    if "--dry-run" in sys.argv:
        print(body)
        return 0
    publish_report(title, body, force=CREATE_EMPTY_REPORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
