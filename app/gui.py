"""Desktop production GUI for the novel video pipeline."""
from __future__ import annotations

import re
import json
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    from app.dependency_manager import ensure_dependencies, report_to_lines

    _DEPENDENCY_STARTUP_REPORT = ensure_dependencies(scope="core")
except Exception as exc:
    ensure_dependencies = None  # type: ignore[assignment]
    report_to_lines = None  # type: ignore[assignment]
    _DEPENDENCY_STARTUP_REPORT = {
        "ok": False,
        "summary": f"核心依赖检测失败：{exc}",
        "logs": [f"[依赖检测] 核心依赖检测失败：{exc}"],
    }

from app import pipeline_runner as pr
from app.api_probe import probe_image, probe_llm, probe_tts
from app.autotune import run_startup_autotune
from app.config import DATA_DIR, config
from app.version import VERSION
from app.backends.tts import EDGE_MULTI_VOICES


IMPORT_DIR = DATA_DIR / "imports"


class _TextValue:
    def __init__(self, widget: tk.Text):
        self.widget = widget

    def get(self) -> str:
        return self.widget.get("1.0", tk.END).strip()

    def set(self, value: str):
        self.widget.delete("1.0", tk.END)
        self.widget.insert("1.0", str(value or ""))


def _safe_name(value: str, fallback: str = "article") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:80].rstrip(" .") or fallback)


def _save_import_text(title: str, text: str) -> Path:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = _safe_name(title or next((x.strip() for x in text.splitlines() if x.strip()), ""), "article")
    path = IMPORT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.txt"
    path.write_text(text.strip(), encoding="utf-8")
    return path


def _upload_profile_names() -> list[str]:
    try:
        profiles = json.loads(str(config.get("browser_profiles", "[]") or "[]"))
    except Exception:
        profiles = []
    names = [str(p.get("name", "")).strip() for p in profiles if isinstance(p, dict) and str(p.get("name", "")).strip()]
    return names or ["无创收精简流程", "完整创收流程"]


class PipelineGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"小说推文视频生产台 v{VERSION}")
        self.root.geometry("1320x820")
        self.root.minsize(1050, 680)
        self._selected_job = ""
        self._search_refs: dict[str, str] = {}
        self._dependency_check_running = False
        self._build_style()
        self._build_ui()
        self._poll_jobs()
        self._poll_log()
        self.root.after(350, self._emit_dependency_startup_logs)
        self.root.after(1600, self._maybe_start_dependency_check)
        self.root.after(700, self._maybe_start_hardware_autotune)
        self.root.after(2400, self._maybe_check_update_on_startup)

    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self):
        outer = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left = ttk.Frame(outer)
        right = ttk.Frame(outer)
        outer.add(left, weight=3)
        outer.add(right, weight=2)

        self._build_source_panel(left)
        self._build_queue_panel(left)
        self._build_right_panel(right)

    def _build_source_panel(self, parent):
        box = ttk.LabelFrame(parent, text="输入 / 导入 / 番茄搜索", padding=8)
        box.pack(fill=tk.X, pady=(0, 8))

        search_row = ttk.Frame(box)
        search_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_row, text="小说名").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.search_var, width=34).pack(side=tk.LEFT, padx=6)
        ttk.Button(search_row, text="番茄搜索", command=self._search_books).pack(side=tk.LEFT)
        ttk.Button(search_row, text="加入选中搜索结果", command=self._add_selected_search).pack(side=tk.LEFT, padx=6)

        result_cols = ("title", "author", "source", "latest", "ref")
        self.search_tree = ttk.Treeview(box, columns=result_cols, show="headings", height=4)
        for col, text, width in [
            ("title", "书名", 180),
            ("author", "作者", 90),
            ("source", "来源", 70),
            ("latest", "最新章节", 180),
            ("ref", "引用", 330),
        ]:
            self.search_tree.heading(col, text=text)
            self.search_tree.column(col, width=width, anchor=tk.W)
        self.search_tree.pack(fill=tk.X, pady=(0, 8))

        manual_row = ttk.Frame(box)
        manual_row.pack(fill=tk.X)
        ttk.Label(manual_row, text="标题").pack(side=tk.LEFT)
        self.manual_title = tk.StringVar()
        ttk.Entry(manual_row, textvariable=self.manual_title, width=28).pack(side=tk.LEFT, padx=6)
        ttk.Button(manual_row, text="加入粘贴文本", command=self._add_manual_text).pack(side=tk.LEFT)
        ttk.Button(manual_row, text="导入 TXT 多篇", command=self._import_txt_files).pack(side=tk.LEFT, padx=6)

        self.manual_text = scrolledtext.ScrolledText(box, height=5, wrap=tk.WORD, font=("Microsoft YaHei UI", 10))
        self.manual_text.pack(fill=tk.X, pady=(6, 0))

    def _build_queue_panel(self, parent):
        box = ttk.LabelFrame(parent, text="任务队列", padding=8)
        box.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(box)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(toolbar, text="启动选中", style="Primary.TButton", command=self._start_selected).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="启动全部待处理", command=self._start_all_pending).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="停止选中", command=self._stop_selected_jobs).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="刷新", command=self._refresh_jobs).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="打开任务目录", command=self._open_selected_dir).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="清除图片缓存", command=self._clear_selected_media_cache).pack(side=tk.LEFT, padx=6)
        ttk.Button(toolbar, text="删除选中任务", command=self._delete_selected_jobs).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="清空已结束任务", command=self._delete_finished_jobs).pack(side=tk.LEFT, padx=6)

        cols = ("job_id", "stage", "progress", "worker", "title", "video", "youtube")
        self.job_tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="extended")
        headings = {
            "job_id": "任务ID",
            "stage": "制作阶段",
            "progress": "进度",
            "worker": "Worker",
            "title": "标题",
            "video": "视频",
            "youtube": "YouTube",
        }
        widths = {"job_id": 170, "stage": 150, "progress": 62, "worker": 88, "title": 150, "video": 190, "youtube": 170}
        for col in cols:
            self.job_tree.heading(col, text=headings[col])
            self.job_tree.column(col, width=widths[col], anchor=tk.W)
        self.job_tree.pack(fill=tk.BOTH, expand=True)
        self.job_tree.bind("<<TreeviewSelect>>", self._on_job_select)

    def _build_right_panel(self, parent):
        notebook = ttk.Notebook(parent)
        self._right_notebook = notebook
        notebook.pack(fill=tk.BOTH, expand=True)

        log_tab = ttk.Frame(notebook, padding=6)
        cfg_tab = ttk.Frame(notebook, padding=8)
        notebook.add(log_tab, text="实时日志")
        notebook.add(cfg_tab, text="流水线配置")
        try:
            from app.update_tab import UpdateTab

            self.update_tab = UpdateTab(notebook, config)
            notebook.add(self.update_tab.frame, text="软件更新")
        except Exception as exc:
            self.update_tab = None
            self._append_probe_log(f"[软件更新] 更新页加载失败：{exc}")

        self.log_text = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._build_config_panel(cfg_tab)

    def _build_config_panel(self, parent):
        self.vars: dict[str, tk.Variable] = {}
        self.combo_widgets: dict[str, ttk.Combobox] = {}

        footer = ttk.Frame(parent)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(footer, text="保存配置", style="Primary.TButton", command=self._save_config).pack(side=tk.RIGHT)
        ttk.Button(footer, text="保存到当前方案", command=self._save_profile).pack(side=tk.RIGHT, padx=(0, 6))

        scroll_area = ttk.Frame(parent)
        scroll_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(scroll_area, highlightthickness=0)
        scroll = ttk.Scrollbar(scroll_area, orient=tk.VERTICAL, command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def row(label: str, key: str, default="", width=30):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            ttk.Entry(line, textvariable=var, width=width).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            return var

        def textrow(label: str, key: str, default="", height=3):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT, anchor=tk.N)
            widget = tk.Text(line, height=height, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
            widget.insert("1.0", str(config.get(key, default)))
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = _TextValue(widget)
            return widget

        def combo(label: str, key: str, values: list[str], default=""):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default or (values[0] if values else ""))))
            widget = ttk.Combobox(line, textvariable=var, values=values, state="readonly")
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            self.combo_widgets[key] = widget

        def model_combo(label: str, key: str, default="", values: list[str] | None = None):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(config.get(key, default)))
            widget = ttk.Combobox(line, textvariable=var, values=values or [], state="normal")
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.vars[key] = var
            self.combo_widgets[key] = widget
            return var

        def check(label: str, key: str):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=3)
            ttk.Label(line, text=label, width=18).pack(side=tk.LEFT)
            var = tk.StringVar(value="开启" if bool(config.get(key, False)) else "关闭")
            ttk.Combobox(line, textvariable=var, values=["开启", "关闭"], state="readonly", width=12).pack(side=tk.LEFT)
            self.vars[key] = var

        def buttons(*items: tuple[str, callable]):
            line = ttk.Frame(body)
            line.pack(fill=tk.X, pady=(5, 3))
            ttk.Label(line, text="", width=18).pack(side=tk.LEFT)
            for text, command in items:
                ttk.Button(line, text=text, command=command).pack(side=tk.LEFT, padx=(0, 6))

        profile_box = ttk.LabelFrame(body, text="配置方案", padding=6)
        profile_box.pack(fill=tk.X, pady=(0, 8))
        profile_line = ttk.Frame(profile_box)
        profile_line.pack(fill=tk.X)
        ttk.Label(profile_line, text="当前方案", width=18).pack(side=tk.LEFT)
        self.profile_var = tk.StringVar(value=str(config.get("active_profile", "配置1")))
        self.profile_combo = ttk.Combobox(
            profile_line,
            textvariable=self.profile_var,
            values=config.list_profiles(),
            state="normal",
            width=20,
        )
        self.profile_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(profile_line, text="加载方案", command=self._load_profile).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(profile_line, text="保存到方案", command=self._save_profile).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(body, text="系统与依赖", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
        check("启动时检测依赖", "dependency_check_on_startup")
        check("自动安装 Python 包", "dependency_auto_install_python")
        check("自动下载 FFmpeg", "dependency_auto_install_ffmpeg")
        check("自动安装 Chrome", "dependency_auto_install_browser")
        check("启动时检查更新", "update_check_on_startup")
        row("反馈入口 URL", "feedback_issue_url", "", width=48)
        row("PIP 镜像", "dependency_pip_index_url", "", width=48)
        row("PIP 额外参数", "dependency_pip_extra_args", "", width=48)
        row("PIP 超时秒数", "dependency_pip_timeout_seconds", "1800")
        row("FFmpeg 下载地址", "dependency_ffmpeg_url", "", width=48)
        row("更新清单 URL", "update_manifest_url", "", width=48)
        row("依赖检测摘要", "dependency_last_report", "", width=48)
        buttons(("检测/安装依赖", self._manual_dependency_check), ("提交反馈", self._open_feedback_url))

        ttk.Separator(body).pack(fill=tk.X, pady=8)
        ttk.Label(body, text="TTS", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
        combo("Provider", "tts_provider", ["edge", "voicevox", "openai", "azure", "elevenlabs", "custom", "silent"], "edge")
        combo("常用音色", "tts_voice", EDGE_MULTI_VOICES, config.tts_voice)
        row("语速", "tts_rate", "+0%")
        row("音量倍率", "tts_volume", "1.0")
        model_combo("TTS 模型名", "tts_model", "tts-1", ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
        row("情感/指令", "tts_emotion", "")
        row("TTS Base URL/中转", "tts_base_url", "")
        row("TTS API Key", "tts_api_key", "")
        row("TTS 单段超时秒", "tts_segment_timeout_seconds", "180")
        row("TTS 心跳日志秒", "tts_heartbeat_seconds", "30")
        buttons(("检测 TTS API / 识别模型", lambda: self._probe_api("tts")))

        ttk.Separator(body).pack(fill=tk.X, pady=8)
        ttk.Label(body, text="图片与提示词", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W)
        check("AI 扫文清理", "ai_text_clean_enabled")
        row("扫文批大小", "ai_text_clean_batch_chars", "6000")
        textrow("扫文清理提示词", "ai_text_clean_prompt", str(config.get("ai_text_clean_prompt", "")), height=4)
        combo("图片 Provider", "image_provider", ["placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"], "placeholder")
        row("图片 Base URL/中转", "image_base_url", "")
        row("图片 API Key", "image_api_key", "")
        model_combo("图片模型名", "image_model", "", ["gpt-image-1", "dall-e-3"])
        row("图片 API 超时秒", "image_api_timeout_seconds", "300")
        row("风格后缀", "llm_image_style_suffix", "")
        row("分镜 LLM Provider", "llm_provider", "openai")
        model_combo("分镜模型", "llm_model", "gpt-4o-mini", ["gpt-4o-mini", "gpt-4o", "deepseek-chat"])
        row("分镜 Base URL/中转", "llm_base_url", "")
        row("分镜 API Key", "llm_api_key", "")
        buttons(
            ("检测分镜 LLM", lambda: self._probe_api("llm")),
            ("检测图片 API", lambda: self._probe_api("image")),
        )

        ttk.Separator(body).pack(fill=tk.X, pady=8)
        ttk.Label(body, text="封面", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W)
        check("自动生成封面", "cover_enabled")
        combo("封面 Provider", "cover_provider", ["same_as_image", "placeholder", "comfyui", "sdwebui", "openai", "replicate", "aliyun", "custom"], "same_as_image")
        row("封面 Base URL/中转", "cover_base_url", "")
        row("封面 API Key", "cover_api_key", "")
        model_combo("封面模型名", "cover_model", "", ["gpt-image-1", "dall-e-3"])
        buttons(("检测封面 API", lambda: self._probe_api("cover")))
        row("封面宽", "cover_width", "1280")
        row("封面高", "cover_height", "720")
        row("封面标题字号", "cover_title_size", "72")
        row("封面提示词模板", "cover_prompt_template", str(config.get("cover_prompt_template", "")), width=48)

        ttk.Separator(body).pack(fill=tk.X, pady=8)
        ttk.Label(body, text="画面节奏 / 视频", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W)
        combo("配图模式", "pacing_mode", ["by_duration", "by_sentence", "by_paragraph", "fixed_count"], "by_duration")
        row("每张图秒数", "pacing_seconds_per_image", "6")
        row("固定图片数", "pacing_fixed_count", "10")
        row("视频宽", "video_width", "1920")
        row("视频高", "video_height", "1080")
        row("FPS", "video_fps", "30")
        combo("视频编码器", "video_encoder", ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"], "libx264")
        row("编码预设", "video_encoder_preset", "veryfast")
        row("编码质量", "video_encoder_quality", "20")
        combo("图片动作", "video_motion", ["随机", "上下移动", "左右移动", "向上推进", "向下推进", "向左推进", "向右推进", "轻微缩放", "静态"], "上下移动")
        combo("移动曲线", "video_motion_curve", ["ease", "linear"], "ease")
        row("移动周期秒(0=整段)", "video_motion_cycle_seconds", "0")
        combo("转场", "video_transition", ["随机", "none", "fade", "fadeblack", "fadewhite"], "none")
        row("转场秒数", "video_transition_duration", "0.4")
        check("长视频稳定模式", "video_long_mode")
        check("Ken Burns 移动特效", "ken_burns")
        combo("字幕位置", "video_subtitle_position", ["下边", "中间", "上边"], "下边")
        row("字幕字体", "video_subtitle_font", "Microsoft YaHei")
        row("字幕字号", "video_subtitle_size", "48")
        row("字幕颜色", "video_subtitle_color", "#FFFFFF")
        row("描边颜色", "video_subtitle_outline_color", "#000000")
        row("描边宽度", "video_subtitle_outline", "4")
        row("阴影深度", "video_subtitle_shadow", "1")
        row("字幕边距", "video_subtitle_margin_v", "60")
        row("每行字数", "video_subtitle_chars_per_line", "24")
        row("最多行数", "video_subtitle_max_lines", "2")
        check("字幕粗体", "video_subtitle_bold")
        check("字幕斜体", "video_subtitle_italic")
        buttons(("预览字幕样式", self._preview_subtitle))
        check("内嵌字幕（强制）", "video_subtitle")
        check("导出 SRT", "video_external_subtitle")

        ttk.Separator(body).pack(fill=tk.X, pady=8)
        ttk.Label(body, text="批量与上传", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor=tk.W)
        check("自动短说明标题", "short_title_enabled")
        row("短标题最少字", "short_title_min_chars", "20")
        row("短标题最多字", "short_title_max_chars", "30")
        textrow("短标题提示词", "short_title_prompt", str(config.get("short_title_prompt", "")), height=3)
        check("首次启动自动检测", "hardware_autotune_enabled")
        row("硬件检测摘要", "hardware_autotune_summary", "", width=48)
        buttons(("检测电脑并自动配置并发", self._manual_hardware_autotune))
        row("同时任务数", "max_concurrent_jobs", "1")
        row("外部 API 并发", "max_concurrent_external_api", "2")
        row("FFmpeg 并发", "max_concurrent_ffmpeg", "1")
        row("TTS 并行数", "max_parallel_tts", "2")
        row("图片并行数", "max_parallel_images", "2")
        row("视频片段并行数", "max_parallel_video_clips", "1")
        check("TTS 与图片并行", "pipeline_overlap_tts_images")
        check("生成后自动上传 YouTube", "upload_enabled")
        combo("上传方案", "browser_active_profile", _upload_profile_names(), str(config.get("browser_active_profile", "无创收精简流程")))
        combo("上传流程", "browser_flow", ["simple", "full"], "simple")
        combo("公开范围", "youtube_visibility", ["PRIVATE", "UNLISTED", "PUBLIC"], "PRIVATE")
        row("上传标题模板", "youtube_title_template", "💥【爆款短文】《{clean_title}》{intro} #小说 #推文")
        row("上传标题字数", "youtube_title_max_chars", "100")
        textrow("说明模板", "youtube_description", "", height=4)
        row("固定 Tags", "youtube_tags", "")
        row("上传政策", "browser_upload_policy", "BTRA")
        row("广告间隔秒", "browser_ad_interval", "60")
        row("广告起始秒", "browser_ad_start", "0")

        ttk.Label(
            body,
            text="上传标题可用：{clean_title}=去作者名书名，{title}=原标题，{short_title}=短说明标题。simple=无创收精简流程，full=完整创收/广告/分级流程。",
            foreground="#666",
            wraplength=520,
        ).pack(anchor=tk.W, pady=(4, 14))

    def _config_key_sets(self):
        int_keys = {
            "pacing_fixed_count", "video_width", "video_height", "video_fps",
            "video_encoder_quality", "ai_text_clean_batch_chars",
            "image_api_timeout_seconds", "tts_segment_timeout_seconds", "tts_heartbeat_seconds",
            "max_concurrent_jobs", "max_concurrent_external_api", "max_concurrent_ffmpeg",
            "max_parallel_tts", "max_parallel_images", "max_parallel_video_clips",
            "browser_ad_interval", "browser_ad_start", "dependency_pip_timeout_seconds",
            "cover_width", "cover_height", "cover_title_size",
            "video_subtitle_size", "video_subtitle_margin_v", "video_subtitle_margin_lr",
            "video_subtitle_chars_per_line", "video_subtitle_max_lines",
            "short_title_min_chars", "short_title_max_chars", "youtube_title_max_chars",
        }
        float_keys = {
            "tts_volume", "pacing_seconds_per_image", "video_motion_cycle_seconds",
            "video_transition_duration", "cover_title_area_ratio",
            "video_subtitle_outline", "video_subtitle_shadow", "video_subtitle_spacing",
        }
        bool_keys = {
            "video_long_mode", "ken_burns", "video_subtitle", "video_external_subtitle",
            "upload_enabled", "cover_enabled", "video_subtitle_bold", "video_subtitle_italic",
            "short_title_enabled", "pipeline_overlap_tts_images", "ai_text_clean_enabled",
            "hardware_autotune_enabled",
            "dependency_check_on_startup", "dependency_auto_install_python",
            "dependency_auto_install_ffmpeg", "dependency_auto_install_browser",
            "update_check_on_startup",
        }
        return int_keys, float_keys, bool_keys

    def _apply_config_form(self, save_profile: bool = True) -> str:
        int_keys, float_keys, bool_keys = self._config_key_sets()
        for key, var in self.vars.items():
            value = var.get()
            if key in bool_keys:
                value = str(value) == "开启"
            elif key in int_keys:
                try:
                    value = int(float(value))
                except Exception:
                    value = int(config.get(key, 0) or 0)
            elif key in float_keys:
                try:
                    value = float(value)
                except Exception:
                    value = float(config.get(key, 0) or 0)
            config.set(key, value)
        config.set("video_subtitle", True)
        if "video_subtitle" in self.vars:
            self.vars["video_subtitle"].set("开启")
        profile_var = getattr(self, "profile_var", None)
        profile_name = str(profile_var.get() if profile_var is not None else "配置1").strip() or "配置1"
        config.set("active_profile", profile_name)
        if save_profile:
            profile_name = config.save_profile(profile_name)
            if hasattr(self, "profile_var"):
                self.profile_var.set(profile_name)
            if hasattr(self, "profile_combo"):
                self.profile_combo.configure(values=config.list_profiles())
        config.save()
        return profile_name

    def _save_config(self):
        profile_name = self._apply_config_form(save_profile=True)
        messagebox.showinfo("已保存", f"配置已写入 data/settings.json，并保存到方案：{profile_name}")

    def _save_profile(self):
        profile_name = self._apply_config_form(save_profile=True)
        messagebox.showinfo("已保存", f"已保存到配置方案：{profile_name}")

    def _load_profile(self):
        profile_var = getattr(self, "profile_var", None)
        profile_name = str(profile_var.get() if profile_var is not None else "配置1").strip() or "配置1"
        try:
            loaded = config.load_profile(profile_name)
            config.save()
            self._refresh_config_form()
            if hasattr(self, "profile_var"):
                self.profile_var.set(loaded)
            if hasattr(self, "profile_combo"):
                self.profile_combo.configure(values=config.list_profiles())
            messagebox.showinfo("已加载", f"已切换到配置方案：{loaded}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def _refresh_config_form(self):
        _, _, bool_keys = self._config_key_sets()
        for key, var in self.vars.items():
            value = config.get(key, "")
            if key in bool_keys:
                var.set("开启" if bool(value) else "关闭")
            else:
                var.set(str(value))

    def _ui_value(self, key: str, default: str = "") -> str:
        var = self.vars.get(key)
        return str(var.get() if var is not None else config.get(key, default) or "").strip()

    def _emit_dependency_startup_logs(self):
        lines = []
        try:
            if report_to_lines is not None:
                lines = report_to_lines(_DEPENDENCY_STARTUP_REPORT)
            else:
                lines = list(_DEPENDENCY_STARTUP_REPORT.get("logs") or [])
        except Exception:
            lines = list(_DEPENDENCY_STARTUP_REPORT.get("logs") or [])
        for line in lines[-12:]:
            self._append_probe_log(str(line))
        self._refresh_config_form()

    def _maybe_start_dependency_check(self):
        if not bool(config.get("dependency_check_on_startup", True)):
            return
        self._run_dependency_check(silent=True)

    def _manual_dependency_check(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        self._run_dependency_check(silent=False)

    def _open_feedback_url(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        url = str(config.get("feedback_issue_url", "") or "").strip()
        if not url:
            messagebox.showwarning("未配置反馈入口", "请先填写反馈入口 URL。")
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            messagebox.showerror("无法打开反馈入口", str(exc))

    def _run_dependency_check(self, *, silent: bool):
        if ensure_dependencies is None:
            messagebox.showerror("依赖检测不可用", str(_DEPENDENCY_STARTUP_REPORT.get("summary", "依赖检测模块加载失败")))
            return
        if self._dependency_check_running:
            if not silent:
                messagebox.showinfo("依赖检测", "依赖检测正在运行，请稍等。")
            return
        self._dependency_check_running = True
        self._append_probe_log("[依赖检测] 正在检测 Python 包 / FFmpeg / Chrome / 上传脚本...")

        def worker():
            try:
                report = ensure_dependencies(scope="full")
            except Exception as exc:
                self.root.after(0, lambda: self._show_dependency_check_error(exc, silent))
                return
            self.root.after(0, lambda: self._show_dependency_check_result(report, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _show_dependency_check_error(self, exc: Exception, silent: bool):
        self._dependency_check_running = False
        self._append_probe_log(f"[依赖检测] 失败：{exc}")
        if not silent:
            messagebox.showerror("依赖检测失败", str(exc))

    def _show_dependency_check_result(self, report: dict, silent: bool):
        self._dependency_check_running = False
        self._refresh_config_form()
        lines = report_to_lines(report) if report_to_lines is not None else list(report.get("logs") or [])
        for line in lines[-18:]:
            self._append_probe_log(str(line))
        summary = str(report.get("summary") or "依赖检测完成")
        if not silent:
            if bool(report.get("ok")):
                messagebox.showinfo("依赖检测完成", summary)
            else:
                messagebox.showwarning("依赖仍有异常", summary)

    def _maybe_start_hardware_autotune(self):
        if not bool(config.get("hardware_autotune_enabled", True)):
            return
        self._run_hardware_autotune(force=False, silent=True)

    def _maybe_check_update_on_startup(self):
        if not bool(config.get("update_check_on_startup", True)):
            return
        if not str(config.get("update_manifest_url", "") or "").strip():
            return

        def worker():
            try:
                from app.updater import check_for_update

                info = check_for_update(config)
            except Exception as exc:
                self.root.after(0, lambda: self._append_probe_log(f"[软件更新] 检查失败：{exc}"))
                return
            if info:
                self.root.after(0, lambda: self._notify_update_available(info))

        threading.Thread(target=worker, daemon=True).start()

    def _notify_update_available(self, info):
        self._append_probe_log(f"[软件更新] 发现新版本 v{info.version}")
        tab = getattr(self, "update_tab", None)
        if tab is not None:
            tab.check_done(info)
            nb = getattr(self, "_right_notebook", None)
            if nb is not None:
                nb.select(tab.frame)
        messagebox.showinfo("发现新版本", f"发现新版本 v{info.version}，可在「软件更新」页下载。")

    def _manual_hardware_autotune(self):
        try:
            self._apply_config_form(save_profile=False)
        except Exception:
            pass
        self._run_hardware_autotune(force=True, silent=False)

    def _run_hardware_autotune(self, *, force: bool, silent: bool):
        if not silent or not bool(config.get("hardware_autotune_done", False)):
            self._append_probe_log("[硬件检测] 正在扫描 CPU / 内存 / 显卡 / FFmpeg 硬编能力...")

        def worker():
            try:
                changed, result = run_startup_autotune(force=force)
            except Exception as exc:
                self.root.after(0, lambda: self._show_hardware_autotune_error(exc, silent))
                return
            self.root.after(0, lambda: self._show_hardware_autotune_result(changed, result, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _show_hardware_autotune_error(self, exc: Exception, silent: bool):
        text = f"[硬件检测] 失败：{exc}"
        self._append_probe_log(text)
        if not silent:
            messagebox.showerror("硬件检测失败", str(exc))

    def _show_hardware_autotune_result(self, changed: bool, result: dict, silent: bool):
        if silent and str(result.get("status") or "") in {"disabled", "skipped"}:
            return
        self._refresh_config_form()
        if hasattr(self, "profile_combo"):
            self.profile_combo.configure(values=config.list_profiles())
        message = str(result.get("message") or result.get("status") or "硬件检测完成")
        suffix = "已更新配置。" if changed else "配置已是推荐值。"
        self._append_probe_log(f"[硬件检测] {message} {suffix}")
        if not silent:
            messagebox.showinfo("硬件检测完成", f"{message}\n\n{suffix}")

    def _probe_api(self, kind: str):
        title = {
            "tts": "TTS API",
            "llm": "分镜 LLM",
            "image": "图片 API",
            "cover": "封面 API",
        }.get(kind, "API")
        self._append_probe_log(f"[API检测] {title} 检测中...")

        def worker():
            try:
                if kind == "tts":
                    result = probe_tts(
                        provider=self._ui_value("tts_provider", "edge"),
                        base_url=self._ui_value("tts_base_url"),
                        api_key=self._ui_value("tts_api_key"),
                        voice=self._ui_value("tts_voice"),
                        model=self._ui_value("tts_model"),
                    )
                elif kind == "llm":
                    result = probe_llm(
                        provider=self._ui_value("llm_provider", "openai"),
                        base_url=self._ui_value("llm_base_url"),
                        api_key=self._ui_value("llm_api_key"),
                        model=self._ui_value("llm_model"),
                    )
                elif kind == "image":
                    result = probe_image(
                        provider=self._ui_value("image_provider", "placeholder"),
                        base_url=self._ui_value("image_base_url"),
                        api_key=self._ui_value("image_api_key"),
                        model=self._ui_value("image_model"),
                        name="图片 API",
                    )
                elif kind == "cover":
                    provider = self._ui_value("cover_provider", "same_as_image")
                    if provider in {"", "same_as_image", "同配图", "同图片"}:
                        provider = self._ui_value("image_provider", "placeholder")
                    result = probe_image(
                        provider=provider,
                        base_url=self._ui_value("cover_base_url") or self._ui_value("image_base_url"),
                        api_key=self._ui_value("cover_api_key") or self._ui_value("image_api_key"),
                        model=self._ui_value("cover_model") or self._ui_value("image_model"),
                        name="封面 API",
                    )
                else:
                    raise ValueError(f"unknown probe kind: {kind}")
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror("API 检测失败", str(exc)))
                return
            self.root.after(0, lambda: self._show_probe_result(kind, result))

        threading.Thread(target=worker, daemon=True).start()

    def _append_probe_log(self, text: str):
        try:
            self.log_text.insert(tk.END, text.rstrip() + "\n")
            self.log_text.see(tk.END)
        except Exception:
            pass

    def _show_probe_result(self, kind: str, result):
        text = result.to_text()
        target_key = {"llm": "llm_model", "image": "image_model", "cover": "cover_model"}.get(kind)
        if kind == "tts" and self._ui_value("tts_provider") in {"openai", "custom"}:
            target_key = "tts_model"
        elif kind == "tts" and self._ui_value("tts_provider") in {"edge", "voicevox", "elevenlabs"}:
            target_key = "tts_voice"
        if target_key and result.models:
            widget = self.combo_widgets.get(target_key)
            if widget is not None:
                widget.configure(values=result.models)
        if target_key and result.suggested_model and not self._ui_value(target_key):
            var = self.vars.get(target_key)
            if var is not None:
                var.set(result.suggested_model)
                text += f"\n\n已自动填入 {target_key}: {result.suggested_model}"
        self._append_probe_log(text)
        if result.ok:
            messagebox.showinfo("API 检测通过", text)
        else:
            messagebox.showwarning("API 检测未通过", text)

    def _preview_subtitle(self):
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
        except Exception as exc:
            messagebox.showerror("无法预览", f"Pillow/ImageTk 不可用: {exc}")
            return

        width, height = 720, 405
        img = Image.new("RGB", (width, height), (28, 34, 44))
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / max(1, height - 1)
            color = (int(28 + 34 * t), int(34 + 25 * t), int(44 + 40 * t))
            draw.line((0, y, width, y), fill=color)
        draw.rectangle((0, int(height * 0.68), width, height), fill=(18, 18, 22))

        sample = "蒙头睡了十三个小时 醒来一看"
        font_size = max(16, int(int(float(self._ui_value("video_subtitle_size", "48"))) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        font = self._preview_font(self._ui_value("video_subtitle_font", "Microsoft YaHei"), font_size)
        max_chars = max(8, int(float(self._ui_value("video_subtitle_chars_per_line", "24"))))
        lines = self._wrap_preview_text(sample, max_chars)[: max(1, int(float(self._ui_value("video_subtitle_max_lines", "2"))))]
        text = "\n".join(lines)

        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=8)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = max(16, int(int(float(self._ui_value("video_subtitle_margin_v", "60"))) * height / max(1, int(config.get("video_height", 1920) or 1920))))
        pos = self._ui_value("video_subtitle_position", "下边")
        if "上" in pos:
            y = margin
        elif "中" in pos:
            y = (height - th) / 2
        else:
            y = height - margin - th
        x = (width - tw) / 2
        fill = self._rgb(self._ui_value("video_subtitle_color", "#FFFFFF"), (255, 255, 255))
        outline_fill = self._rgb(self._ui_value("video_subtitle_outline_color", "#000000"), (0, 0, 0))
        stroke = max(0, int(float(self._ui_value("video_subtitle_outline", "4")) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        shadow = max(0, int(float(self._ui_value("video_subtitle_shadow", "1")) * width / max(1, int(config.get("video_width", 1080) or 1080))))
        if shadow:
            draw.multiline_text((x + shadow, y + shadow), text, font=font, fill=(0, 0, 0), spacing=8, align="center")
        draw.multiline_text((x, y), text, font=font, fill=fill, spacing=8, align="center", stroke_width=stroke, stroke_fill=outline_fill)

        win = tk.Toplevel(self.root)
        win.title("字幕样式预览")
        photo = ImageTk.PhotoImage(img)
        label = ttk.Label(win, image=photo)
        label.image = photo
        label.pack(padx=10, pady=10)

    def _preview_font(self, font_name: str, size: int):
        from PIL import ImageFont

        fonts_dir = Path(r"C:\Windows\Fonts")
        candidates = [
            font_name,
            fonts_dir / "msyhbd.ttc",
            fonts_dir / "msyh.ttc",
            fonts_dir / "simhei.ttf",
            fonts_dir / "NotoSansSC-VF.ttf",
            "msyhbd.ttc",
            "msyh.ttc",
            "simhei.ttf",
        ]
        for candidate in candidates:
            try:
                return ImageFont.truetype(str(candidate), size)
            except Exception:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        text = str(value or "").strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            return fallback
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        except ValueError:
            return fallback

    @staticmethod
    def _wrap_preview_text(text: str, chars_per_line: int) -> list[str]:
        rows: list[str] = []
        current = ""
        for ch in text:
            current += ch
            if len(current) >= chars_per_line:
                rows.append(current)
                current = ""
        if current:
            rows.append(current)
        return rows or [text]

    def _search_books(self):
        keyword = self.search_var.get().strip()
        if not keyword:
            return
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_tree.insert("", tk.END, values=("搜索中...", "", "", "", ""))

        def worker():
            try:
                items = pr.search_books(keyword, site="qingtian", source="番茄", media="小说", limit=20, enrich_latest=True)
                rows = [(x.title, x.author, x.source, x.latest_chapter, x.ref) for x in items]
            except Exception as exc:
                rows = [(f"搜索失败: {exc}", "", "", "", "")]
            self.root.after(0, lambda: self._render_search(rows))

        threading.Thread(target=worker, daemon=True).start()

    def _render_search(self, rows):
        self.search_tree.delete(*self.search_tree.get_children())
        self._search_refs.clear()
        for row in rows:
            iid = self.search_tree.insert("", tk.END, values=row)
            self._search_refs[iid] = row[-1]

    def _add_selected_search(self):
        for iid in self.search_tree.selection():
            ref = self._search_refs.get(iid, "")
            values = self.search_tree.item(iid, "values")
            if ref:
                self._create_queued_job(str(values[0] or "book"), ref)

    def _add_manual_text(self):
        text = self.manual_text.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("没有文本", "请先粘贴文章正文。")
            return
        title = self.manual_title.get().strip() or next((x.strip() for x in text.splitlines() if x.strip()), "article")
        path = _save_import_text(title, text)
        self._create_queued_job(title, str(path))
        self.manual_text.delete("1.0", tk.END)

    def _import_txt_files(self):
        files = filedialog.askopenfilenames(title="选择 TXT 文件", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        for file in files:
            path = Path(file)
            self._create_queued_job(path.stem, str(path))

    def _create_queued_job(self, title: str, input_text: str):
        job_id = pr.new_job_id("article")
        job_dir = pr.job_dir_for(job_id)
        pr.write_status(job_dir, job_id=job_id, title=title, input=input_text, stage="queued", progress=0.0)
        pr.append_log(job_dir, f"queued: {title}")
        self._refresh_jobs()

    def _selected_job_ids(self) -> list[str]:
        ids = []
        for iid in self.job_tree.selection():
            values = self.job_tree.item(iid, "values")
            if values:
                ids.append(str(values[0]))
        return ids

    def _start_selected(self):
        for job_id in self._selected_job_ids():
            self._start_job(job_id)

    def _start_all_pending(self):
        for row in pr.list_jobs(limit=500):
            if row.get("stage") in {"queued", "failed", "missing"} and not row.get("worker_alive"):
                self._start_job(row["job_id"])

    def _start_job(self, job_id: str):
        st = pr.load_status(job_id)
        if st.get("worker_alive"):
            return
        input_text = st.get("input", "")
        if not input_text:
            messagebox.showwarning("缺少输入", f"{job_id} 没有 input")
            return
        try:
            pr.start_worker(input_text, job_id=job_id, resume=True)
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
        self._refresh_jobs()

    def _clear_selected_media_cache(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        if not messagebox.askyesno("确认清除缓存", "会删除选中任务的 images/cover/final.mp4，下次启动会重新调用生图 API 和合成视频。继续吗？"):
            return
        errors = []
        for job_id in ids:
            try:
                pr.clear_job_media_cache(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        if errors:
            messagebox.showerror("清除失败", "\n".join(errors))

    def _stop_selected_jobs(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        running = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if not running:
            messagebox.showinfo("无需停止", "选中的任务没有正在运行的 worker。")
            return
        if not messagebox.askyesno("确认停止任务", f"将强制停止 {len(running)} 个正在运行的任务。已生成的中间文件会保留，可之后续跑。继续吗？"):
            return
        errors = []
        for job_id in running:
            try:
                pr.stop_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._refresh_jobs()
        self._update_log()
        if errors:
            messagebox.showerror("停止失败", "\n".join(errors))

    def _delete_selected_jobs(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        running = [job_id for job_id in ids if pr.load_status(job_id).get("worker_alive")]
        if running:
            message = f"将删除 {len(ids)} 个任务目录，其中 {len(running)} 个正在运行，会先强制停止再删除。此操作不可恢复。继续吗？"
        else:
            message = f"将删除 {len(ids)} 个任务目录。此操作不可恢复。继续吗？"
        if not messagebox.askyesno("确认删除任务", message):
            return
        errors = []
        for job_id in ids:
            try:
                pr.delete_job(job_id, stop_running=True)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._selected_job = ""
        self._refresh_jobs()
        if errors:
            messagebox.showerror("删除失败", "\n".join(errors))

    def _delete_finished_jobs(self):
        rows = pr.list_jobs(limit=1000)
        ids = [row["job_id"] for row in rows if row.get("stage") in {"completed", "failed", "missing", "broken"} and not row.get("worker_alive")]
        if not ids:
            messagebox.showinfo("无需清理", "没有已结束任务可清理。")
            return
        if not messagebox.askyesno("确认清空", f"将删除 {len(ids)} 个已结束/失败任务目录。继续吗？"):
            return
        errors = []
        for job_id in ids:
            try:
                pr.delete_job(job_id)
            except Exception as exc:
                errors.append(f"{job_id}: {exc}")
        self._selected_job = ""
        self._refresh_jobs()
        if errors:
            messagebox.showerror("清空失败", "\n".join(errors))

    def _refresh_jobs(self):
        self.job_tree.delete(*self.job_tree.get_children())
        for row in pr.list_jobs(limit=500):
            st = pr.load_status(row["job_id"])
            worker = f"PID {st.get('worker_pid')}" if st.get("worker_alive") else ""
            values = (
                row["job_id"],
                row.get("stage_display", row.get("stage", "")),
                f"{float(row.get('progress') or 0) * 100:.0f}%",
                worker,
                row.get("title", ""),
                row.get("video", ""),
                st.get("youtube_url", ""),
            )
            self.job_tree.insert("", tk.END, iid=row["job_id"], values=values)

    def _poll_jobs(self):
        self._refresh_jobs()
        self.root.after(2500, self._poll_jobs)

    def _on_job_select(self, _event=None):
        ids = self._selected_job_ids()
        if ids:
            self._selected_job = ids[0]
            self._update_log()

    def _poll_log(self):
        self._update_log()
        self.root.after(1500, self._poll_log)

    def _update_log(self):
        if not self._selected_job:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.insert(tk.END, "请在左侧选择一个任务以查看实时日志")
            self.log_text.configure(state=tk.DISABLED)
            return
        text = pr.tail_log(self._selected_job, lines=500)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.NORMAL)

    def _open_selected_dir(self):
        ids = self._selected_job_ids()
        if not ids:
            return
        path = pr.job_dir_for(ids[0])
        path.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(str(path))


def main():
    root = tk.Tk()
    PipelineGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
