"""配置管理：所有可配置项均存 data/settings.json，UI 上可改。"""
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
PROFILES_DIR = DATA_DIR / "profiles"
DEFAULT_PROFILE_NAMES = ("配置1", "配置2")

DATA_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)
PROFILES_DIR.mkdir(exist_ok=True)


SETTINGS_SCHEMA_VERSION = 7
DEFAULT_YOUTUBE_TITLE_TEMPLATE = "💥【爆款短文】《{clean_title}》{intro} #小说 #推文"
DEFAULT_YOUTUBE_DESCRIPTION = (
    "原创内容，只只推文，每天更新高分又好看的#AI漫画#小说推文\n"
    "💖感谢您的订阅👉小伙伴想看完整版，欢迎催更"
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "settings_schema_version": SETTINGS_SCHEMA_VERSION,
    "active_profile": "配置1",

    # 爬取
    "scraper_site": "qingtian",
    "scraper_max_chars": 0,
    "scraper_chapter_limit": 0,
    "source_base_url": "https://v1.gyks.cf",
    "source_platform": "番茄",
    "source_media": "小说",
    "source_hosts": (
        "https://v1.gyks.cf\n"
        "https://v2.gyks.cf\n"
        "https://v3.gyks.cf\n"
        "https://v4.gyks.cf\n"
        "https://v5.gyks.cf\n"
        "http://219.154.201.122:5006\n"
        "https://api.langge.cf"
    ),
    "source_delay": 0.15,
    "legado_source_ref": "",

    # LLM（分镜）
    "llm_provider": "openai",  # openai / claude / gemini / deepseek / ollama / custom
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    "llm_storyboard_prompt": (
        "You are an expert at converting novel paragraphs into safe image-generation prompts.\n"
        "For each paragraph, output ONE concise English prompt (<=60 tokens) that captures setting, "
        "characters, mood, lighting, and key action. Use adult-looking characters, symbolic drama, "
        "and non-graphic emotion. Avoid gore, blood, weapons, corpses, explicit violence, nudity, "
        "sexualized minors, abuse, self-harm, logos, watermarks, and any readable text. "
        "Use anime/illustration style by default. "
        "Output ONLY the prompt, no quotes, no numbering."
    ),
    "llm_image_style_suffix": "anime style, detailed, cinematic lighting, masterpiece",
    "llm_negative_prompt": "lowres, bad anatomy, bad hands, text, error, missing fingers, jpeg artifacts, watermark, logo, words, blood, gore, corpse, severed limbs, nudity, sexual content, explicit violence, weapon close-up",

    # Short marketing title
    "short_title_enabled": True,
    "short_title_min_chars": 20,
    "short_title_max_chars": 30,
    "short_title_prompt": (
        "请根据小说简介生成一个适合推文视频的短说明标题。"
        "要求 20 到 30 个中文字符，突出爽点/冲突/悬念，不要书名号，不要引号，不要换行，不要标签。"
    ),
    "ai_text_clean_enabled": False,
    "ai_text_clean_batch_chars": 6000,
    "ai_text_clean_prompt": (
        "你是小说正文清洗器。任务是判断哪些段落不是小说剧情正文，应该删除。"
        "删除范围包括：作者的话、请假条、PS、上架感言、完本感言、求票求收藏求追读、感谢打赏/月票/推荐票、"
        "章节说明、更新说明、网站广告、版权/免责声明、群号、目录、人物设定/人设卡/设定集等非剧情材料。"
        "必须保留所有剧情、对白、心理描写、旁白、世界观叙事。"
        "只返回 JSON，格式为 {\"remove\":[段落编号]}，不要解释。"
    ),

    "character_analysis_enabled": False,
    "character_analysis_max_chars": 12000,
    "character_analysis_max_tokens": 1800,
    "character_analysis_max_characters_per_prompt": 4,
    "character_analysis_always_include_protagonists": True,
    "character_analysis_prompt": "",
    "character_reference_enabled": False,
    "character_reference_provider": "same_as_image",
    "character_reference_base_url": "",
    "character_reference_api_key": "",
    "character_reference_model": "",
    "character_reference_workflow": "",
    "character_reference_width": 768,
    "character_reference_height": 1024,
    "character_reference_max_count": 6,
    "character_reference_prompt_suffix": "",
    "scene_inject_visual_theme": True,
    "scene_inject_character_triggers": True,
    "scene_reference_enabled": False,
    "scene_reference_provider": "same_as_image",
    "scene_reference_base_url": "",
    "scene_reference_api_key": "",
    "scene_reference_model": "",
    "scene_reference_workflow": "",
    "scene_reference_max_images": 2,

    # TTS
    "tts_provider": "edge",  # edge / voicevox / azure / openai / elevenlabs / custom
    "tts_voice": "ja-JP-NanamiNeural",
    "tts_rate": "+0%",
    "tts_volume": 1.0,
    "tts_model": "tts-1",
    "tts_api_key": "",
    "tts_base_url": "",  # voicevox: http://localhost:50021 ; openai: https://api.openai.com/v1 ; etc
    "tts_extra": {},  # 额外参数 (azure region 等)
    "tts_retries": 3,
    "tts_fail_silence": True,
    "tts_emotion": "",
    "tts_segment_timeout_seconds": 180,
    "tts_heartbeat_seconds": 30,

    # Image
    "image_provider": "comfyui",  # comfyui / sdwebui / openai / replicate / aliyun / custom
    "image_base_url": "http://127.0.0.1:8188",
    "image_api_key": "",
    "image_model": "",
    "image_width": 1920,
    "image_height": 1080,
    "image_steps": 25,
    "image_cfg": 7.0,
    "image_api_timeout_seconds": 300,
    "image_workflow": "",  # comfyui workflow json 路径（可选）

    # Cover
    "cover_enabled": True,
    "cover_provider": "same_as_image",
    "cover_base_url": "",
    "cover_api_key": "",
    "cover_model": "",
    "cover_width": 1280,
    "cover_height": 720,
    "cover_title_size": 72,
    "cover_title_font": "Microsoft YaHei",
    "cover_title_color": "#FFFFFF",
    "cover_title_bg": "#050505",
    "cover_title_area_ratio": 0.24,
    "cover_prompt_template": (
        'Create a cinematic YouTube cover illustration for a long-form novel recap video.\n'
        'Exact story title context: "{title}".\n'
        'Story excerpt: {excerpt}\n'
        'Do not render any words, logos, captions, watermarks, UI, or title text inside the artwork. '
        'Leave a natural uncluttered lower area for an editor-added title overlay. {style}'
    ),

    # 图音配比
    "pacing_mode": "by_duration",  # by_duration / by_sentence / by_paragraph / fixed_count
    "pacing_seconds_per_image": 6,
    "pacing_sentences_per_image": 3,
    "pacing_fixed_count": 10,
    "ken_burns": True,
    "video_motion": "上下移动",
    "video_motion_curve": "ease",
    "video_motion_cycle_seconds": 0.0,
    "video_transition": "none",
    "video_transition_duration": 0.4,

    # 视频
    "video_width": 1920,
    "video_height": 1080,
    "video_fps": 30,
    "video_encoder": "libx264",
    "video_encoder_preset": "veryfast",
    "video_encoder_quality": 20,
    "video_subtitle": True,
    "video_external_subtitle": True,
    "video_subtitle_font": "Microsoft YaHei",
    "video_subtitle_size": 48,
    "video_subtitle_position": "下边",
    "video_subtitle_color": "#FFFFFF",
    "video_subtitle_outline_color": "#000000",
    "video_subtitle_back_color": "#000000",
    "video_subtitle_outline": 4.0,
    "video_subtitle_shadow": 1.0,
    "video_subtitle_margin_v": 60,
    "video_subtitle_margin_lr": 56,
    "video_subtitle_spacing": 0.0,
    "video_subtitle_bold": False,
    "video_subtitle_italic": False,
    "video_subtitle_chars_per_line": 24,
    "video_subtitle_max_lines": 2,
    "video_bgm_path": "",
    "video_bgm_volume": 0.15,
    "video_long_mode": True,
    "video_cleanup_temp": True,

    "max_concurrent_jobs": 1,
    "max_concurrent_external_api": 2,
    "max_concurrent_ffmpeg": 1,
    "max_concurrent_media_probe": 2,
    "max_parallel_tts": 2,
    "max_parallel_images": 2,
    "max_parallel_video_clips": 1,
    "pipeline_overlap_tts_images": True,
    "resource_wait_timeout": 0,
    "worker_detached": True,
    "hardware_autotune_enabled": True,
    "hardware_autotune_done": False,
    "hardware_autotune_at": "",
    "hardware_autotune_signature": "",
    "hardware_autotune_summary": "",
    "dependency_check_on_startup": True,
    "dependency_auto_install_python": True,
    "dependency_auto_install_ffmpeg": True,
    "dependency_auto_install_browser": True,
    "dependency_pip_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "dependency_pip_extra_args": "",
    "dependency_pip_timeout_seconds": 1800,
    "dependency_ffmpeg_url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "dependency_last_report": "",
    "update_manifest_url": "https://github.com/1951779219/novel_video_pipeline_release/releases/latest/download/latest.json",
    "update_check_on_startup": True,
    "feedback_issue_url": "https://github.com/1951779219/novel_video_pipeline_feedback/issues/new/choose",

    # 上传
    "upload_enabled": False,
    "youtube_upload_method": "browser",
    "youtube_visibility": "PRIVATE",
    "youtube_title_template": DEFAULT_YOUTUBE_TITLE_TEMPLATE,
    "youtube_title_max_chars": 100,
    "youtube_description": DEFAULT_YOUTUBE_DESCRIPTION,
    "youtube_tags": "",
    "browser_flow": "simple",
    "browser_upload_policy": "BTRA",
    "browser_ad_interval": 60,
    "browser_ad_start": 0,
    "browser_auto_restart": True,
    "browser_stall_timeout_min": 10,
    "browser_op_speed": "normal",
    "upload_dependency_check_before_upload": True,
    "browser_profiles": json.dumps(
        [
            {
                "name": "无创收精简流程",
                "flow": "simple",
                "upload_policy": "BTRA",
                "ad_interval": 60,
                "ad_start": 0,
                "visibility": "PUBLIC",
                "title_template": "",
                "description": "",
            },
            {
                "name": "完整创收流程",
                "flow": "full",
                "upload_policy": "BTRA",
                "ad_interval": 60,
                "ad_start": 0,
                "visibility": "PUBLIC",
                "title_template": "",
                "description": "",
            },
        ],
        ensure_ascii=False,
    ),
    "browser_active_profile": "无创收精简流程",
}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _looks_like_legacy_title_template(value: Any) -> bool:
    text = str(value or "")
    return (
        not text.strip()
        or "#爽文" in text
        or "#一口气看完" in text
        or "#灏忚" in text
        or "{short_title}" in text
    )


