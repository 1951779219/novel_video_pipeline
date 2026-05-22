"""YouTube 浏览器上传：直接复用 drama_pipeline_release 的 stage5_upload_browser.py。
用 importlib 按文件路径加载，避免与本工程 app 包同名冲突。

要求：Chrome 已通过 --remote-debugging-port=9222 启动且登录 YouTube Studio。
可双击 drama_pipeline_release/Chrome调试模式启动.bat 启动 Chrome。
"""
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

_DRAMA_CANDIDATES = [
    Path(__file__).resolve().parent / "vendor" / "stage5_upload_browser.py",
    Path(r"F:\Manao\drama_pipeline_app\app\stages\stage5_upload_browser.py"),
    Path(r"F:\Manao\drama_pipeline_release\app\stages\stage5_upload_browser.py"),
]

_module = None

def _load():
    global _module
    if _module is not None:
        return _module
    drama_file = next((p for p in _DRAMA_CANDIDATES if p.exists()), None)
    if drama_file is None:
        raise RuntimeError("找不到浏览器上传脚本: " + " / ".join(str(p) for p in _DRAMA_CANDIDATES))
    spec = importlib.util.spec_from_file_location("drama_stage5_upload_browser", drama_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _module = mod
    return mod


@dataclass
class _Job:
    cancelled: bool = False
    mode: str = "full"

    def is_cancelled(self) -> bool:
        return self.cancelled


def upload_to_youtube(video_path: Path, title: str, description: str = "",
                       visibility: str = "PRIVATE",
                       cover_path: Optional[Path] = None,
                       flow: str = "simple",
                       upload_policy: str = "BTRA",
                       ad_interval: int = 60,
                       ad_start: int = 0,
                       auto_restart: bool = True,
                       stall_timeout_min: int = 10,
                       op_speed: str = "normal",
                       on_log: Callable = print,
                       on_progress: Callable = lambda x: None) -> Optional[str]:
    """visibility: PUBLIC / UNLISTED / PRIVATE"""
    mod = _load()

    fake_config = SimpleNamespace(
        browser_profiles="[]",
        browser_active_profile="",
        browser_upload_policy=upload_policy,
        browser_ad_interval=int(ad_interval),
        browser_ad_start=int(ad_start),
        browser_visibility=visibility,
        browser_auto_restart=bool(auto_restart),
        browser_stall_timeout_min=int(stall_timeout_min),
        browser_op_speed=op_speed,
        youtube_description=description,
    )
    orig = mod._get_active_profile
    mod._get_active_profile = lambda c: {
        "name": "novel", "flow": flow,
        "upload_policy": upload_policy,
        "ad_interval": int(ad_interval), "ad_start": int(ad_start),
        "visibility": visibility,
        "description": description,
    }
    try:
        return mod.upload_via_browser(
            _Job(), fake_config, video_path, title, cover_path,
            on_log=on_log, on_progress=on_progress,
        )
    finally:
        mod._get_active_profile = orig
