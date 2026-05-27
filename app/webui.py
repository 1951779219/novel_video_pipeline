"""Gradio WebUI entry point."""
from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")

import gradio as gr

from app.config import JOBS_DIR, config
from app import pipeline_runner as pr
from app.backends.tts import EDGE_JA_VOICES
from app.version import VERSION


ACTIVE_JOBS: dict[str, int] = {}

SOURCE_SITES = ["qingtian", "syosetu", "legado_static"]
MEDIA_TYPES = ["小说", "听书", "漫画", "短剧"]
PLATFORMS = [
    "番茄",
    "全部",
    "七猫",
    "书旗",
    "塔读",
    "QQ阅读",
    "69书吧",
    "顶点",
    "爱下电子书",
    "中华典藏",
]
LLM_PROVIDERS = ["openai", "claude", "deepseek", "gemini", "ollama", "custom"]
TTS_PROVIDERS = ["edge", "voicevox", "openai", "azure", "elevenlabs", "custom", "silent"]
IMAGE_PROVIDERS = ["placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"]
PACING_MODES = ["by_duration", "by_sentence", "by_paragraph", "fixed_count"]
VIS = ["PUBLIC", "UNLISTED", "PRIVATE"]

LLM_PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "claude": ("https://api.anthropic.com/v1", "claude-3-5-haiku-20241022"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-1.5-flash"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    "custom": ("", ""),
}
IMG_PRESETS = {
    "placeholder": "",
    "comfyui": "http://127.0.0.1:8188",
    "sdwebui": "http://127.0.0.1:7860",
    "openai": "https://api.openai.com/v1",
    "aliyun": "https://dashscope.aliyuncs.com",
    "replicate": "",
    "custom": "",
}

STORYBOARD_TEMPLATES = {
    "通用动漫": (
        "You convert novel paragraphs into one concise English image prompt. "
        "Capture setting, characters, mood, lighting, and action. "
        "Use anime illustration style. Output only the prompt."
    ),
    "写实电影": (
        "Convert the paragraph into one concise English prompt. "
        "Style: photorealistic cinematic still, dramatic lighting, 35mm film. "
        "Output only the prompt."
    ),
    "赛璐璐插画": (
        "Output one English image prompt. Style: cel-shaded anime illustration, vivid colors, clean lines."
    ),
    "水彩绘本": (
        "Output one English image prompt. Style: soft watercolor picture-book illustration, gentle palette."
    ),
}


def _save_settings(d: dict) -> str:
    config.update(d)
    config.save()
    return "已保存到 data/settings.json"


def _apply_source_settings(site, max_chars, chapter_limit, base_url, platform, media, hosts, delay, legado_ref):
    config.update(
        {
            "scraper_site": site,
            "scraper_max_chars": int(max_chars or 0),
            "scraper_chapter_limit": int(chapter_limit or 0),
            "source_base_url": base_url or "",
            "source_platform": platform or "番茄",
            "source_media": media or "小说",
            "source_hosts": hosts or "",
            "source_delay": float(delay or 0),
            "legado_source_ref": legado_ref or "",
        }
    )
    config.save()


