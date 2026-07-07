# 文镜画师静态拆包分析

分析对象：`<文镜画师目录>\文镜画师.exe`

分析时间：2026-07-06

## 结论

`文镜画师.exe` 不是 Electron `app.asar` 包，也没有可直接吸收的本地 Python 逻辑。它更像是原生桌面程序 + WebView2 + SQLite + 本地 Web 资源的组合：

- 主程序内可见 `WebView2`、`web.zip`、`aigc.sqlite`、`spider*` 消息通道、SQLite 相关字符串。
- 同目录存在已展开的 `web/` 前端资源和 `aigc.sqlite`。
- `<文镜画师目录>\temp\web.zip` 可直接解压，内容与 `web/` 基本一致。
- PE 无 overlay，资源主要在 `.rsrc`，继续拆二进制的收益低于读取 `web/`、SQLite 和前端消息协议。

可吸收的是产品结构、提示词模板、推文分镜流程、项目数据模型和前端交互设计；不建议吸收其云服务调用、Cookie 读取、账号登录、付费 API、剪映路径或平台爬取实现。

## 已解出的本地资源

- `<文镜画师目录>\web\spider\index.html`：推文解析、授权、分镜、成品输出页面。
- `<文镜画师目录>\web\ui\index.html` 和 `<文镜画师目录>\web\ui\app.js`：视频项目主界面、批量文本处理、图片/视频提示词面板。
- `<文镜画师目录>\web\ui\app-data-cinematic.js`：约 259 个电影感图片提示词配置项。
- `<文镜画师目录>\web\ui\app-data-video-motion.js`：约 306 个视频运镜配置项。
- `<文镜画师目录>\web\test\index.html`：一键口播、一键视频替换、剪映草稿相关配置页面。
- `<文镜画师目录>\aigc.sqlite`：项目模型、设置、提示词模板表。

## 推文相关配置

`spider/index.html` 是最直接相关模块。它支持粘贴短链、分享码、分享文案，并识别抖音、TikTok、小红书、Bilibili、微博、YouTube、X/Twitter 等平台。页面流程包含：

- 授权页打开、Cookie 检测、清除 Cookie、代理/Cookie 手工输入。
- 解析结果、推文、日志、咒语、修改对比等标签页。
- 推文准备：选择演讲者、画面预设、语言、识别/分镜参数、GPT 文本纠错。
- 出分镜：从视频或 TXT 生成镜头，支持原始 TXT 和 GPT 修改版 TXT。
- 出成品：已有演讲者和镜头后调用主页成品流程。

对本项目可吸收的不是 Cookie/爬取逻辑，而是“推文准备 -> 文本校对 -> 分镜预览 -> 成品输出”的前置闭环。

## SQLite 数据模型

`aigc.sqlite` 有 21 张表，样例数据大多为空，但模型有参考价值：

- `Videos` / `Infos`：完整视频项目配置，包含文案、提示词、风格、时代、视频模式、尺寸、字幕、BGM、工作流等。
- `Articles`：每个镜头/段落的角色、场景、物品、中文提示词、英文提示词、视频提示词、音效、图片/视频路径。
- `Audios` / `SRTs`：配音、字幕、时间轴。
- `Profiles` / `Roles` / `Speakers`：人物设定、角色、说话人。
- `WorkFlows` / `Resources` / `TRes`：工作流和资源管理。
- `LTXDirectors`：`TimeLineData`、`LocalPrompts`、`SegmentLengths`、`Duration`，适合参考到长视频/分镜时间轴。
- `Settings` / `Prompts`：提示词模板、ComfyUI、本地/云端模型、剪映、TTS、UI 皮肤等设置。

## 可吸收到 novel_video_pipeline 的候选

1. 推文任务前置面板

在当前“启动后台任务”前增加可选预处理：推文文本粘贴、字数/合并数量、是否 AI 校对、是否先出分镜预览。这样能把当前流水线从“直接跑长任务”升级为“先确认文本与分镜，再成片”。

2. 提示词模板分层

当前项目已有 `llm_storyboard_prompt`、`llm_image_style_suffix`、`llm_negative_prompt`。可新增可选模板：

- 人物设定抽取模板：要求输出稳定 JSON 人设。
- 静态底图镜头模板：提取角色、场景、道具、景别。
- 图生视频提示词模板：保持首帧一致，强调动作、视线、表情、镜头稳定。
- 15 秒/6 分镜短视频拆分模板：适合短剧/推文切片。

3. 电影感与运镜预设库

可把 `app-data-cinematic.js` 和 `app-data-video-motion.js` 的配置思想转成项目内 JSON 预设，而不是直接复制第三方前端：

- 图片侧：成片风格、镜头、光影、调色、质感、语言。
- 视频侧：场景类型、运镜动作、路径、节奏、稳定性、主体约束。

这能增强当前 `video_motion` 和图片风格后缀，不会改变核心流水线。

4. 批量文本操作

文镜主界面对文案、人物、场景、物品、中文提示词、英文提示词、视频提示词、音效提示词提供导入、导出、搜索、替换、删除。当前项目可先吸收为 job 级 artifact 编辑工具，避免直接动 `data/settings.json`。

5. 角色/说话人模型

当前项目偏向旁白 TTS。可参考 `Profiles`、`Roles`、`Speakers`，在未来支持“旁白 + 多角色台词”的配音分配，但这会影响生成结果，应作为单独功能开关。

6. 时间轴/分镜预览模型

`Articles`、`SRTs`、`Audios`、`LTXDirectors` 的组合可映射到当前 `segments.json`、`durations.json`、`prompts.json`、`subtitle.srt`。适合作为后续“分镜表/时间轴预览”功能的设计参考。

## 不建议吸收的部分

- Cookie 自动读取、保存、复用和平台授权页流程。
- 抖音/小红书/B站/YouTube/X 等平台解析或规避风控实现。
- 文镜/仙宫云/RH/无影等云 API 调用、部署、账号、token、key。
- 剪映用户路径、草稿路径和本机账号路径。
- `temp/webview2` 目录中的浏览器 profile、Cookie、Login Data 或缓存。
- 任何可能消耗付费额度或触发平台账号风控的自动调用。

## 建议落地顺序

1. 先做无副作用的“提示词模板与预设库”吸收，存为项目内默认 JSON，不触碰用户配置。
2. 再做“推文预处理/分镜预览”UI，默认关闭，输出到 job 目录 artifact。
3. 最后考虑角色/说话人/时间轴编辑，这会改变成片逻辑，应单独确认。

## 验证命令

本次没有运行目标 exe。只执行了静态读取：

```powershell
Get-ChildItem -LiteralPath '<文镜画师目录>' -Force
rg -n "推文|文案|分享|短链|分镜|口播|成品|Cookie|抖音|小红书|YouTube|运镜|提示词" '<文镜画师目录>\web'
python -  # 读取 PE section、抽取字符串、枚举 SQLite schema
```
