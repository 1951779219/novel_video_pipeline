# GitHub 发布与在线更新

本项目按“短剧流水线”的方式发布：

1. 代码仓库：`1951779219/novel_video_pipeline`。
2. 公开更新仓库：`1951779219/novel_video_pipeline_release`。
3. 公开反馈仓库：`1951779219/novel_video_pipeline_feedback`。
4. 推送 `v*` 标签后，`.github/workflows/release.yml` 会自动打包更新包，并把以下资产发布到公开更新仓库：
   - `novel_video_pipeline_v<版本号>_update.zip`
   - `latest.json`

客户端默认读取：

```text
https://github.com/1951779219/novel_video_pipeline_release/releases/latest/download/latest.json
```

发布新版本时：

```powershell
git status
git add .
git commit -m "Release v1.0.1"
git tag v1.0.1
git push origin main
git push origin v1.0.1
```

注意：

- 不要把 `data/`、`runtime/ffmpeg/*.exe`、`__pycache__/` 提交到仓库。
- `app/version.py` 里的 `VERSION` 要与发布标签对应。
- 更新包会保留用户本地的 `data/settings.json`、任务记录和运行数据。
- 用户反馈从软件里的“提交反馈”入口进入公开反馈仓库，issue 会由 `.github/workflows/issue_triage.yml` 自动分类和标优先级。