def build_source_tab():
    with gr.Tab("内容来源"):
        gr.Markdown("### 搜索国内聚合/阅读书源")
        with gr.Row():
            site = gr.Dropdown(SOURCE_SITES, value=config.scraper_site, label="来源引擎")
            platform = gr.Dropdown(PLATFORMS, value=config.source_platform, label="平台")
            media = gr.Dropdown(MEDIA_TYPES, value=config.source_media, label="内容类型")
        with gr.Row():
            keyword = gr.Textbox(value="十日终焉", label="关键词。可用 x:小说名@番茄 / t:书名@七猫")
            limit = gr.Slider(1, 50, value=20, step=1, label="返回数量")
            search_btn = gr.Button("搜索", variant="primary")
        results = gr.Dataframe(
            headers=["序号", "书名", "作者", "来源", "字数", "最新章节", "引用"],
            datatype=["number", "str", "str", "str", "str", "str", "str"],
            label="搜索结果",
            wrap=True,
        )
        ref_choice = gr.Dropdown(label="选择引用", choices=[], interactive=True)
        picked_ref = gr.Textbox(label="流水线输入引用", interactive=True)
        gr.Markdown("聚合书源使用普通 HTTP 接口，不会绕过登录、验证码或禁止访问的页面。JS 书源可用专用聚合引擎；普通静态 Legado JSON 可用 legado_static。")

        def _search(site_v, platform_v, media_v, key, lim):
            try:
                items = pr.search_books(key, site=site_v, source=platform_v, media=media_v, limit=int(lim))
                rows = [
                    [i + 1, r.title, r.author, r.source, r.word_count, r.latest_chapter, r.ref]
                    for i, r in enumerate(items)
                ]
                choices = [r.ref for r in items]
                first = choices[0] if choices else ""
                return rows, gr.update(choices=choices, value=first), first
            except Exception as exc:
                return [[0, f"搜索失败: {exc}", "", "", "", "", ""]], gr.update(choices=[], value=None), ""

        search_btn.click(_search, [site, platform, media, keyword, limit], [results, ref_choice, picked_ref])
        ref_choice.change(lambda x: x or "", [ref_choice], [picked_ref])
        return picked_ref


