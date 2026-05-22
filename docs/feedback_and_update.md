# 反馈与自动更新闭环

这个项目现在沿用短剧流水线的三仓库结构：

| 仓库 | 可见性 | 用途 |
| --- | --- | --- |
| `1951779219/novel_video_pipeline` | Private | 源码、发布 workflow |
| `1951779219/novel_video_pipeline_release` | Public | `latest.json` 和更新包 |
| `1951779219/novel_video_pipeline_feedback` | Public | 用户提交 BUG、建议和问题 |

用户反馈入口：

```text
https://github.com/1951779219/novel_video_pipeline_feedback/issues/new/choose
```

公开反馈仓库包含：

```text
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/ISSUE_TEMPLATE/config.yml
.github/workflows/issue_triage.yml
tools/github_issue_triage.py
```

Issue 创建、编辑、重开时会自动打：

- `type:bug` / `type:feature` / `type:question`
- `priority:P0` 到 `priority:P3`
- `needs:repro`
- `area:scraper` / `area:tts` / `area:image` / `area:video` / `area:upload` / `area:update` / `area:ui`

闭环流程：

1. 用户在软件里点“提交反馈”，打开公开反馈仓库。
2. 用户提交 issue。
3. GitHub Actions 自动分类、标优先级和模块。
4. 修复后修改 `app/version.py` 和 `CHANGELOG.md`，推送 `v*` 标签。
5. 发布 workflow 生成 `latest.json` 和更新 zip。
6. 软件读取 `latest.json`，用户在“软件更新”页下载并应用。
