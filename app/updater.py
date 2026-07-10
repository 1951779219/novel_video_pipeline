"""Online update client for the novel video pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .version import VERSION


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str
    url: str
    sha256: str = ""
    notes: str = ""
    mandatory: bool = False


def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def manifest_url(config: Any | None = None) -> str:
    configured = getattr(config, "update_manifest_url", "") if config is not None else ""
    return (os.getenv("NOVEL_VIDEO_UPDATE_MANIFEST_URL") or configured or "").strip()


def _version_key(version: str) -> tuple[int, ...]:
    import re

    numbers = [int(x) for x in re.findall(r"\d+", str(version or ""))]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def is_newer(remote_version: str, current_version: str = VERSION) -> bool:
    return _version_key(remote_version) > _version_key(current_version)


def fetch_manifest(config: Any | None = None) -> dict[str, Any]:
    url = manifest_url(config)
    if not url:
        raise UpdateError("未配置更新清单 URL")
    if url.lower().startswith(("http://", "https://")):
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}_ts={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": f"novel-video-pipeline/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = getattr(resp, "status", None) or 200
            if status >= 400:
                raise UpdateError(f"更新清单请求失败: HTTP {status}")
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法获取更新清单: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise UpdateError("更新清单不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("更新清单格式错误")
    return data


def check_for_update(config: Any | None = None, current_version: str = VERSION) -> UpdateInfo | None:
    data = fetch_manifest(config)
    info = UpdateInfo(
        version=str(data.get("version", "")).strip().lstrip("v"),
        url=str(data.get("url", "")).strip(),
        sha256=str(data.get("sha256", "")).strip().lower(),
        notes=str(data.get("notes", "")).strip(),
        mandatory=bool(data.get("mandatory", False)),
    )
    if not info.version or not info.url:
        raise UpdateError("更新清单缺少 version 或 url")
    return info if is_newer(info.version, current_version) else None


def download_update(info: UpdateInfo, progress: Callable[[int, int], None] | None = None) -> Path:
    target_dir = app_root() / "data" / "updates"
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"novel_video_pipeline_update_{info.version}.zip"
    tmp = out.with_suffix(".download")
    h = hashlib.sha256()

    req = urllib.request.Request(info.url, headers={"User-Agent": f"novel-video-pipeline/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as handle:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            done = 0
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                handle.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载更新包失败: {exc}") from exc

    digest = h.hexdigest().lower()
    if info.sha256 and digest != info.sha256:
        tmp.unlink(missing_ok=True)
        raise UpdateError("更新包 SHA256 校验失败，已取消")
    tmp.replace(out)
    return out


def extract_update(zip_path: Path, version: str) -> Path:
    extract_dir = app_root() / "data" / "updates" / f"extracted_{version}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extract(zf, extract_dir)
    except zipfile.BadZipFile as exc:
        raise UpdateError("更新包不是有效 zip 文件") from exc
    return extract_dir


def _safe_extract(zf: zipfile.ZipFile, target: Path) -> None:
    root = target.resolve()
    for member in zf.infolist():
        dest = (target / member.filename).resolve()
        if root != dest and root not in dest.parents:
            raise UpdateError(f"更新包包含非法路径: {member.filename}")
    zf.extractall(target)


def launch_update(zip_path: Path, version: str, target_dir: Path | None = None) -> None:
    target = target_dir or app_root()
    script = target / "scripts" / "apply_update.py"
    if script.exists():
        args = [
            sys.executable,
            str(script),
            "--zip",
            str(zip_path),
            "--target",
            str(target),
            "--version",
            str(version),
            "--wait",
            "2",
        ]
        kwargs: dict[str, Any] = {"cwd": str(target)}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)
        return

    extract_dir = extract_update(zip_path, version)
    bat = extract_dir / "update.bat"
    if not bat.exists():
        raise UpdateError("更新包内缺少 update.bat，且当前安装包没有 scripts/apply_update.py")
    if os.name != "nt":
        raise UpdateError("当前安装包只提供 Windows update.bat；请先安装带 macOS 更新器的新版本")
    subprocess.Popen(
        ["cmd", "/c", str(bat), str(target)],
        cwd=str(extract_dir),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Check, download, or launch online updates.")
    parser.add_argument("--check", action="store_true", help="check whether a newer version exists")
    parser.add_argument("--download", action="store_true", help="download the update package if available")
    parser.add_argument("--apply", action="store_true", help="download and launch update.bat if available")
    args = parser.parse_args()

    from .config import config

    try:
        info = check_for_update(config)
    except UpdateError as exc:
        print(f"检查更新失败: {exc}")
        return 1
    if not info:
        print(f"已是最新版本: v{VERSION}")
        return 0
    print(f"发现新版本: v{info.version}")
    if info.notes:
        print(info.notes)
    if args.download or args.apply:
        zip_path = download_update(info)
        print(f"已下载: {zip_path}")
        if args.apply:
            launch_update(zip_path, info.version)
            print("已启动更新程序，请关闭当前软件窗口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