def build_config_tab():
    with gr.Tab("配置"):
        with gr.Accordion("抓取来源", open=True):
            with gr.Row():
                scraper_site = gr.Dropdown(SOURCE_SITES, value=config.scraper_site, label="默认来源引擎")
                scraper_max_chars = gr.Number(value=config.scraper_max_chars, label="最大字数，0 表示不限", precision=0)
                scraper_chapter_limit = gr.Number(value=config.scraper_chapter_limit, label="最大章节数，0 表示不限", precision=0)
            with gr.Row():
                source_base_url = gr.Textbox(value=config.source_base_url, label="聚合接口 Base URL")
                source_platform = gr.Dropdown(PLATFORMS, value=config.source_platform, label="默认平台")
                source_media = gr.Dropdown(MEDIA_TYPES, value=config.source_media, label="默认内容类型")
            source_hosts = gr.Textbox(value=config.source_hosts, label="聚合接口备用线路，一行一个", lines=5)
            with gr.Row():
                source_delay = gr.Number(value=config.source_delay, label="章节抓取间隔秒", precision=2)
                legado_source_ref = gr.Textbox(value=config.legado_source_ref, label="普通 Legado JSON 文件路径或 URL")

        with gr.Accordion("分镜 LLM", open=False):
            with gr.Row():
                llm_provider = gr.Dropdown(LLM_PROVIDERS, value=config.llm_provider, label="Provider")
                llm_model = gr.Textbox(value=config.llm_model, label="模型")
            with gr.Row():
                llm_base_url = gr.Textbox(value=config.llm_base_url, label="Base URL")
                llm_api_key = gr.Textbox(value=config.llm_api_key, label="API Key", type="password")
            template_name = gr.Dropdown(list(STORYBOARD_TEMPLATES.keys()), value="通用动漫", label="模板")
            llm_storyboard_prompt = gr.Textbox(value=config.llm_storyboard_prompt, label="System prompt", lines=5)
            with gr.Row():
                style_suffix = gr.Textbox(value=config.llm_image_style_suffix, label="图片风格后缀")
                llm_negative_prompt = gr.Textbox(value=config.llm_negative_prompt, label="负面 prompt")

            def _on_llm_provider(prov):
                base, model = LLM_PRESETS.get(prov, ("", ""))
                return gr.update(value=base or gr.skip()), gr.update(value=model or gr.skip())

            llm_provider.change(_on_llm_provider, [llm_provider], [llm_base_url, llm_model])
            template_name.change(lambda n: STORYBOARD_TEMPLATES[n], [template_name], [llm_storyboard_prompt])

        with gr.Accordion("TTS", open=True):
            with gr.Row():
                tts_provider = gr.Dropdown(TTS_PROVIDERS, value=config.tts_provider, label="Provider")
                tts_voice = gr.Textbox(value=config.tts_voice, label="音色")
                voice_quick = gr.Dropdown(EDGE_JA_VOICES, value=config.tts_voice if config.tts_voice in EDGE_JA_VOICES else EDGE_JA_VOICES[0], label="Edge 常用音色")
            with gr.Row():
                tts_rate = gr.Textbox(value=config.tts_rate, label="语速，如 +0% / +20%")
                tts_volume = gr.Number(value=config.get("tts_volume", 1.0), label="TTS 音量倍率", precision=2)
                tts_base_url = gr.Textbox(value=config.tts_base_url, label="Base URL")
                tts_api_key = gr.Textbox(value=config.tts_api_key, label="API Key", type="password")
            with gr.Row():
                tts_retries = gr.Number(value=config.tts_retries, label="TTS 重试次数", precision=0)
                tts_fail_silence = gr.Checkbox(value=config.tts_fail_silence, label="TTS 失败后用静音占位")
                tts_segment_timeout_seconds = gr.Number(value=config.get("tts_segment_timeout_seconds", 180), label="TTS segment timeout seconds", precision=0)
                tts_heartbeat_seconds = gr.Number(value=config.get("tts_heartbeat_seconds", 30), label="TTS heartbeat seconds", precision=0)
            tts_extra_json = gr.Textbox(value=json.dumps(config.tts_extra or {}, ensure_ascii=False), label="额外参数 JSON")
            voice_quick.change(lambda v: v, [voice_quick], [tts_voice])

        with gr.Accordion("图片", open=False):
            with gr.Row():
                image_provider = gr.Dropdown(IMAGE_PROVIDERS, value=config.image_provider, label="Provider")
                image_model = gr.Textbox(value=config.image_model, label="模型")
            with gr.Row():
                image_base_url = gr.Textbox(value=config.image_base_url, label="Base URL")
                image_api_key = gr.Textbox(value=config.image_api_key, label="API Key", type="password")
            with gr.Row():
                image_width = gr.Number(value=config.image_width, label="宽", precision=0)
                image_height = gr.Number(value=config.image_height, label="高", precision=0)
                image_steps = gr.Number(value=config.image_steps, label="Steps", precision=0)
                image_cfg = gr.Number(value=config.image_cfg, label="CFG")
            image_workflow = gr.Textbox(value=config.image_workflow, label="ComfyUI workflow JSON 路径")
            image_provider.change(lambda p: gr.update(value=IMG_PRESETS.get(p, "")), [image_provider], [image_base_url])

        with gr.Accordion("图音配比与视频", open=True):
            with gr.Row():
                pacing_mode = gr.Dropdown(PACING_MODES, value=config.pacing_mode, label="配比模式")
                pacing_seconds_per_image = gr.Slider(2, 30, value=config.pacing_seconds_per_image, step=1, label="每张图秒数")
                pacing_sentences_per_image = gr.Slider(1, 10, value=config.pacing_sentences_per_image, step=1, label="每图段数")
                pacing_fixed_count = gr.Slider(3, 300, value=config.pacing_fixed_count, step=1, label="固定图数")
            with gr.Row():
                video_width = gr.Number(value=config.video_width, label="宽", precision=0)
                video_height = gr.Number(value=config.video_height, label="高", precision=0)
                video_fps = gr.Number(value=config.video_fps, label="FPS", precision=0)
            with gr.Row():
                ken_burns = gr.Checkbox(value=config.ken_burns, label="短视频 Ken Burns 动效")
                video_long_mode = gr.Checkbox(value=config.video_long_mode, label="长视频稳定模式")
                video_cleanup_temp = gr.Checkbox(value=config.video_cleanup_temp, label="清理临时片段")
            with gr.Row():
                video_subtitle = gr.Checkbox(value=config.video_subtitle, label="内嵌字幕")
                video_external_subtitle = gr.Checkbox(value=config.video_external_subtitle, label="同时导出 SRT")
                video_subtitle_font = gr.Textbox(value=config.video_subtitle_font, label="字幕字体")
                video_subtitle_size = gr.Number(value=config.video_subtitle_size, label="字幕字号", precision=0)
            with gr.Row():
                video_bgm_path = gr.Textbox(value=config.video_bgm_path, label="BGM 路径，可空")
                video_bgm_volume = gr.Slider(0, 1, value=config.video_bgm_volume, step=0.05, label="BGM 音量")

        with gr.Accordion("上传", open=False):
            youtube_visibility = gr.Dropdown(VIS, value=config.youtube_visibility, label="YouTube 可见性")
            youtube_tags = gr.Textbox(value=config.youtube_tags, label="标签，逗号分隔")
            youtube_title_template = gr.Textbox(value=config.youtube_title_template, label="默认标题模板", lines=2)
            youtube_title_max_chars = gr.Number(value=config.get("youtube_title_max_chars", 100), label="标题字数上限", precision=0)
            youtube_description = gr.Textbox(value=config.youtube_description, label="默认描述", lines=3)

        save_btn = gr.Button("保存配置", variant="primary")
        with gr.Accordion("Stability / ordinary PCs", open=False):
            with gr.Row():
                max_concurrent_external_api = gr.Number(value=config.max_concurrent_external_api, label="Max external API slots", precision=0)
                max_concurrent_ffmpeg = gr.Number(value=config.max_concurrent_ffmpeg, label="Max FFmpeg slots", precision=0)
                max_concurrent_media_probe = gr.Number(value=config.max_concurrent_media_probe, label="Max ffprobe slots", precision=0)
            with gr.Row():
                resource_wait_timeout = gr.Number(value=config.resource_wait_timeout, label="Slot wait timeout seconds (0 = no limit)", precision=0)
                worker_detached = gr.Checkbox(value=config.worker_detached, label="Detached worker process")

        save_msg = gr.Markdown()

        all_inputs = [
            scraper_site,
            scraper_max_chars,
            scraper_chapter_limit,
            source_base_url,
            source_platform,
            source_media,
            source_hosts,
            source_delay,
            legado_source_ref,
            llm_provider,
            llm_base_url,
            llm_api_key,
            llm_model,
            llm_storyboard_prompt,
            style_suffix,
            llm_negative_prompt,
            tts_provider,
            tts_voice,
            tts_rate,
            tts_volume,
            tts_base_url,
            tts_api_key,
            tts_extra_json,
            tts_retries,
            tts_fail_silence,
            image_provider,
            image_base_url,
            image_api_key,
            image_model,
            image_width,
            image_height,
            image_steps,
            image_cfg,
            image_workflow,
            pacing_mode,
            pacing_seconds_per_image,
            pacing_sentences_per_image,
            pacing_fixed_count,
            ken_burns,
            video_width,
            video_height,
            video_fps,
            video_subtitle,
            video_external_subtitle,
            video_subtitle_font,
            video_subtitle_size,
            video_bgm_path,
            video_bgm_volume,
            video_long_mode,
            video_cleanup_temp,
            youtube_visibility,
            youtube_tags,
            youtube_title_template,
            youtube_title_max_chars,
            youtube_description,
            max_concurrent_external_api,
            max_concurrent_ffmpeg,
            max_concurrent_media_probe,
            resource_wait_timeout,
            worker_detached,
            tts_segment_timeout_seconds,
            tts_heartbeat_seconds,
        ]

        def _save(*vals):
            try:
                tts_extra = json.loads(vals[22]) if str(vals[22]).strip() else {}
            except Exception:
                tts_extra = {}
            data = {
                "scraper_site": vals[0],
                "scraper_max_chars": int(vals[1] or 0),
                "scraper_chapter_limit": int(vals[2] or 0),
                "source_base_url": vals[3],
                "source_platform": vals[4],
                "source_media": vals[5],
                "source_hosts": vals[6],
                "source_delay": float(vals[7] or 0),
                "legado_source_ref": vals[8],
                "llm_provider": vals[9],
                "llm_base_url": vals[10],
                "llm_api_key": vals[11],
                "llm_model": vals[12],
                "llm_storyboard_prompt": vals[13],
                "llm_image_style_suffix": vals[14],
                "llm_negative_prompt": vals[15],
                "tts_provider": vals[16],
                "tts_voice": vals[17],
                "tts_rate": vals[18],
                "tts_volume": float(vals[19] or 1.0),
                "tts_base_url": vals[20],
                "tts_api_key": vals[21],
                "tts_extra": tts_extra,
                "tts_retries": int(vals[23] or 1),
                "tts_fail_silence": bool(vals[24]),
                "image_provider": vals[25],
                "image_base_url": vals[26],
                "image_api_key": vals[27],
                "image_model": vals[28],
                "image_width": int(vals[29]),
                "image_height": int(vals[30]),
                "image_steps": int(vals[31]),
                "image_cfg": float(vals[32]),
                "image_workflow": vals[33],
                "pacing_mode": vals[34],
                "pacing_seconds_per_image": float(vals[35]),
                "pacing_sentences_per_image": int(vals[36]),
                "pacing_fixed_count": int(vals[37]),
                "ken_burns": bool(vals[38]),
                "video_width": int(vals[39]),
                "video_height": int(vals[40]),
                "video_fps": int(vals[41]),
                "video_subtitle": bool(vals[42]),
                "video_external_subtitle": bool(vals[43]),
                "video_subtitle_font": vals[44],
                "video_subtitle_size": int(vals[45]),
                "video_bgm_path": vals[46],
                "video_bgm_volume": float(vals[47]),
                "video_long_mode": bool(vals[48]),
                "video_cleanup_temp": bool(vals[49]),
                "youtube_visibility": vals[50],
                "youtube_tags": vals[51],
                "youtube_title_template": vals[52],
                "youtube_title_max_chars": int(vals[53] or 100),
                "youtube_description": vals[54],
                "max_concurrent_external_api": int(vals[55] or 1),
                "max_concurrent_ffmpeg": int(vals[56] or 1),
                "max_concurrent_media_probe": int(vals[57] or 1),
                "resource_wait_timeout": int(vals[58] or 0),
                "worker_detached": bool(vals[59]),
                "tts_segment_timeout_seconds": int(vals[60] or 180),
                "tts_heartbeat_seconds": int(vals[61] or 30),
            }
            return _save_settings(data)

        save_btn.click(_save, all_inputs, [save_msg])


