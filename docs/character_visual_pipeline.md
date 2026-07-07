# 人设图与主题锁定改版设计

## 目标

把“导入文章 -> 自动分析人设/剧情 -> 生成固定人设图 -> 按角色名/触发词生成推文画面”做成一个可复用流水线。

核心原则：

- 一个任务只锁定一种画面类型，例如武侠、魔幻、日漫、都市、悬疑。
- 每个任务生成自己的角色卡和参考图，不污染全局配置。
- 角色外观先分析并固定，后续每张图都复用同一套外观描述和触发词。
- 图生图/中转站作为可插拔后端，不默认消耗付费 API。

## 当前已落地

0. Unified Image API routing

All image surfaces now resolve through one `ImageBackend` abstraction:

- Scene images use `image_provider`, `image_base_url`, `image_api_key`, `image_model`, and `image_workflow`.
- Covers default to `same_as_image`, with optional cover-specific overrides.
- Character reference images default to `same_as_image`, with optional character-reference-specific OpenAI/custom/ComfyUI/SDWebUI overrides.
- Scene reference images are opt-in; when enabled, generated character images can be sent as reference images for scene generation.

Supported routes:

- `comfyui`: local ComfyUI `/prompt` with an API workflow.
- `openai`: OpenAI-compatible image generation.
- `custom`: OpenAI-compatible relay for other image software.
- `sdwebui`: `/sdapi/v1/txt2img`, and `/sdapi/v1/img2img` when references are enabled.

OpenAI/custom reference-image mode calls:

```text
POST {base_url}/images/edits
multipart fields: model, prompt, size, image
```

This lets character reference images use an OpenAI-compatible relay, and lets later scene images send those reference images to the same or another relay.

1. 自动剧情与人设分析

开启 `character_analysis_enabled` 后，清洗文章后会生成：

```text
data/jobs/{job_id}/character_profiles.json
```

内容包含：

- `plot_summary`
- `story_conflict`
- `visual_theme`
- `protagonists`
- `supporting_characters`
- `characters`
- 每个角色的 `trigger`、`visual_prompt_en`、`reference_prompt_en`

2. 任务主题锁定

分析结果包含 `visual_theme`：

- `genre`
- `theme_name_zh`
- `style_prompt_en`
- `background_prompt_en`
- `negative_prompt_en`

开启 `scene_inject_visual_theme` 后，所有场景图 prompt 都会注入同一任务主题。

3. 人设参考图

开启 `character_reference_enabled` 后，会为主角和重要配角生成参考图：

```text
data/jobs/{job_id}/characters/{trigger}.png
data/jobs/{job_id}/character_reference_manifest.json
```

默认关闭。Provider 可选：

- `same_as_image`
- `placeholder`
- `comfyui`
- `sdwebui`
- `openai`
- `replicate`
- `aliyun`
- `custom`

如果当前图片 Provider 是本地 ComfyUI/SDWebUI，`same_as_image` 就会走本地生成。

4. 场景图人设注入

开启 `scene_inject_character_triggers` 后，每张图的 `prompts.json` 会记录：

- `theme_context`
- `character_context`
- 注入后的最终 `prompt`

角色匹配逻辑基于角色名和别名；单字代词会过滤，避免“他/她/我”导致全片误匹配。

## 下一阶段：图生图中转站

当前阶段已经生成参考图和触发词，但普通文生图后端不会真正“看见”参考图。下一步需要新增一个图生图中转站接口：

```text
scene text + visual_theme + character reference image(s) + trigger(s)
  -> relay / ComfyUI workflow / img2img backend
  -> final scene image
```

建议支持两种后端：

- ComfyUI API workflow：把参考图送入 IPAdapter、InstantID、PuLID、FaceID 或角色一致性工作流。
- OpenAI-compatible image edit relay：如果中转站支持 `/images/edits`，上传参考图和 prompt。

为了避免破坏现有流程，图生图中转站应作为新 Provider 或新开关，不替换现有 `ImageBackend.generate()`。

## 后续分镜改版

可参考文镜画师已拆出的配置，分成三个层：

1. 文案层：原文、GPT 校对版、分镜文本、15 秒/6 分镜短视频脚本。
2. 画面层：角色、场景、道具、中文提示词、英文提示词、视频提示词、音效提示词。
3. 时间轴层：图片、配音、字幕、时长、镜头运动、成品输出。

建议先新增 job artifact，不直接改 `data/settings.json`：

```text
story_analysis.json
character_profiles.json
character_reference_manifest.json
scene_boards.json
prompts.json
```

## 风险边界

- 不自动读取 Cookie。
- 不默认调用第三方付费云 API。
- 不把参考图写入全局配置。
- 不删除或迁移已有 `data/jobs/**`。
- 图生图中转站接入前，需要确认具体 API 格式或 ComfyUI workflow 输入节点。