def _looks_like_legacy_description(value: Any) -> bool:
    text = str(value or "")
    return (
        not text.strip()
        or "{short_title}" in text
        or "{intro}" in text
        or "#爽文" in text
        or "#灏忚" in text
        or "涔﹀悕" in text
    )


def _apply_compat_migrations(data: dict[str, Any], saved: dict[str, Any] | None = None) -> None:
    saved = saved or {}
    saved_version = _to_int(saved.get("settings_schema_version"), 0)
    if saved_version < SETTINGS_SCHEMA_VERSION:
        if _to_int(data.get("scraper_max_chars"), 0) == 8000:
            data["scraper_max_chars"] = 0
        if _looks_like_legacy_title_template(data.get("youtube_title_template")) or "{title}" in str(data.get("youtube_title_template") or ""):
            data["youtube_title_template"] = DEFAULT_YOUTUBE_TITLE_TEMPLATE
        if _looks_like_legacy_description(data.get("youtube_description")):
            data["youtube_description"] = DEFAULT_YOUTUBE_DESCRIPTION
        if _to_int(data.get("image_width"), 0) == 1080 and _to_int(data.get("image_height"), 0) == 1920:
            data["image_width"] = 1920
            data["image_height"] = 1080
        if _to_int(data.get("video_width"), 0) == 1080 and _to_int(data.get("video_height"), 0) == 1920:
            data["video_width"] = 1920
            data["video_height"] = 1080
        if "Leave a clean darker lower area" in str(data.get("cover_prompt_template") or ""):
            data["cover_prompt_template"] = DEFAULT_SETTINGS["cover_prompt_template"]
        if _to_int(data.get("max_concurrent_jobs"), 0) == 2:
            data["max_concurrent_jobs"] = 1
    if saved_version < 6:
        if _to_int(data.get("image_api_timeout_seconds"), 0) == 900:
            data["image_api_timeout_seconds"] = 300
    data["settings_schema_version"] = SETTINGS_SCHEMA_VERSION