def build_test_tab():
    with gr.Tab("单步测试"):
        url = gr.Textbox(value="https://ncode.syosetu.com/n4185ci/1/", label="URL / ID / qingtian 引用")
        with gr.Row():
            btn_scrape = gr.Button("1. 抓取")
            btn_clean = gr.Button("2. 清洗切片")
            btn_tts = gr.Button("3. TTS")
            btn_img = gr.Button("4. 出图")
            btn_video = gr.Button("5. 合成")
        log = gr.Textbox(label="日志", lines=16, interactive=False, autoscroll=True)
        novel_text = gr.Textbox(label="正文预览", lines=8, interactive=False)
        segs_view = gr.Dataframe(headers=["index", "text"], label="Segments")
        audio_file = gr.Audio(label="首段音频", interactive=False)
        image_gallery = gr.Gallery(label="生成图片", columns=4, height=360)
        video_out = gr.Video(label="最终视频", interactive=False)
        state = gr.State({})

        def _do_scrape(u, st):
            logs = []
            try:
                novel = pr.stage_scrape(u, site=config.scraper_site, max_chars=config.scraper_max_chars, on_log=logs.append)
                st = dict(st or {})
                st["novel"] = novel
                return "\n".join(logs), novel.full_text[:3000], st
            except Exception as exc:
                return "\n".join(logs) + f"\nERROR {exc}\n{traceback.format_exc()}", "", st

        def _do_clean(st):
            logs = []
            if not st or "novel" not in st:
                return "请先抓取", [], st
            segs = pr.stage_clean(st["novel"], on_log=logs.append)
            st = dict(st)
            st["segments"] = segs
            return "\n".join(logs), [[s.index, s.text] for s in segs[:300]], st

        def _do_tts(st):
            logs = []
            if not st or "segments" not in st:
                return "请先清洗切片", None, st
            job_id = pr.new_job_id("test")
            job_dir = JOBS_DIR / job_id
            audios, durs = pr.stage_tts(st["segments"], job_dir, on_log=logs.append)
            st = dict(st)
            st.update({"audios": audios, "durations": durs, "job_dir": str(job_dir)})
            return "\n".join(logs), str(audios[0]) if audios else None, st

        def _do_img(st):
            logs = []
            if not st or "audios" not in st:
                return "请先 TTS", [], st
            plans = pr.stage_pacing(st["segments"], st["durations"], on_log=logs.append)
            images = pr.stage_storyboard_and_image(plans, Path(st["job_dir"]), on_log=logs.append)
            st = dict(st)
            st.update({"plans": plans, "images": images})
            return "\n".join(logs), [str(p) for p in images], st

        def _do_video(st):
            logs = []
            if not st or "images" not in st:
                return "请先出图", None, st
            video = pr.stage_compose(
                st["audios"],
                st["durations"],
                st["segments"],
                st["plans"],
                st["images"],
                Path(st["job_dir"]),
                on_log=logs.append,
            )
            return "\n".join(logs), str(video), st

        btn_scrape.click(_do_scrape, [url, state], [log, novel_text, state])
        btn_clean.click(_do_clean, [state], [log, segs_view, state])
        btn_tts.click(_do_tts, [state], [log, audio_file, state])
        btn_img.click(_do_img, [state], [log, image_gallery, state])
        btn_video.click(_do_video, [state], [log, video_out, state])


