# 小说推文长视频流水线

把小说/推文文本变成长视频：内容抓取 -> 清洗切片 -> TTS -> 分镜出图 -> 字幕 -> 竖屏视频 -> 可选上传 YouTube。

当前版本重点增强了三件事：

- 国内内容来源：内置晴天/番茄聚合接口，支持番茄、七猫、书旗、塔读、QQ 阅读等聚合来源搜索与抓取。
- 阅读/Legado 兼容：提供 `legado_static` 轻量解析器，可导入普通静态 JSON 书源；复杂 `<js>` 书源由专用聚合引擎处理。
- 长视频稳定性：后台任务、`status.json`、`log.txt`、已有音频/图片复用、长视频 ffmpeg 单次图片清单合成、内嵌 ASS + 外置 SRT 字幕。

## 启动

双击：

```bat
启动.bat
```

或在项目目录运行：

```bat
python -m app.webui
```

生产使用建议启动桌面 GUI：

```bat
桌面GUI.bat
```

桌面 GUI 面向工作人员批量生产：支持番茄搜索加入任务、粘贴文章、导入多个 TXT、查看每个任务实时日志、保存 TTS/图片/节奏/字幕/上传配置、启动多个待处理任务。任务仍然走独立 worker 进程和 manifest 续跑机制。

打开：

```text
http://127.0.0.1:7860
```

如果电脑开了系统代理，启动脚本会自动设置 `NO_PROXY=localhost,127.0.0.1,::1`，避免 Gradio 自检 localhost 时被代理拦截。

## 内容来源

### 聚合书源

默认来源引擎是 `qingtian`。搜索框支持：

```text
十日终焉
x:十日终焉@番茄
t:十日终焉@七猫
m:十日终焉@番茄
d:短剧名@番茄
```

搜索后复制或选择 `qingtian://...` 引用，完整流水线可直接使用。

内置线路来自用户给的阅读书源结构，默认会优先尝试：

- `https://v1.gyks.cf`
- `http://219.154.201.122:5006`
- `https://api.langge.cf`

不会绕过登录、验证码、禁止访问或反爬限制；如果某线路不可用，会切换备用线路。

### Legado 静态书源

`legado_static` 支持普通 CSS/JSON 规则书源，例如包含：

- `bookSourceName`
- `bookSourceUrl`
- `searchUrl`
- `ruleSearch`
- `ruleBookInfo`
- `ruleToc`
- `ruleContent`

在“配置 -> 抓取来源 -> 普通 Legado JSON 文件路径或 URL”里填入路径或 URL。

复杂 `<js>` 书源不能用通用静态解析器直接执行，应使用 `qingtian` 或后续新增的专用适配器。

## 长视频建议

制作十几小时到四十小时以上的视频时，建议保持：

- `长视频稳定模式`: 开启
- `内嵌字幕`: 开启
- `同时导出 SRT`: 开启
- `短视频 Ken Burns 动效`: 关闭或让长视频模式自动关闭
- `TTS 失败后用静音占位`: 开启

长视频模式不会为每张图生成单独 mp4 片段，而是用 concat 图片清单一次性合成，减少临时文件数量和崩溃概率。

## 续跑与排错

每次完整流水线会生成：

```text
data/jobs/{job_id}/
  status.json
  log.txt
  novel.json
  segments.json
  durations.json
  prompts.json
  subtitle.ass
  subtitle.srt
  final.mp4
```

如果任务中断，在“完整流水线”里填入同一个 `job_id` 到“续跑 job_id”，再次启动即可。已有音频和图片会自动复用。

## 主要目录

```text
app/
  scrapers/
    qingtian.py       国内聚合接口
    legado.py         普通 Legado 静态 JSON 书源
    syosetu.py        日本 Syosetu
  backends/
    tts.py
    llm.py
    image.py
  stages/
    stage2_clean.py
    stage_pacing.py
    stage6_compose.py
  pipeline_runner.py
  webui.py
data/jobs/
```

## 稳定性增强

当前版本已吸收 HUANCUN MVP 中最适合本项目的轻量机制：

- 完整流水线从 WebUI 派发为独立 worker 进程，WebUI 关闭或重启时，已派发的长任务不会因为 Gradio 线程结束而立刻丢失。
- `data/runtime/locks/` 提供跨进程资源锁，默认普通电脑只允许 1 个 FFmpeg 任务并发，外部 API 默认 2 路，避免多任务抢满 CPU、内存和接口额度。
- `data/jobs/{job_id}/audio/tts_manifest.json` 记录每段 TTS 的文本 hash、配置 hash、文件指纹和状态，续跑时优先复用已完成音频。
- `data/jobs/{job_id}/compose_manifest.json` 记录音频拼接和最终合成输入 hash，合成失败不会覆盖已有 `final.mp4`。

