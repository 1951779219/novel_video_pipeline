# Codex 自动维护任务

目标：像交给 Codex 的日常自动化一样，每天查看反馈仓库，进行多智能体分析，安全修复明确问题，并在验证通过后发布在线更新。

## 工作流

1. 扫描 `1951779219/novel_video_pipeline_feedback` 的 open issues。
2. 优先处理：
   - 新增或最近更新的问题
   - 未分诊或 `needs:repro`
   - `priority:P0` / `priority:P1`
3. 读取私有源码仓库中的 `[反馈巡检] YYYY-MM-DD` 报告。
4. 使用多智能体分工：
   - 分诊：判断是否 actionable，归类优先级。
   - 诊断：定位可能模块，寻找最小复现和相关日志。
   - 修复：只做最小安全改动，保留现有用户配置和任务数据。
   - 验证：运行 `python -B -m py_compile ...` 或更强测试。
   - 发布：验证通过后 bump patch version、更新 `CHANGELOG.md`、推 tag，等待 release workflow 成功。
5. 用中文给出每日简报：新/变更问题、优先级、已处理或候选修复、测试结果、需要确认的事项。

## 自动发布规则

允许自动发布：

- 明确 bug，能在本地用小范围改动修复。
- 文档、反馈入口、更新检查、打包脚本这类低风险变更。
- 已运行验证，并且工作区没有无关改动。

暂停并请求确认：

- Issue 缺少复现信息或日志。
- 修复会删除/迁移用户数据。
- 修复会改变账号、token、YouTube 上传、付费 API 调用或大范围生成逻辑。
- 验证失败，或只能靠猜测修。

## 建议的 Codex 自动化提示词

```text
每天检查 F:\Manao\novel_video_pipeline 和 GitHub 反馈仓库 1951779219/novel_video_pipeline_feedback。

请像多智能体维护任务一样工作：
1. 分诊智能体：查看 open issues，优先 new/unlabeled/recently updated/P0/P1/needs:repro。
2. 诊断智能体：读取相关代码、日志线索和当天 [反馈巡检] 报告，判断是否可复现、是否可安全修复。
3. 修复智能体：只对明确、低风险、可验证的问题做最小代码改动；不要改动无关文件。
4. 验证智能体：运行 py_compile 或相关测试，记录命令和结果。
5. 发布智能体：如果修复验证通过，更新 app/version.py 和 CHANGELOG.md，提交、推送 main，创建 v* tag，等待 release workflow 成功，确认 latest.json 指向新版本。

如果没有 actionable issue，请输出简短中文报告：无新问题、无候选修复、无需用户确认。
如果问题需要确认，请不要猜测修复，列出需要用户补充的信息。
最终用中文汇报：新/变更问题、优先级、已处理或候选修复、测试结果、是否已发布更新。
```