def build_pipeline_tab():
    with gr.Tab("完整流水线"):
        gr.Markdown("### 后台长任务")
        pipeline_input = gr.Textbox(value="", label="URL / ID / qingtian 引用 / 关键词")
        with gr.Row():
            resume_job = gr.Textbox(value="", label="续跑 job_id，可空")
            start_btn = gr.Button("启动后台任务", variant="primary")
            stop_btn = gr.Button("停止当前任务")
            delete_btn = gr.Button("停止并删除当前任务")
            refresh_btn = gr.Button("刷新状态")
        current_job = gr.Textbox(label="当前 job_id", interactive=True)
        progress = gr.Slider(0, 1, value=0, label="进度", interactive=False)
        status = gr.Markdown()
        log = gr.Textbox(label="日志", lines=20, interactive=False, autoscroll=True)
        video_out = gr.Video(label="最终视频", interactive=False)
        result_json = gr.JSON(label="结果")
        timer = gr.Timer(5, active=True)

        def _start(u, resume_id):
            job_id = (resume_id or "").strip() or pr.new_job_id("pipeline")
            input_text = (u or "").strip()
            if not input_text and resume_id:
                input_text = pr.load_status(job_id).get("input", "")
            if not input_text:
                return job_id, 0, "请填写输入或可续跑的 job_id", "", None, {}

            try:
                job_id, pid = pr.start_worker(input_text, job_id=job_id, resume=bool(resume_id))
                ACTIVE_JOBS[job_id] = pid
            except Exception as exc:
                return job_id, 0, f"启动 worker 失败: {exc}", pr.tail_log(job_id), None, pr.load_status(job_id)
            return (job_id, *_refresh(job_id))

        def _refresh(job_id):
            job_id = (job_id or "").strip()
            if not job_id:
                return 0, "未启动任务", "", None, {}
            st = pr.load_status(job_id)
            running = f"worker pid={st.get('worker_pid')} running" if st.get("worker_alive") else "worker not running"
            video = st.get("video") or ""
            video_path = video if video and Path(video).exists() else None
            md = (
                f"制作阶段：{pr.stage_display(st)} / {running}  \n"
                f"标题：{st.get('title', '')}  \n"
                f"更新时间：{st.get('updated_at', '')}  \n"
                f"目录：{pr.job_dir_for(job_id)}"
            )
            return float(st.get("progress", 0) or 0), md, pr.tail_log(job_id), video_path, st

        def _stop(job_id):
            job_id = (job_id or "").strip()
            if not job_id:
                return 0, "未启动任务", "", None, {}
            try:
                pr.stop_job(job_id)
                ACTIVE_JOBS.pop(job_id, None)
            except Exception as exc:
                return 0, f"停止失败: {exc}", pr.tail_log(job_id), None, pr.load_status(job_id)
            return _refresh(job_id)

        def _delete(job_id):
            job_id = (job_id or "").strip()
            if not job_id:
                return "", 0, "未选择任务", "", None, {}
            try:
                pr.delete_job(job_id, stop_running=True)
                ACTIVE_JOBS.pop(job_id, None)
            except Exception as exc:
                return job_id, 0, f"删除失败: {exc}", pr.tail_log(job_id), None, pr.load_status(job_id)
            return "", 0, f"已删除任务：{job_id}", "", None, {}

        start_btn.click(_start, [pipeline_input, resume_job], [current_job, progress, status, log, video_out, result_json])
        stop_btn.click(_stop, [current_job], [progress, status, log, video_out, result_json])
        delete_btn.click(_delete, [current_job], [current_job, progress, status, log, video_out, result_json])
        refresh_btn.click(_refresh, [current_job], [progress, status, log, video_out, result_json])
        timer.tick(_refresh, [current_job], [progress, status, log, video_out, result_json])
        return pipeline_input