普通配置电脑建议保持：

```text
Max FFmpeg slots = 1
Max external API slots = 1-2
Long video stable mode = enabled
Ken Burns = disabled for very long videos
```

FFmpeg 会按顺序查找：`MEDIA_FFMPEG_BIN` / `MEDIA_FFPROBE_BIN` 环境变量、系统 PATH、项目内 `runtime/ffmpeg`、`tools/ffmpeg`、`tools/ffmpeg/bin`、`vendor/ffmpeg`，最后才兼容旧的 `F:\Manao\drama_pipeline_release\tools\ffmpeg`。给普通电脑发包时，把 `ffmpeg.exe` 和 `ffprobe.exe` 放进上述任一项目内目录即可。

## YouTube 自动上传

生成后自动上传可在桌面 GUI 的“流水线配置 -> 批量与上传”里开启。当前浏览器上传优先复用：

```text
F:\Manao\drama_pipeline_app\app\stages\stage5_upload_browser.py
```

保留 `simple/full` 两种流程；`full` 流程会按配置尝试上传政策、广告位间隔、广告起始时间和公开范围。使用前需要 Chrome 调试端口 `9222` 可用，并已登录 YouTube Studio。

## 封面生成

完整流水线会在 `data/jobs/{job_id}/cover/` 里生成：

```text
cover_prompt.txt
cover_raw.png
cover.jpg
cover_manifest.json
```

封面提示词会自动带入当前任务标题 `{title}` 和正文摘要 `{excerpt}`，并要求图片模型不要直接绘制文字；最终标题由程序稳定压在图片底部，避免 AI 出图标题乱码。YouTube 自动上传时会优先使用 `cover.jpg`。

## API 检测

桌面 GUI 的配置页提供 TTS、分镜 LLM、图片、封面四组检测按钮。检测会优先访问 `/models` 或对应平台的模型/声音列表；分镜 LLM 在填写模型名后会做一次极小 `chat/completions` 测试。图片和封面检测默认不实际生成图片，避免误消耗出图额度。识别到候选模型且当前模型框为空时，GUI 会自动填入一个候选值。

检测到模型列表后，GUI 会把模型输入框切换成可选下拉列表，同时仍允许手动输入自定义模型名，方便使用中转站或私有模型别名。

图片 Provider 选择 `openai` 或 `custom` 时，会按 OpenAI 兼容格式调用：

```text
POST {base_url}/images/generations
```

如果一个任务已经生成过图片，流水线会复用 `data/jobs/{job_id}/images`，不会再次调用生图 API。更换中转站、模型或提示词后，先在任务队列点击“清除图片缓存”，再重新启动任务。

## 任务清理

桌面 GUI 的任务队列提供：

- 删除选中任务
- 清空已结束任务
- 清除图片缓存

“清除图片缓存”会删除选中任务的图片、封面和最终视频，保留抓取文本与 TTS 结果，适合重新调用生图 API 或重新合成。

## 标题与说明模板

流水线会从小说简介或正文开头自动生成一个 20-30 字左右的短说明标题，并写入 `data/jobs/{job_id}/metadata.json`。上传标题模板和说明模板都支持这些变量：

```text
{title}
{short_title}
{intro}
{author}
{tags}
{job_id}
```

例如标题模板可以写成 `{title}｜{short_title} #小说推文`，说明模板可以写成 `今日推荐：{short_title}\n\n{intro}\n\n{tags}`。

## 画面动作与字幕

图片动作支持上下、左右、单向推进、轻微缩放、静态和随机。启用移动时，每张图的运动会按该图实际持续时间走完整条曲线；例如 600 秒一张图，就是 600 秒内缓慢移动完成。默认曲线为 `ease`，会慢起慢停；也可以切到 `linear`。

最终视频固定内嵌 ASS 字幕，同时可选导出 SRT。字幕在最终 FFmpeg 合成阶段一次性烧录进视频，不是边生成边压字幕；这种方式适合长视频，内存占用相对稳定。GUI 中可配置字幕位置、字体、字号、颜色、描边、阴影、边距、每行字数和行数，并提供预览按钮。
