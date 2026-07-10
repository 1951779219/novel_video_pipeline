"""Hardware-aware startup tuning for ordinary production PCs."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, config
from app.utils.ffmpeg import ffmpeg_path


AUTOTUNE_REPORT = DATA_DIR / "hardware_autotune.json"


def run_startup_autotune(*, force: bool = False) -> tuple[bool, dict[str, Any]]:
    """Scan the machine and apply conservative concurrency settings.

    When force is False this only runs once, controlled by
    hardware_autotune_done. The manual GUI button calls it with force=True.
    """
    if not force and not bool(config.get("hardware_autotune_enabled", True)):
        return False, {
            "status": "disabled",
            "message": "硬件自动检测已关闭，本次跳过。",
        }
    if not force and bool(config.get("hardware_autotune_done", False)):
        quick_snapshot = quick_hardware_snapshot()
        current_signature = hardware_signature(quick_snapshot)
        saved_signature = str(config.get("hardware_autotune_signature", "") or "")
        if saved_signature and saved_signature == current_signature:
            return False, {
                "status": "skipped",
                "message": str(config.get("hardware_autotune_summary", "硬件已检测过，本次跳过。")),
            }
        # A copied install may carry another machine's settings.json. If the
        # signature is missing or changed, treat this PC as a first launch.

    snapshot = scan_hardware()
    settings = recommend_settings(snapshot)
    changed_keys: dict[str, dict[str, Any]] = {}
    for key, value in settings.items():
        old_value = config.get(key)
        if old_value != value:
            changed_keys[key] = {"old": old_value, "new": value}
        config.set(key, value)

    summary = build_summary(snapshot, settings)
    signature = hardware_signature(snapshot)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config.set("hardware_autotune_done", True)
    config.set("hardware_autotune_at", now)
    config.set("hardware_autotune_signature", signature)
    config.set("hardware_autotune_summary", summary)
    _save_config_and_profile()

    result = {
        "status": "applied",
        "at": now,
        "snapshot": snapshot,
        "settings": settings,
        "changed_keys": changed_keys,
        "message": summary,
    }
    _write_report(result)
    return bool(changed_keys), result


def quick_hardware_snapshot() -> dict[str, Any]:
    mac_info = _detect_macos_hardware()
    nvidia_gpus = _detect_nvidia_smi()
    wmi_gpu_names = _detect_windows_gpu_names()
    gpu_names = list(mac_info.get("gpu_names") or []) + [g["name"] for g in nvidia_gpus] + [
        name for name in wmi_gpu_names if name and name not in {g["name"] for g in nvidia_gpus}
    ]
    return {
        "platform": sys.platform,
        "machine_name": mac_info.get("machine_name", ""),
        "chip_type": mac_info.get("chip_type", ""),
        "gpu_cores": mac_info.get("gpu_cores", 0),
        "cpu_threads": max(1, int(os.cpu_count() or 1)),
        "ram_gb": _total_ram_gb(),
        "gpu_names": gpu_names,
    }


def hardware_signature(snapshot: dict[str, Any]) -> str:
    gpu_names = sorted({str(name).strip().lower() for name in snapshot.get("gpu_names", []) if str(name).strip()})
    signature = {
        "platform": str(snapshot.get("platform") or sys.platform),
        "machine_name": str(snapshot.get("machine_name") or ""),
        "chip_type": str(snapshot.get("chip_type") or ""),
        "cpu_threads": int(snapshot.get("cpu_threads") or 1),
        "ram_gb": round(float(snapshot.get("ram_gb") or 0.0), 1),
        "gpu_names": gpu_names[:6],
    }
    return json.dumps(signature, ensure_ascii=False, sort_keys=True)


def scan_hardware() -> dict[str, Any]:
    cpu_threads = max(1, int(os.cpu_count() or 1))
    ram_gb = _total_ram_gb()
    mac_info = _detect_macos_hardware()
    nvidia_gpus = _detect_nvidia_smi()
    wmi_gpu_names = _detect_windows_gpu_names()
    gpu_names = list(mac_info.get("gpu_names") or []) + [g["name"] for g in nvidia_gpus] + [
        name for name in wmi_gpu_names if name and name not in {g["name"] for g in nvidia_gpus}
    ]
    gpu_text = " ".join(gpu_names).lower()

    ffmpeg_bin = ffmpeg_path()
    encoders_text = _ffmpeg_encoders(ffmpeg_bin)
    encoder_candidates: list[str] = []
    if "nvidia" in gpu_text or nvidia_gpus:
        encoder_candidates.append("h264_nvenc")
    if "intel" in gpu_text or "uhd graphics" in gpu_text or "iris" in gpu_text:
        encoder_candidates.append("h264_qsv")
    if "amd" in gpu_text or "radeon" in gpu_text:
        encoder_candidates.append("h264_amf")
    if sys.platform == "darwin":
        encoder_candidates.append("h264_videotoolbox")
    for encoder in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if encoder in encoders_text and encoder not in encoder_candidates:
            encoder_candidates.append(encoder)
    if "h264_videotoolbox" in encoders_text and "h264_videotoolbox" not in encoder_candidates:
        encoder_candidates.append("h264_videotoolbox")

    encoder_tests: dict[str, bool] = {}
    for encoder in encoder_candidates[:3]:
        if encoder not in encoders_text:
            encoder_tests[encoder] = False
            continue
        encoder_tests[encoder] = _smoke_test_encoder(ffmpeg_bin, encoder)

    hardware_encoder = next((name for name, ok in encoder_tests.items() if ok), "")
    return {
        "platform": sys.platform,
        "os_name": _macos_version() if sys.platform == "darwin" else platform.platform(),
        **mac_info,
        "cpu_threads": cpu_threads,
        "ram_gb": ram_gb,
        "gpu_names": gpu_names,
        "nvidia_gpus": nvidia_gpus,
        "ffmpeg": ffmpeg_bin,
        "ffmpeg_encoders_available": sorted(
            name for name in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox") if name in encoders_text
        ),
        "encoder_tests": encoder_tests,
        "hardware_encoder": hardware_encoder,
    }


def recommend_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    cpu_threads = int(snapshot.get("cpu_threads") or 1)
    ram_gb = float(snapshot.get("ram_gb") or 0.0)
    hardware_encoder = str(snapshot.get("hardware_encoder") or "")
    has_hw = bool(hardware_encoder)

    if str(snapshot.get("platform") or "") == "darwin":
        if cpu_threads >= 16 and ram_gb >= 64:
            tts_workers = 4
            image_workers = 3
            ffmpeg_workers = 2
            probe_workers = 4
        elif cpu_threads >= 8 and ram_gb >= 16:
            tts_workers = 3
            image_workers = 2
            ffmpeg_workers = 1
            probe_workers = 3
        else:
            tts_workers = 1
            image_workers = 1
            ffmpeg_workers = 1
            probe_workers = 1
        external_api_slots = max(1, min(4, max(tts_workers, image_workers)))
        video_clip_workers = max(1, min(2, ffmpeg_workers))
        return {
            "video_encoder": hardware_encoder or "libx264",
            "video_encoder_preset": "realtime" if hardware_encoder == "h264_videotoolbox" else "veryfast",
            "video_encoder_quality": 24,
            "max_concurrent_jobs": 1,
            "max_concurrent_external_api": external_api_slots,
            "max_concurrent_ffmpeg": ffmpeg_workers,
            "max_concurrent_media_probe": probe_workers,
            "max_parallel_tts": tts_workers,
            "max_parallel_images": image_workers,
            "max_parallel_video_clips": video_clip_workers,
            "pipeline_overlap_tts_images": True,
            "worker_detached": True,
        }

    if cpu_threads <= 4 or ram_gb < 8:
        tts_workers = 1
        image_workers = 1
        ffmpeg_workers = 1
        probe_workers = 1
    elif cpu_threads <= 8 or ram_gb < 16:
        tts_workers = 2
        image_workers = 2
        ffmpeg_workers = 2 if has_hw and cpu_threads >= 8 and ram_gb >= 12 else 1
        probe_workers = 2
    elif cpu_threads >= 16 and ram_gb >= 32:
        tts_workers = 4
        image_workers = 4
        ffmpeg_workers = 3 if has_hw else 2
        probe_workers = 4
    else:
        tts_workers = 3
        image_workers = 3
        ffmpeg_workers = 2 if has_hw or cpu_threads >= 12 else 1
        probe_workers = 3

    external_api_slots = max(1, min(4, max(tts_workers, image_workers)))
    video_clip_workers = max(1, min(3, ffmpeg_workers))

    if has_hw:
        encoder = hardware_encoder
        preset = "p4" if encoder == "h264_nvenc" else "veryfast"
        quality = 23
    else:
        encoder = "libx264"
        preset = "ultrafast" if cpu_threads <= 4 or ram_gb < 8 else "veryfast"
        quality = 23

    return {
        "video_encoder": encoder,
        "video_encoder_preset": preset,
        "video_encoder_quality": quality,
        "max_concurrent_jobs": 1,
        "max_concurrent_external_api": external_api_slots,
        "max_concurrent_ffmpeg": ffmpeg_workers,
        "max_concurrent_media_probe": probe_workers,
        "max_parallel_tts": tts_workers,
        "max_parallel_images": image_workers,
        "max_parallel_video_clips": video_clip_workers,
        "pipeline_overlap_tts_images": True,
        "worker_detached": True,
    }


def build_summary(snapshot: dict[str, Any], settings: dict[str, Any]) -> str:
    cpu_threads = int(snapshot.get("cpu_threads") or 1)
    ram_gb = float(snapshot.get("ram_gb") or 0.0)
    gpu_names = snapshot.get("gpu_names") or []
    if str(snapshot.get("platform") or "") == "darwin":
        machine = str(snapshot.get("machine_name") or "Mac")
        chip = str(snapshot.get("chip_type") or "").strip()
        gpu_cores = int(snapshot.get("gpu_cores") or 0)
        unified = "统一内存" if bool(snapshot.get("unified_memory", True)) else "内存"
        gpu_text = str(gpu_names[0]) if gpu_names else (chip or "Apple GPU")
        gpu_suffix = f"，GPU {gpu_cores} 核" if gpu_cores else ""
        machine_text = f"{machine} {chip}".strip()
        hw_note = "VideoToolbox 硬编可用" if settings.get("video_encoder") == "h264_videotoolbox" else "VideoToolbox 未通过，使用 CPU 编码"
        return (
            f"识别到 Mac：{machine_text or 'Mac'}，CPU {cpu_threads} 线程，{unified} {ram_gb:.1f}GB，"
            f"GPU {gpu_text}{gpu_suffix}；{hw_note}：{settings.get('video_encoder')}；"
            f"推荐并发：API {settings.get('max_concurrent_external_api')}，FFmpeg {settings.get('max_concurrent_ffmpeg')}，"
            f"TTS {settings.get('max_parallel_tts')}，图片 {settings.get('max_parallel_images')}，"
            f"视频片段 {settings.get('max_parallel_video_clips')}。"
        )
    gpu_text = " / ".join(str(x) for x in gpu_names[:3]) if gpu_names else "未识别到独立显卡"
    encoder = str(settings.get("video_encoder") or "libx264")
    hw_note = "硬编可用" if encoder != "libx264" else "硬编未通过，使用 CPU 编码"
    return (
        f"CPU {cpu_threads} 线程，内存 {ram_gb:.1f}GB，GPU {gpu_text}；"
        f"{hw_note}：{encoder}；推荐并发：API {settings.get('max_concurrent_external_api')}，"
        f"FFmpeg {settings.get('max_concurrent_ffmpeg')}，TTS {settings.get('max_parallel_tts')}，"
        f"图片 {settings.get('max_parallel_images')}，视频片段 {settings.get('max_parallel_video_clips')}。"
    )


def _total_ram_gb() -> float:
    if sys.platform == "darwin":
        proc = _run(["sysctl", "-n", "hw.memsize"], timeout=3)
        if proc.returncode == 0:
            try:
                return round(float(proc.stdout.strip()) / (1024 ** 3), 1)
            except Exception:
                pass
    if os.name == "nt":
        try:
            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

                def __init__(self) -> None:
                    super().__init__()
                    self.dwLength = ctypes.sizeof(self)

            status = MemoryStatusEx()
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(float(status.ullTotalPhys) / (1024 ** 3), 1)
        except Exception:
            pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return round(float(page_size * pages) / (1024 ** 3), 1)
    except Exception:
        return 0.0


def _detect_nvidia_smi() -> list[dict[str, Any]]:
    proc = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=6,
    )
    if proc.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if not parts or not parts[0]:
            continue
        memory_mb = 0
        if len(parts) > 1:
            try:
                memory_mb = int(float(parts[1]))
            except Exception:
                memory_mb = 0
        gpus.append({"name": parts[0], "memory_mb": memory_mb})
    return gpus


def _detect_windows_gpu_names() -> list[str]:
    if os.name != "nt":
        return []
    proc = _run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }",
        ],
        timeout=8,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _macos_version() -> str:
    proc = _run(["sw_vers", "-productVersion"], timeout=3)
    if proc.returncode == 0 and proc.stdout.strip():
        return f"macOS {proc.stdout.strip()}"
    return "macOS"


def _detect_macos_hardware() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {}
    info: dict[str, Any] = {
        "machine_name": "",
        "machine_model": "",
        "chip_type": _sysctl_value("machdep.cpu.brand_string"),
        "gpu_names": [],
        "gpu_cores": 0,
        "metal": False,
        "unified_memory": True,
    }
    proc = _run(["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"], timeout=15)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            data = json.loads(proc.stdout)
        except Exception:
            data = {}
        hardware = (data.get("SPHardwareDataType") or [{}])[0]
        if isinstance(hardware, dict):
            info["machine_name"] = str(hardware.get("machine_name") or "")
            info["machine_model"] = str(hardware.get("machine_model") or "")
            info["chip_type"] = str(hardware.get("chip_type") or info["chip_type"] or "")
            memory_text = str(hardware.get("physical_memory") or "")
            match = re.search(r"([\d.]+)\s*GB", memory_text, re.I)
            if match:
                info["reported_memory_gb"] = float(match.group(1))
        display_rows = data.get("SPDisplaysDataType") or []
        gpu_names: list[str] = []
        for row in display_rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("sppci_model") or row.get("_name") or "").strip()
            if name and name not in gpu_names:
                gpu_names.append(name)
            cores = str(row.get("sppci_cores") or "").strip()
            if cores.isdigit():
                info["gpu_cores"] = max(int(info.get("gpu_cores") or 0), int(cores))
            metal = str(row.get("spdisplays_mtlgpufamilysupport") or "").strip()
            if metal:
                info["metal"] = True
        info["gpu_names"] = gpu_names
    if not info.get("machine_name"):
        info["machine_name"] = _sysctl_value("hw.model") or "Mac"
    if not info.get("chip_type"):
        info["chip_type"] = _sysctl_value("machdep.cpu.brand_string")
    if not info.get("gpu_names") and info.get("chip_type"):
        info["gpu_names"] = [str(info["chip_type"])]
    return info


def _sysctl_value(name: str) -> str:
    proc = _run(["sysctl", "-n", name], timeout=3)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _ffmpeg_encoders(ffmpeg_bin: str) -> str:
    proc = _run([ffmpeg_bin, "-hide_banner", "-encoders"], timeout=8)
    if proc.returncode != 0:
        return ""
    return proc.stdout.lower() + "\n" + proc.stderr.lower()


def _smoke_test_encoder(ffmpeg_bin: str, encoder: str) -> bool:
    args: list[str]
    if encoder == "h264_nvenc":
        args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_qsv":
        args = ["-vf", "format=nv12", "-c:v", "h264_qsv", "-global_quality", "26", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_amf":
        args = ["-c:v", "h264_amf", "-quality", "speed", "-qp_i", "26", "-qp_p", "26", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_videotoolbox":
        args = ["-c:v", "h264_videotoolbox", "-q:v", "45", "-pix_fmt", "yuv420p"]
    else:
        return False

    with tempfile.TemporaryDirectory(prefix="novel_autotune_") as temp_dir:
        out_path = Path(temp_dir) / "probe.mp4"
        proc = _run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=640x360:r=10:d=0.6",
                "-frames:v",
                "6",
                *args,
                str(out_path),
            ],
            timeout=20,
        )
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            **kwargs,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _save_config_and_profile() -> None:
    active = str(config.get("active_profile", "配置1") or "配置1")
    try:
        config.save_profile(active)
    except Exception:
        pass
    config.save()


def _write_report(result: dict[str, Any]) -> None:
    try:
        AUTOTUNE_REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