def build_jobs_tab():
    with gr.Tab("任务记录"):
        btn = gr.Button("刷新任务列表")
        job_id = gr.Textbox(label="查看 job_id")
        with gr.Row():
            detail_btn = gr.Button("查看详情")
            stop_btn = gr.Button("停止任务")
            delete_btn = gr.Button("停止并删除任务")
        table = gr.Dataframe(headers=["job_id", "制作阶段", "stage", "progress", "worker_pid", "worker_alive", "title", "video", "updated_at"], label="Jobs with worker state")
        status = gr.JSON(label="status.json")
        log = gr.Textbox(label="日志尾部", lines=18, interactive=False)

        def _list():
            return [[r["job_id"], r.get("stage_display", r["stage"]), r["stage"], r["progress"], r.get("worker_pid"), r.get("worker_alive"), r["title"], r["video"], r["updated_at"]] for r in pr.list_jobs()]

        def _detail(j):
            return pr.load_status(j), pr.tail_log(j)

        def _stop(j):
            j = (j or "").strip()
            if not j:
                return _list(), {}, "请先填写 job_id"
            try:
                pr.stop_job(j)
            except Exception as exc:
                return _list(), pr.load_status(j), f"停止失败: {exc}\n\n{pr.tail_log(j)}"
            return _list(), pr.load_status(j), pr.tail_log(j)

        def _delete(j):
            j = (j or "").strip()
            if not j:
                return _list(), {}, "请先填写 job_id"
            try:
                pr.delete_job(j, stop_running=True)
            except Exception as exc:
                return _list(), pr.load_status(j), f"删除失败: {exc}\n\n{pr.tail_log(j)}"
            return _list(), {}, f"已删除任务：{j}"

        btn.click(_list, [], [table])
        detail_btn.click(_detail, [job_id], [status, log])
        stop_btn.click(_stop, [job_id], [table, status, log])
        delete_btn.click(_delete, [job_id], [table, status, log])


