"""Tkinter update tab."""
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .version import VERSION


class UpdateTab:
    def __init__(self, parent, config):
        self.config = config
        self.frame = ttk.Frame(parent, padding=8)
        self._info = None
        self._zip_path: Path | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        box = ttk.LabelFrame(self.frame, text="联网更新", padding=10)
        box.pack(fill=tk.X)

        ttk.Label(box, text=f"当前版本: v{VERSION}", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor=tk.W)

        try:
            from .updater import manifest_url

            url = manifest_url(self.config)
        except Exception:
            url = str(getattr(self.config, "update_manifest_url", "") or "")

        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(row, text="更新清单").pack(side=tk.LEFT)
        self._manifest_var = tk.StringVar(value=url or "未配置")
        ttk.Entry(row, textvariable=self._manifest_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        btns = ttk.Frame(box)
        btns.pack(fill=tk.X, pady=(10, 0))
        self._check_btn = ttk.Button(btns, text="检查更新", command=self.check_update)
        self._check_btn.pack(side=tk.LEFT)
        self._download_btn = ttk.Button(btns, text="下载并应用更新", command=self._download_update, state=tk.DISABLED)
        self._download_btn.pack(side=tk.LEFT, padx=6)

        self._progress = ttk.Progressbar(box, maximum=100, mode="determinate")
        self._progress.pack(fill=tk.X, pady=(10, 0))
        self._status = ttk.Label(box, text="从 GitHub Release 读取 latest.json；应用更新时会保留 data/settings.json、profiles、jobs 和浏览器登录态。", foreground="#555")
        self._status.pack(anchor=tk.W, pady=(6, 0))

        notes = ttk.LabelFrame(self.frame, text="发布说明", padding=8)
        notes.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._notes = tk.Text(notes, height=10, wrap=tk.WORD, state=tk.DISABLED, font=("Microsoft YaHei UI", 10))
        self._notes.pack(fill=tk.BOTH, expand=True)

    def check_update(self) -> None:
        self._busy(True, "正在检查更新...")

        def worker() -> None:
            try:
                from .updater import check_for_update

                info = check_for_update(self.config)
                self.frame.after(0, lambda: self.check_done(info))
            except Exception as exc:
                self.frame.after(0, lambda: self._fail(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def check_done(self, info) -> None:
        self._info = info
        self._busy(False)
        if not info:
            self._status.config(text="已是最新版本", foreground="green")
            self._set_notes("")
            return
        self._status.config(text=f"发现新版本 v{info.version}", foreground="green")
        self._set_notes(info.notes)
        self._download_btn.config(state=tk.NORMAL)

    def _download_update(self) -> None:
        if not self._info:
            return
        self._busy(True, "正在下载更新包...")
        self._progress["value"] = 0

        def on_progress(done: int, total: int) -> None:
            if total:
                pct = max(0, min(100, int(done * 100 / total)))
                self.frame.after(0, lambda: self._progress.config(value=pct))

        def worker() -> None:
            try:
                from .updater import download_update

                zip_path = download_update(self._info, progress=on_progress)
                self.frame.after(0, lambda: self._download_done(zip_path))
            except Exception as exc:
                self.frame.after(0, lambda: self._fail(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _download_done(self, zip_path: Path) -> None:
        self._zip_path = zip_path
        self._busy(False)
        self._progress["value"] = 100
        self._status.config(text=f"已下载: {zip_path.name}", foreground="green")
        if not messagebox.askyesno("应用更新", "更新包已下载完成。现在应用更新并关闭软件？\n\n用户配置、任务和浏览器登录态会保留。"):
            return
        try:
            from .updater import launch_update

            launch_update(zip_path, self._info.version)
            self.frame.winfo_toplevel().destroy()
        except Exception as exc:
            self._fail(str(exc))

    def _busy(self, busy: bool, text: str = "") -> None:
        self._check_btn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self._download_btn.config(state=tk.DISABLED if busy or not self._info else tk.NORMAL)
        if text:
            self._status.config(text=text, foreground="#555")

    def _set_notes(self, text: str) -> None:
        self._notes.config(state=tk.NORMAL)
        self._notes.delete("1.0", tk.END)
        self._notes.insert("1.0", text or "暂无发布说明")
        self._notes.config(state=tk.DISABLED)

    def _fail(self, message: str) -> None:
        self._busy(False)
        self._status.config(text=message, foreground="red")
