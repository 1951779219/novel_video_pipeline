# 小说推文图片提示词模板

这些模板已按小说推文视频场景改写，可复制到 GUI 的“图片与提示词 / 封面”配置里继续微调。

## 封面固定模板

```text
Create ONE finished premium YouTube novel-recap cover, like a conceptual typography movie poster.
Exact title text: "{title}"
The exact title must be the main hero element: huge, readable, spelled exactly, not translated, not shortened, not replaced, and integrated into the artwork.
Story excerpt: {excerpt}
Custom direction: {custom}
Composition: one strong hero visual; the character, object, or scene should interact with the title typography by overlapping it, emerging from it, being framed by it, casting shadow on it, or breaking through it. Use a restrained 4-6 color palette, high contrast, cinematic lighting, and clear thumbnail hierarchy. Single cover only: no moodboard, no grid, no process sheet, no mockup, no extra captions, no UI, no watermark. {style}
```

## 封面自定义提示词示例

悬疑反转：

```text
viral suspense novel recap thumbnail, shocked character close-up, symbolic clue object, red warning accent, deep shadow background, high click-through tension
```

恋爱虐心：

```text
emotional romance novel recap thumbnail, two characters separated by rain and glass reflection, soft neon pink and blue, cinematic tearful expression, premium dramatic poster
```

逆袭爽文：

```text
powerful revenge comeback novel recap thumbnail, protagonist stepping from darkness into golden light, shattered contract papers, bold contrast, heroic cinematic poster
```

古风权谋：

```text
ancient court intrigue novel recap thumbnail, elegant royal silhouette, jade seal and palace shadows, ink-wash texture mixed with cinematic realism, restrained red and gold palette
```

## 视频图通用前缀

动漫推文：

```text
Single finished cinematic anime/editorial illustration for a novel recap video, one clear hero subject, visible story action, strong visual hierarchy, expressive adult-looking characters, dramatic lighting, restrained 4-6 color palette, clean composition, no readable text.
```

写实电影：

```text
Single finished cinematic film still for a novel recap video, one clear hero subject, visible story action, natural imperfections, realistic materials, expressive adult-looking characters, 50mm lens feel, dramatic practical lighting, restrained color palette, no readable text.
```

## 视频图 AI 分析用户模板

```text
Task: turn this novel excerpt into a single narrative storyboard image prompt for a recap video.
Common visual prefix that must be preserved: {prefix}
Style suffix: {style}
Scene index: {index}

Extract these visual decisions before writing the prompt: main event/action, main character, conflict hook, camera angle, lighting, mood, palette, and one strong symbolic detail. If a character/theme lock is supplied later in the pipeline, keep it visually compatible and do not contradict it.

Novel excerpt:
{text}

Return one English image prompt only, <=90 words. No explanation, no labels, no JSON.
```

## 人设分析提示词

```text
你是小说推文视频的剧情解析和人设导演。
任务：阅读导入文章，自动区分主角、重要配角、路人，并为后续生图锁定固定人设。

必须只输出 JSON，不要 Markdown，不要解释。
如果原文没有明确外观，请合理补全一次，并在后续保持一致；不要写“可能”“推测”“未提及”。
人物外观必须适合全年龄推文画面，避免血腥、裸露、色情、儿童危险、真实名人、商标水印。
主角和重要配角必须有稳定的 hair / outfit / visual_prompt_en，后续每张图都会复用。
```

输出 JSON 里最重要的字段是 `visual_theme.style_prompt_en`、`visual_theme.background_prompt_en`、`characters[].visual_prompt_en` 和 `characters[].reference_prompt_en`。剧情图阶段会把这些内容写进 `theme_context` / `character_context`，并记录在每个任务的 `prompts.json`。

## 可用变量

- `{title}`：任务标题，封面必须固定使用。
- `{excerpt}`：正文摘要，封面用来判断剧情气质。
- `{custom}`：封面自定义提示词。
- `{style}`：通用风格后缀。
- `{text}`：当前视频分镜对应的正文。
- `{index}`：当前分镜序号。
- `{prefix}`：视频图通用前缀。
- `{theme_context}`：预留变量；当前由图片阶段自动追加视觉主题锁。
- `{character_context}`：预留变量；当前由图片阶段自动追加人物一致性锁。