def build_upload_tab():
    with gr.Tab("上传"):
        gr.Markdown("上传依赖已有的浏览器自动化环境。请先启动带 remote debugging 的 Chrome 并登录 YouTube Studio。")
        video = gr.Textbox(label="视频文件路径")
        title = gr.Textbox(label="标题")
        desc = gr.Textbox(label="描述", lines=3, value=config.youtube_description)
        vis = gr.Dropdown(VIS, value=config.youtube_visibility, label="可见性")
        cover = gr.Textbox(label="封面图路径，可空")
        btn = gr.Button("上传", variant="primary")
        log = gr.Textbox(label="日志", lines=15, interactive=False, autoscroll=True)
        progress = gr.Slider(0, 100, value=0, label="进度", interactive=False)
        result = gr.Textbox(label="video_id", interactive=False)

        def _upload(v, t, d, vi, c):
            from app.upload import upload_to_youtube

            logs = []
            cur = [0.0]
            try:
                vid = upload_to_youtube(
                    Path(v),
                    t,
                    d,
                    vi,
                    Path(c) if c else None,
                    on_log=lambda m: logs.append(str(m)),
                    on_progress=lambda p: cur.__setitem__(0, float(p)),
                )
                return cur[0], "\n".join(logs), vid or ""
            except Exception as exc:
                return cur[0], "\n".join(logs) + f"\nERROR {exc}\n{traceback.format_exc()}", ""

        btn.click(_upload, [video, title, desc, vis, cover], [progress, log, result])