class Config:
    def __init__(self):
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if SETTINGS_FILE.exists():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    self._data[k] = v
                _apply_compat_migrations(self._data, saved)
            except Exception:
                pass

    def save(self):
        SETTINGS_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_profiles(self) -> list[str]:
        names = list(DEFAULT_PROFILE_NAMES)
        for path in sorted(PROFILES_DIR.glob("*.json")):
            if path.stem not in names:
                names.append(path.stem)
        return names

    def _profile_path(self, name: str) -> Path:
        cleaned = _safe_profile_name(name)
        return PROFILES_DIR / f"{cleaned}.json"

    def save_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        self._data["active_profile"] = cleaned
        path = self._profile_path(cleaned)
        path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return cleaned

    def load_profile(self, name: str) -> str:
        cleaned = _safe_profile_name(name)
        path = self._profile_path(cleaned)
        data = dict(DEFAULT_SETTINGS)
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
                _apply_compat_migrations(data, saved)
        data["active_profile"] = cleaned
        self._data = data
        return cleaned

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value

    def update(self, mapping: dict):
        self._data.update(mapping)

    def as_dict(self) -> dict:
        return dict(self._data)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


config = Config()


def _safe_profile_name(value: str) -> str:
    text = str(value or "").strip() or "配置1"
    for ch in '<>:"/\\|?*':
        text = text.replace(ch, "_")
    text = "".join("_" if ord(ch) < 32 else ch for ch in text)
    return text.strip(" .")[:80] or "配置1"
