"""Resolve ffmpeg and ffprobe for portable Windows installs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGACY_DRAMA_FFMPEG = Path(r"f:\Manao\drama_pipeline_release\tools\ffmpeg")


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _env_key(name: str) -> str:
    return f"MEDIA_{name.upper()}_BIN"


def _candidates(name: str) -> list[Path]:
    exe = _exe(name)
    return [
        ROOT / "runtime" / "ffmpeg" / exe,
        ROOT / "tools" / "ffmpeg" / exe,
        ROOT / "tools" / "ffmpeg" / "bin" / exe,
        ROOT / "vendor" / "ffmpeg" / exe,
        LEGACY_DRAMA_FFMPEG / exe,
    ]


def _resolve(name: str) -> str:
    env_value = os.environ.get(_env_key(name))
    if env_value and Path(env_value).exists():
        return env_value
    found = shutil.which(name)
    if found:
        return found
    for candidate in _candidates(name):
        if candidate.exists():
            return str(candidate)
    return name


def ffmpeg_path() -> str:
    return _resolve("ffmpeg")


def ffprobe_path() -> str:
    return _resolve("ffprobe")