def build_update_tab():
    with gr.Tab("软件更新"):
        gr.Markdown(f"### 当前版本 v{VERSION}")
        manifest = gr.Textbox(value=config.get("update_manifest_url", ""), label="更新清单 URL")
        feedback_url = gr.Textbox(value=config.get("feedback_issue_url", ""), label="反馈入口 URL")
        check_start = gr.Checkbox(value=bool(config.get("update_check_on_startup", True)), label="桌面 GUI 启动时检查新版本")
        with gr.Row():
            save_btn = gr.Button("保存更新配置")
            check_btn = gr.Button("检查更新", variant="primary")
            download_btn = gr.Button("下载更新包")
        status = gr.Markdown()
        notes = gr.Textbox(label="发布说明", lines=8, interactive=False)
        feedback_link = gr.Markdown(f"[提交反馈]({config.get('feedback_issue_url', '')})")
        update_state = gr.State(None)

        def _save_update_config(url, feedback, enabled):
            config.set("update_manifest_url", str(url or "").strip())
            config.set("feedback_issue_url", str(feedback or "").strip())
            config.set("update_check_on_startup", bool(enabled))
            config.save()
            link = str(feedback or "").strip()
            return "已保存更新配置", f"[提交反馈]({link})" if link else "未配置反馈入口"

        def _check(url, feedback, enabled):
            _save_update_config(url, feedback, enabled)
            try:
                from app.updater import check_for_update

                info = check_for_update(config)
            except Exception as exc:
                return f"检查失败：{exc}", "", None
            if not info:
                return "已是最新版本", "", None
            return f"发现新版本 v{info.version}", info.notes or "暂无发布说明", info.__dict__

        def _download(info):
            if not info:
                return "请先检查到新版本。"
            try:
                from app.updater import UpdateInfo, download_update

                zip_path = download_update(UpdateInfo(**info))
                return f"已下载：{zip_path}"
            except Exception as exc:
                return f"下载失败：{exc}"

        save_btn.click(_save_update_config, [manifest, feedback_url, check_start], [status, feedback_link])
        check_btn.click(_check, [manifest, feedback_url, check_start], [status, notes, update_state])
        download_btn.click(_download, [update_state], [status])


def main():
    css = """
    .gradio-container { max-width: 1280px !important; }
    textarea, input { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
    """
    with gr.Blocks(title="小说推文长视频流水线", theme=gr.themes.Soft(), css=css) as demo:
        gr.Markdown("# 小说推文长视频流水线")
        gr.Markdown("国内聚合书源、普通 Legado 书源、日本 Syosetu、TTS、分镜出图、长视频合成、字幕与上传。")
        picked_ref = build_source_tab()
        build_config_tab()
        build_test_tab()
        pipeline_input = build_pipeline_tab()
        build_jobs_tab()
        build_upload_tab()
        build_update_tab()
        picked_ref.change(lambda x: x or "", [picked_ref], [pipeline_input])
    demo.queue(default_concurrency_limit=4)
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)


if __name__ == "__main__":
    main()
