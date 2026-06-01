"""
Stage 5 (浏览器上传): 用 Playwright 通过 CDP 连接已打开的 Chrome
前提：Chrome 必须以 --remote-debugging-port=9222 启动（见软件目录的快捷方式）
流程：
  1. 连接已有 Chrome（复用登录态，无需重新登录）
  2. 打开上传页面 → 填标题/封面
  精简流程（flow=simple）：直接跳公开范围页 → 自动发布
  完整流程（flow=full）：设置权利管理 → 创收/广告位 → 分级 → 发布
"""
import re
import json
import time
import socket
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

_DEBUG_PORT = 9222


def _is_browser_disconnect_error(exc_or_text) -> bool:
    text = str(exc_or_text or "").lower()
    needles = (
        "target page, context or browser has been closed",
        "connection closed",
        "websocket",
        "targetclosederror",
        "browser has been closed",
        "browser has been disconnected",
        "connection reset",
        "protocol error",
    )
    return any(needle in text for needle in needles)


_STALL = "STALL"   # 标志：上传卡住，需要外层重启浏览器


def _activate_chrome_window():
    """将 Chrome 主窗口激活到前台，确保原生文件对话框能正常弹出。
    用 keybd_event 模拟按键解除 Windows 对 SetForegroundWindow 的限制。"""
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    chrome_hwnds = []

    def _enum(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "Chrome_WidgetWin_1":
                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title_buf, 256)
                if title_buf.value:
                    chrome_hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_enum), 0)
    if not chrome_hwnds:
        return False

    hwnd = chrome_hwnds[0]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 模拟一次 Alt 按键释放，解除 Windows 对非前台进程调用
        # SetForegroundWindow 的限制（否则只会闪烁任务栏图标）
        VK_MENU = 0x12
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# 操作速度延迟配置
# ─────────────────────────────────────────────────────────────

class _Delays:
    """根据速度档位统一管理各步骤等待时间（毫秒）"""
    def __init__(self, speed: str):
        if speed == "very_slow":
            self.click_after   = 1500
            self.step_between  = 2000
            self.type_delay    = 30
            self.page_load     = 3000
        elif speed == "slow":
            self.click_after   = 1000
            self.step_between  = 1500
            self.type_delay    = 20
            self.page_load     = 2000
        else:  # normal
            self.click_after   = 800
            self.step_between  = 1000
            self.type_delay    = 15
            self.page_load     = 1000


# ─────────────────────────────────────────────────────────────
# Chrome 重启工具
# ─────────────────────────────────────────────────────────────

def _find_chrome_exe() -> Optional[str]:
    import os
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _kill_chrome():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _launch_chrome(config, on_log: Callable) -> bool:
    """启动调试模式 Chrome，等端口就绪后再等页面基本加载完"""
    chrome = _find_chrome_exe()
    if not chrome:
        on_log("❌ 未找到 Chrome 可执行文件，无法自动重启")
        return False
    profile = str(Path(__file__).parent.parent.parent / "data" / "chrome_debug_profile")
    cmd = [
        chrome,
        f"--remote-debugging-port={_DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://studio.youtube.com",
    ]
    try:
        subprocess.Popen(cmd)
        on_log("  Chrome 已启动，等待调试端口就绪...")
        for _ in range(30):
            time.sleep(0.5)
            if _is_port_open(_DEBUG_PORT):
                on_log(f"  ✓ 调试端口 {_DEBUG_PORT} 就绪，等待 YouTube Studio 加载...")
                time.sleep(3)
                return True
        on_log(f"  ⚠️ 等待超时，端口 {_DEBUG_PORT} 仍未开放")
        return False
    except Exception as e:
        on_log(f"  ❌ 启动 Chrome 失败: {e}")
        return False


def _restart_chrome(config, on_log: Callable) -> bool:
    on_log("⚙ 重启调试浏览器...")
    _kill_chrome()
    time.sleep(2)
    return _launch_chrome(config, on_log)


# ─────────────────────────────────────────────────────────────
# 激活方案解析
# ─────────────────────────────────────────────────────────────

def _get_active_profile(config) -> dict:
    """解析激活的上传方案，找不到时 fallback 到旧字段"""
    profiles_raw = getattr(config, "browser_profiles", "[]") or "[]"
    try:
        profiles = json.loads(profiles_raw)
    except Exception:
        profiles = []

    if not profiles:
        # fallback 到旧字段（向后兼容）
        return {
            "flow": "full",
            "upload_policy": getattr(config, "browser_upload_policy", "BTRA"),
            "ad_interval": int(getattr(config, "browser_ad_interval", 60)),
            "ad_start": int(getattr(config, "browser_ad_start", 0)),
            "visibility": str(getattr(config, "browser_visibility", "PUBLIC")).strip().upper() or "PUBLIC",
            "title_template": "",
            "description": getattr(config, "youtube_description", ""),
        }

    active_name = getattr(config, "browser_active_profile", "") or ""
    for p in profiles:
        if p.get("name") == active_name:
            return p
    return profiles[0]


# ─────────────────────────────────────────────────────────────
# 公开入口
# ─────────────────────────────────────────────────────────────

def upload_via_browser(job, config, video_path: Path, title: str,
                       cover_path: Optional[Path],
                       on_log: Callable, on_progress: Callable) -> Optional[str]:
    """
    连接已打开的 Chrome（需带 --remote-debugging-port=9222），执行上传流程。
    根据激活方案的 flow 字段选择精简或完整流程。
    支持 Chrome 崩溃自动重启和上传卡住自动重启。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        on_log("❌ playwright 未安装，请运行: pip install playwright && playwright install chrome")
        return None

    profile = _get_active_profile(config)
    flow          = profile.get("flow", "simple")
    ad_interval   = int(profile.get("ad_interval", 60))
    ad_start      = int(profile.get("ad_start", 0))
    upload_policy = str(profile.get("upload_policy", "BTRA")).strip() or "BTRA"
    visibility    = str(profile.get("visibility", "PUBLIC")).strip().upper() or "PUBLIC"
    desc_override = profile.get("description", "")

    auto_restart      = getattr(config, "browser_auto_restart", True)
    stall_timeout_min = int(getattr(config, "browser_stall_timeout_min", 10))
    op_speed          = getattr(config, "browser_op_speed", "normal")
    delays            = _Delays(op_speed)

    profile_name = profile.get("name", "")
    on_log(f"上传方案: 「{profile_name}」  流程: {'精简' if flow == 'simple' else '完整'}")

    _MAX_BROWSER_RESTARTS = 3
    for _browser_attempt in range(1, _MAX_BROWSER_RESTARTS + 1):
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        # 检查调试端口是否可连
        if not _is_port_open(_DEBUG_PORT):
            if auto_restart and _browser_attempt == 1:
                on_log(f"⚠️ 未检测到 Chrome 调试端口 {_DEBUG_PORT}，尝试自动启动...")
                if not _launch_chrome(config, on_log):
                    on_log("   请手动双击「Chrome调试模式启动.bat」，登录 YouTube Studio 后重试。")
                    return None
            elif auto_restart and _browser_attempt > 1:
                on_log(f"⚠️ Chrome 端口消失（第 {_browser_attempt} 次），重启...")
                if not _restart_chrome(config, on_log):
                    on_log("❌ 无法重启 Chrome，放弃")
                    return None
            else:
                on_log(f"❌ 未检测到 Chrome 调试端口 {_DEBUG_PORT}")
                on_log("   请用调试模式启动 Chrome（双击软件目录中的「Chrome调试模式启动.bat」），")
                on_log("   打开 YouTube Studio 并登录后，再点击上传。")
                return None

        if job.is_cancelled():
            on_log("上传已取消")
            return None

        on_log(f"检测到 Chrome 调试端口，正在连接...")
        on_progress(22)

        result = _run_upload_session(
            job, config, video_path, title, cover_path,
            flow, ad_interval, ad_start, upload_policy, visibility,
            desc_override, delays, stall_timeout_min,
            on_log, on_progress,
        )

        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if result == _STALL:
            on_log(f"⚠️ 上传卡住超过 {stall_timeout_min} 分钟（第 {_browser_attempt} 次），重启 Chrome...")
            if auto_restart:
                if not _restart_chrome(config, on_log):
                    on_log("❌ 无法重启 Chrome，放弃")
                    return None
                on_log("  等待 YouTube Studio 加载完成...")
                time.sleep(10)
                continue
            else:
                on_log("❌ 上传超时，已禁用自动重启")
                return None
        elif result == "CHROME_CRASH":
            on_log(f"⚠️ Chrome 连接断开（第 {_browser_attempt} 次），重启...")
            if auto_restart:
                if not _restart_chrome(config, on_log):
                    on_log("❌ 无法重启 Chrome，放弃")
                    return None
                time.sleep(10)
                continue
            else:
                on_log("❌ Chrome 崩溃，已禁用自动重启")
                return None
        elif result:
            return result
        else:
            if _browser_attempt < _MAX_BROWSER_RESTARTS:
                on_log(f"  上传失败，等待重试（第 {_browser_attempt} 次）...")
                time.sleep(5)
            continue

    on_log(f"❌ 重启 {_MAX_BROWSER_RESTARTS} 次后仍失败")
    return None


def _run_upload_session(job, config, video_path, title, cover_path,
                        flow, ad_interval, ad_start, upload_policy, visibility,
                        desc_override, delays, stall_timeout_min,
                        on_log, on_progress):
    """
    单次 Playwright 会话：连接 Chrome，尝试上传，返回 video_id / None / _STALL / 'CHROME_CRASH'
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as pw:
        on_log(f"连接到 Chrome (localhost:{_DEBUG_PORT})...")
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{_DEBUG_PORT}")
        except Exception as e:
            on_log(f"❌ 连接 Chrome 失败: {e}")
            return "CHROME_CRASH"

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        try:
            page = None
            for p in ctx.pages:
                if "studio.youtube.com" in p.url:
                    page = p
                    break
            if page is None:
                page = ctx.new_page()

            _MAX_UPLOAD_RETRY = 3
            for _attempt in range(1, _MAX_UPLOAD_RETRY + 1):
                if job.is_cancelled():
                    on_log("上传已取消")
                    return None

                if _attempt > 1:
                    on_log(f"⚠ 上传流程第 {_attempt} 次重试，导航回 Studio 主页重新开始...")
                    _close_stale_file_dialogs()
                    try:
                        channel_m = re.search(r"studio\.youtube\.com/channel/([^/?]+)", page.url)
                        if channel_m:
                            page.goto(f"https://studio.youtube.com/channel/{channel_m.group(1)}", timeout=60_000)
                        else:
                            page.goto("https://studio.youtube.com", timeout=60_000)
                        page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    except Exception as nav_e:
                        if _is_browser_disconnect_error(nav_e):
                            on_log(f"  ⚠️ Chrome 连接已断开，准备重启后重试: {str(nav_e)[:80]}")
                            return "CHROME_CRASH"
                        on_log(f"  导航失败: {nav_e}，继续尝试...")

                ban_check = getattr(job, "mode", "full") == "ban_check"
                try:
                    video_id = _do_upload(
                        page, job, config, video_path, title, cover_path,
                        ad_interval, ad_start, upload_policy, visibility, on_log, on_progress,
                        ban_check=ban_check, flow=flow, desc_override=desc_override,
                        delays=delays, stall_timeout_min=stall_timeout_min,
                    )
                except Exception as e:
                    err = str(e)
                    if _is_browser_disconnect_error(err):
                        on_log(f"  ⚠️ Chrome 连接断开: {err[:80]}")
                        return "CHROME_CRASH"
                    on_log(f"  上传异常: {e}")
                    video_id = None

                if video_id == _STALL:
                    return _STALL
                if video_id:
                    return video_id
                if job.is_cancelled():
                    on_log("上传已取消")
                    return None
                if _attempt < _MAX_UPLOAD_RETRY:
                    on_log(f"  上传失败，稍后重试...")
                    time.sleep(3)

            on_log("❌ 上传重试 3 次均失败")
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# 上传主流程
# ─────────────────────────────────────────────────────────────

def _do_upload(page, job, config, video_path: Path, title: str,
               cover_path: Optional[Path],
               ad_interval: int, ad_start: int, upload_policy: str,
               visibility: str,
               on_log: Callable, on_progress: Callable,
               ban_check: bool = False,
               flow: str = "simple",
               desc_override: str = "",
               delays: "_Delays" = None,
               stall_timeout_min: int = 10) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PWTimeout

    if delays is None:
        delays = _Delays("normal")

    TIMEOUT = 60_000
    UPLOAD_TIMEOUT = 1_800_000  # 30 分钟
    LOGIN_WAIT = 300_000        # 5 分钟等待登录

    # 描述：优先用方案覆盖值，否则用 config 默认
    desc = desc_override if desc_override else getattr(config, "youtube_description", "")

    # ── 步骤1：确认 Studio 已就绪 ─────────────────────────
    on_log("打开 YouTube Studio...")
    if not re.search(r"studio\.youtube\.com/channel/[^/]+/?$", page.url):
        channel_m = re.search(r"studio\.youtube\.com/channel/([^/?]+)", page.url)
        if channel_m:
            clean_url = f"https://studio.youtube.com/channel/{channel_m.group(1)}"
        else:
            clean_url = "https://studio.youtube.com"
        page.goto(clean_url, timeout=TIMEOUT)
        try:
            page.wait_for_url(re.compile(r"studio\.youtube\.com/channel/"), timeout=LOGIN_WAIT)
        except PWTimeout:
            on_log(f"❌ 未能进入 Studio 主页，当前: {page.url}")
            return None
        cur = page.url
        if "accounts.google.com" in cur or "youtube.com/signin" in cur:
            on_log("检测到登录页面，请在浏览器中完成登录（最多 5 分钟）...")
            try:
                page.wait_for_url(re.compile(r"studio\.youtube\.com/channel/"), timeout=LOGIN_WAIT)
            except PWTimeout:
                on_log("❌ 登录超时")
                return None

    ctx = page.context
    for p in ctx.pages:
        if re.search(r"studio\.youtube\.com/channel/", p.url) and p != page:
            on_log("  切换到 Studio 活跃 tab")
            page = p
            break

    page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT)
    on_log(f"✓ Studio 已就绪: {page.url}")

    # ── 步骤2：点击"创建"→"上传视频" ─────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log("点击创建按钮...")
    try:
        page.click('button[aria-label="创建"]', timeout=TIMEOUT)
    except PWTimeout:
        try:
            page.click('button[aria-label="Create"]', timeout=10_000)
        except PWTimeout:
            on_log("❌ 未找到创建按钮")
            return None
    page.wait_for_timeout(delays.click_after)

    try:
        page.click('tp-yt-paper-item:has-text("上传视频")', timeout=8_000)
    except PWTimeout:
        try:
            page.click('tp-yt-paper-item:has-text("Upload videos")', timeout=8_000)
        except PWTimeout:
            on_log("❌ 未找到上传视频菜单项")
            return None
    page.wait_for_timeout(delays.step_between)
    on_progress(25)

    # ── 步骤3：选择文件 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log(f"选择视频文件: {video_path.name}")
    if not _select_file_via_dialog(page, video_path, on_log, TIMEOUT):
        on_log("❌ 文件对话框未识别，触发整体重试...")
        return None
    on_progress(28)

    # ── 步骤4：填写标题 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if not ban_check:
        on_log("填写标题...")
        title_inner = page.locator(
            'ytcp-video-metadata-editor #title-textarea #textbox, '
            'ytcp-video-metadata-editor ytcp-social-suggestions-textbox #textbox'
        ).first
        title_inner.wait_for(timeout=10_000)
        title_inner.click()
        page.evaluate(
            "el => { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }",
            title_inner.element_handle()
        )
        title_inner.type(title, delay=delays.type_delay)
        page.wait_for_timeout(300)

        if desc:
            desc_box = page.locator(
                'ytcp-video-metadata-editor #description-container #textbox, '
                'ytcp-video-metadata-editor #description-textarea #textbox'
            ).first
            try:
                desc_box.wait_for(timeout=5_000)
                desc_box.click()
                page.evaluate(
                    "el => { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }",
                    desc_box.element_handle()
                )
                desc_box.type(desc, delay=5)
            except PWTimeout:
                pass

    on_progress(32)

    # ── 步骤5：上传封面 ────────────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if not ban_check and cover_path and cover_path.exists():
        on_log("上传封面...")
        _upload_cover(page, cover_path, on_log)

    on_progress(35)

    # ── 步骤6-10：根据 flow 和 ban_check 决定中间步骤 ─────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    if ban_check:
        on_log("[查禁播] 跳过标题/封面/版权/广告/分级，直接跳到公开范围...")
        on_progress(50)
        _next_clicks = 5
    elif flow == "simple":
        on_log("精简流程：跳过创收/广告位/分级，直接跳到公开范围...")
        on_progress(50)
        _next_clicks = 5
    else:
        on_log("继续 → 权利管理...")
        _click_next(page, TIMEOUT)
        page.wait_for_timeout(delays.step_between)
        on_progress(38)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log(f"设置上传政策（{upload_policy}）...")
        _set_upload_policy(page, upload_policy, on_log, TIMEOUT)
        on_progress(45)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("继续 → 创收页...")
        _click_next(page, TIMEOUT)
        page.wait_for_timeout(delays.step_between + 200)
        on_progress(50)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("插入中贴片广告位...")
        _insert_ad_breaks(page, ad_interval, ad_start, on_log, TIMEOUT, job=job)
        on_progress(60)

        if job.is_cancelled():
            on_log("上传已取消")
            return None
        on_log("提交广告适合性分级...")
        on_log(f"  当前 URL: {page.url}")
        page.wait_for_timeout(delays.step_between + 500)
        _submit_ad_suitability(page, on_log, TIMEOUT)
        page.wait_for_timeout(delays.click_after)
        _next_clicks = 3

    # ── 步骤11：跳到公开范围页 ────────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log("继续 → 公开范围...")
    for _ in range(_next_clicks):
        try:
            _click_next(page, TIMEOUT)
            page.wait_for_timeout(delays.click_after)
        except Exception:
            break

    on_progress(70)

    # ── 步骤12：等待上传完成并发布 ────────────────────────
    if job.is_cancelled():
        on_log("上传已取消")
        return None
    on_log("等待视频上传完成并发布...")
    video_id = _wait_for_upload_and_publish(
        page, job, visibility, on_log, on_progress, UPLOAD_TIMEOUT,
        stall_timeout_min=stall_timeout_min,
    )

    if video_id is None:
        on_log("❌ 上传失败或被取消")
        return None

    if video_id == _STALL:
        return _STALL

    on_log(f"✓ 视频已发布，video_id: {video_id}")
    on_progress(100)

    return video_id


# ─────────────────────────────────────────────────────────────
# 封面上传
# ─────────────────────────────────────────────────────────────

def _upload_cover(page, cover_path: Path, on_log: Callable):
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        # 等封面上传区域可见，找其中 visible 的按钮
        page.wait_for_selector('ytcp-thumbnail-uploader', timeout=10_000)
        with page.expect_file_chooser(timeout=10_000) as fc:
            # 用 JS 点击第一个可见的 select-button，避免 Playwright 误选隐藏元素
            page.evaluate('''() => {
                const btns = document.querySelectorAll("ytcp-thumbnail-editor button#select-button");
                for (const b of btns) {
                    if (b.offsetParent !== null) { b.click(); return; }
                }
                // fallback: 点第一个
                if (btns.length) btns[0].click();
            }''')
        fc.value.set_files(str(cover_path))
        page.wait_for_timeout(2000)
        on_log("  ✓ 封面已上传")
    except Exception as e:
        on_log(f"  ⚠️ 封面上传失败（继续）: {e}")


# ─────────────────────────────────────────────────────────────
# 权利管理：选择 BTRA 上传政策
# ─────────────────────────────────────────────────────────────

def _set_upload_policy(page, policy_name: str, on_log: Callable, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout

    # 等待权利管理页面出现（特征：ytcp-form-select#metadata-asset-select）
    try:
        page.wait_for_selector('ytcp-form-select#metadata-asset-select', timeout=timeout)
    except PWTimeout:
        on_log("  ⚠️ 未找到权利管理页，跳过政策设置")
        return

    # 点开"上传政策"下拉（专属 trigger id: policy-select-trigger）
    try:
        page.click('ytcp-text-dropdown-trigger#policy-select-trigger', timeout=10_000)
        page.wait_for_timeout(800)
    except PWTimeout:
        on_log("  ⚠️ 无法点击上传政策下拉")
        return

    # 等待选项列表渲染，选项是 tp-yt-paper-item
    try:
        page.wait_for_selector('#policy-content-container tp-yt-paper-item', timeout=5_000)
        page.click(f'#policy-content-container tp-yt-paper-item:has-text("{policy_name}")', timeout=8_000)
        page.wait_for_timeout(400)
        # 点击"应用"按钮确认
        page.click('ytcp-button#apply-policy', timeout=5_000)
        page.wait_for_timeout(500)
        on_log(f"  ✓ 已选择上传政策: {policy_name}")
    except PWTimeout:
        on_log(f"  ⚠️ 未找到政策「{policy_name}」，请手动选择")


# ─────────────────────────────────────────────────────────────
# 创收：插入中贴片广告位
# ─────────────────────────────────────────────────────────────

def _insert_ad_breaks(page, interval: int, start: int, on_log: Callable, timeout: int, job=None):
    from playwright.sync_api import TimeoutError as PWTimeout

    # 等待创收页出现
    try:
        page.wait_for_selector('ytcp-uploads-monetization', timeout=timeout)
    except PWTimeout:
        on_log("  ⚠️ 未找到创收页，跳过广告位设置")
        return

    # 点击"管理广告位"按钮进入广告位编辑页面
    try:
        page.click('ytcp-button#place-manually-button', timeout=10_000)
        page.wait_for_timeout(1000)
        on_log(f"  进入广告位页面，开始自动插入（间隔={interval}s，起始={start}s）...")
    except PWTimeout:
        on_log("  ⚠️ 未找到管理广告位按钮，跳过广告位插入")
        return

    # ── 读取视频真实时长 ────────────────────────────────────
    # 广告位编辑页包含隐藏的 <video> 元素，duration 属性直接给出精确秒数
    # 最多等 8 秒让视频元数据加载完
    duration = None
    for _ in range(16):
        duration = page.evaluate("""() => {
            const v = document.querySelector('video');
            if (!v) return null;
            const d = v.duration;
            return (d && isFinite(d) && d > 10) ? Math.floor(d) : null;
        }""")
        if duration:
            break
        page.wait_for_timeout(500)

    if duration:
        on_log(f"  视频时长: {duration}s ({duration//3600}:{(duration%3600)//60:02d}:{duration%60:02d})")
        # 最后一个广告位至少距结尾 60 秒
        stop_at = duration - 60
    else:
        on_log("  ⚠️ 无法读取视频时长，将靠连续卡住检测停止")
        stop_at = None

    # ── 注入广告位插入脚本 ──────────────────────────────────
    # 注意：JS 模板里含 % 运算符，不能用 % 格式化，用字符串拼接传参
    _stop_at_js = str(stop_at) if stop_at is not None else 'null'
    ad_script = (
        "(function(intervalSeconds, startTimeInput, stopAt) {\n"
        "    let currentStartTime = startTimeInput;\n"
        "    let sameTimeCount = 0;\n"
        "    let lastDetectedTime = null;\n"
        "    let insertCount = 0;\n"
        "\n"
        "    function formatTime(s) {\n"
        "        const h = Math.floor(s / 3600);\n"
        "        const m = Math.floor((s % 3600) / 60);\n"
        "        const ss = s % 60;\n"
        "        return h + ':' + String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0') + ':00';\n"
        "    }\n"
        "\n"
        "    function findTimeInput() {\n"
        "        const sels = [\n"
        "            'input[type=\"text\"][class*=\"ytcp-media-timestamp-input\"]',\n"
        "            'input.ytcp-media-timestamp-input',\n"
        "            'input[maxlength=\"10\"][type=\"text\"]',\n"
        "        ];\n"
        "        for (const sel of sels) {\n"
        "            const inputs = document.querySelectorAll(sel);\n"
        "            for (let i = inputs.length - 1; i >= 0; i--) {\n"
        "                if (inputs[i].offsetWidth > 0) return inputs[i];\n"
        "            }\n"
        "        }\n"
        "        return null;\n"
        "    }\n"
        "\n"
        "    function getTimeFromInput(input) {\n"
        "        try {\n"
        "            const parts = (input.value || '').trim().split(':');\n"
        "            if (parts.length !== 4) return null;\n"
        "            return +parts[0]*3600 + +parts[1]*60 + +parts[2];\n"
        "        } catch(e) { return null; }\n"
        "    }\n"
        "\n"
        "    function setInputValue(input, value) {\n"
        "        input.focus(); input.select();\n"
        "        document.execCommand('insertText', false, value);\n"
        "        ['input','change'].forEach(t => input.dispatchEvent(new Event(t, {bubbles:true})));\n"
        "    }\n"
        "\n"
        "    function clickInsertButton() {\n"
        "        for (const btn of document.querySelectorAll('ytcp-button#place-manually-button, button')) {\n"
        "            const t = btn.textContent || btn.getAttribute('aria-label') || '';\n"
        "            if (t.includes('\\u63d2\\u5165\\u5e7f\\u544a\\u4f4d') || t.includes('Place ad break')) {\n"
        "                btn.click(); return true;\n"
        "            }\n"
        "        }\n"
        "        return false;\n"
        "    }\n"
        "\n"
        "    function processInsert() {\n"
        "        if (!document.body.innerText.includes('\\u4e2d\\u8d34\\u7247\\u5e7f\\u544a\\u4f4d')) {\n"
        "            window.__adHelperDone = true; return;\n"
        "        }\n"
        "        if (stopAt !== null && currentStartTime > stopAt) {\n"
        "            window.__adHelperDone = true; return;\n"
        "        }\n"
        "        if (sameTimeCount >= 5) { window.__adHelperDone = true; return; }\n"
        "\n"
        "        const input = findTimeInput();\n"
        "        if (!input) { setTimeout(processInsert, 200); return; }\n"
        "\n"
        "        const cur = getTimeFromInput(input);\n"
        "        if (cur !== null) {\n"
        "            if (lastDetectedTime === cur) sameTimeCount++;\n"
        "            else { sameTimeCount = 0; lastDetectedTime = cur; }\n"
        "            if (cur === currentStartTime) {\n"
        "                currentStartTime += intervalSeconds;\n"
        "                setTimeout(processInsert, 50);\n"
        "                return;\n"
        "            }\n"
        "        }\n"
        "\n"
        "        setInputValue(input, formatTime(currentStartTime));\n"
        "        setTimeout(() => {\n"
        "            if (getTimeFromInput(input) === currentStartTime) {\n"
        "                clickInsertButton();\n"
        "                insertCount++;\n"
        "                currentStartTime += intervalSeconds;\n"
        "                setTimeout(processInsert, 80);\n"
        "            } else {\n"
        "                setTimeout(processInsert, 150);\n"
        "            }\n"
        "        }, 40);\n"
        "    }\n"
        "\n"
        "    window.__adHelperDone = false;\n"
        "    window.__adInsertCount = 0;\n"
        "    setTimeout(processInsert, 300);\n"
        "    const _ticker = setInterval(() => {\n"
        "        window.__adInsertCount = insertCount;\n"
        "        if (window.__adHelperDone) clearInterval(_ticker);\n"
        "    }, 200);\n"
        f"}})({interval}, {start}, {_stop_at_js});\n"
    )

    page.evaluate(ad_script)
    on_log("  广告位脚本已注入，等待插入完成...")

    deadline = time.time() + 300
    last_count = 0
    while time.time() < deadline:
        if job is not None and job.is_cancelled():
            on_log("上传已取消（广告插入阶段）")
            return
        if page.evaluate("() => !!window.__adHelperDone"):
            break
        count = page.evaluate("() => window.__adInsertCount || 0")
        if count != last_count:
            on_log(f"  已插入 {count} 个广告位...")
            last_count = count
        page.wait_for_timeout(1500)

    final_count = page.evaluate("() => window.__adInsertCount || 0")
    on_log(f"  ✓ 广告位插入完成，共插入 {final_count} 个，返回创收页...")

    # 点广告位页的"继续"（#save-button）返回创收界面
    try:
        save_btn = page.locator('ytcp-button#save-button')
        save_btn.wait_for(state="visible", timeout=10_000)
        save_btn.click(timeout=10_000)
        page.wait_for_timeout(1500)
    except PWTimeout:
        on_log("  ⚠️ 未找到广告位页继续按钮，请手动点击")
        return

    # 等创收界面的"继续"（#next-button）变为 enabled 后点击
    on_log("  继续 → 是否适合投放广告...")
    try:
        next_btn = page.locator('ytcp-button#next-button')
        next_btn.wait_for(state="visible", timeout=10_000)
        for _ in range(20):
            if next_btn.is_enabled():
                break
            page.wait_for_timeout(300)
        next_btn.click(timeout=10_000)
        page.wait_for_timeout(1500)
        on_log(f"  点击继续后 URL: {page.url}")
    except PWTimeout:
        on_log("  ⚠️ 未找到创收页继续按钮，请手动点击")


def _submit_ad_suitability(page, on_log: Callable, timeout: int):
    """
    是否适合投放广告页面：全程用 JS evaluate 操作（元素在深层 Shadow DOM 内）
    1. 轮询等待分级页面出现
    2. 展开第一个问题（toggle-button 的内部 button）
    3. 点第一个 radio
    4. 点 #submit-questionnaire-button 提交
    """
    JS_HELPERS = """
        function findInShadow(root, sel) {
            const el = root.querySelector(sel);
            if (el) return el;
            for (const c of root.querySelectorAll('*')) {
                if (c.shadowRoot) { const f = findInShadow(c.shadowRoot, sel); if (f) return f; }
            }
            return null;
        }
        function findAllInShadow(root, sel, out) {
            for (const el of root.querySelectorAll(sel)) out.push(el);
            for (const c of root.querySelectorAll('*')) {
                if (c.shadowRoot) findAllInShadow(c.shadowRoot, sel, out);
            }
        }
    """

    # 轮询等待分级页面（JS 穿透 Shadow DOM）
    on_log("  等待分级页面...")
    deadline = time.time() + 30
    found = False
    while time.time() < deadline:
        found = page.evaluate(f"() => {{ {JS_HELPERS} return !!findInShadow(document, 'ytpp-self-certification-question-answer-details'); }}")
        if found:
            break
        page.wait_for_timeout(500)

    if not found:
        on_log(f"  ⚠️ 未找到是否适合投放广告页面，跳过")
        return

    on_log("  分级页面已就绪")
    page.wait_for_timeout(500)

    # 展开第一个问题卡片
    # 结构（全 light DOM，无 shadowRoot）：
    #   ytpp-self-certification-questionnaire .questions
    #     > ytpp-self-certification-question
    #       > ytcp-expansion-panel #container > ytcp-ve > div#button-area > button  ← 展开按钮
    #       > ytcp-expansion-panel #container > div#content-area
    #         > tp-yt-paper-radio-group > tp-yt-paper-radio-button  ← radio 选项
    result = page.evaluate("""() => {
        const q0 = document.querySelector(
            'ytpp-self-certification-questionnaire .questions ytpp-self-certification-question'
        );
        if (!q0) return 'no-question';
        const btn = q0.querySelector('ytcp-expansion-panel #button-area button');
        if (!btn) return 'no-btn';
        btn.click();
        return 'clicked:' + btn.textContent.trim().slice(0, 20);
    }""")
    on_log(f"  展开结果: {result}")
    page.wait_for_timeout(1200)  # 等展开动画完成

    # 点第一个 radio（tp-yt-paper-radio-button，直接在 light DOM）
    clicked = page.evaluate("""() => {
        const q0 = document.querySelector(
            'ytpp-self-certification-questionnaire .questions ytpp-self-certification-question'
        );
        if (!q0) return 'no-question';
        const rg = q0.querySelector('tp-yt-paper-radio-group');
        if (!rg) return 'no-radio-group';
        const radio = rg.querySelector('tp-yt-paper-radio-button');
        if (!radio) return 'no-radio';
        radio.click();
        return 'clicked:' + (radio.id || radio.getAttribute('name') || '?');
    }""")
    on_log(f"  radio 点击结果: {clicked}")
    page.wait_for_timeout(600)

    # 点提交按钮
    submitted = page.evaluate(f"""() => {{
        {JS_HELPERS}
        const btn = findInShadow(document, 'ytcp-button#submit-questionnaire-button');
        if (!btn) return false;
        const inner = btn.shadowRoot ? btn.shadowRoot.querySelector('button') : btn.querySelector('button');
        if (inner) {{ inner.click(); return 'inner'; }}
        btn.click(); return 'outer';
    }}""")
    on_log(f"  提交结果: {submitted}")
    if submitted:
        page.wait_for_timeout(500)
        on_log("  ✓ 已提交分级结果")
    else:
        on_log("  ⚠️ 点击提交分级结果失败，请手动操作")



# ─────────────────────────────────────────────────────────────
# 等待上传完成并发布
# ─────────────────────────────────────────────────────────────

def _wait_for_upload_and_publish(page, job, visibility: str, on_log: Callable,
                                  on_progress: Callable,
                                  upload_timeout: int,
                                  stall_timeout_min: int = 10) -> Optional[str]:
    from playwright.sync_api import TimeoutError as PWTimeout

    start = time.time()
    on_log("等待上传进度...")

    last_pct = 60
    last_log_pct = -1
    last_activity = time.time()   # 最近一次有变化的时间
    stall_sec = stall_timeout_min * 60 if stall_timeout_min > 0 else 0

    while True:
        if job.is_cancelled():
            on_log("上传已取消")
            return None

        if (time.time() - start) > upload_timeout / 1000:
            on_log("❌ 上传超时")
            return None

        # 卡住检测：stall_timeout_min 分钟无任何进度变化
        if stall_sec > 0 and (time.time() - last_activity) > stall_sec:
            on_log(f"⚠️ 上传已 {stall_timeout_min} 分钟无进度，判定为卡住")
            return _STALL

        pct_result = page.evaluate("""() => {
            function searchInShadow(root) {
                const text = root.innerText || root.textContent || '';
                const m = text.match(/(\\d+)\\s*%/);
                if (m) { const n = parseInt(m[1]); if (n >= 0 && n <= 100) return n; }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) { const r = searchInShadow(el.shadowRoot); if (r !== null) return r; }
                }
                return null;
            }
            const comp = document.querySelector('ytcp-video-upload-progress');
            if (comp) { const r = searchInShadow(comp.shadowRoot || comp); if (r !== null) return r; }
            return null;
        }""")

        if pct_result is not None:
            mapped = 60 + int(pct_result * 0.22)
            if pct_result != last_log_pct:
                on_log(f"上传进度: {pct_result}%")
                last_log_pct = pct_result
                last_activity = time.time()
            on_progress(mapped)
            last_pct = mapped
            page.wait_for_timeout(2000)
            continue

        processing_result = page.evaluate("""() => {
            const t = document.body.innerText || '';
            const isProcessing = t.includes('正在进行高清处理') || t.includes('Processing') || t.includes('处理中');
            const isChecking   = t.includes('正在检查') || t.includes('仍在检查') || t.includes('Checking');
            return isProcessing || isChecking ? 'processing' : null;
        }""")
        if processing_result:
            last_activity = time.time()  # 处理中=有活动
            if last_pct < 82:
                on_log("视频处理/检查中，检查发布按钮是否可用...")
                on_progress(82)
                last_pct = 82
            try:
                publish_btn = page.locator(
                    'ytcp-button#done-button:has-text("发布"), '
                    'ytcp-button:has-text("Publish"), '
                    'ytcp-button#done-button'
                ).first
                if publish_btn.is_enabled(timeout=1000):
                    on_log("发布按钮已可用（处理中状态），直接发布...")
                    break
            except PWTimeout:
                pass
            page.wait_for_timeout(2000)
            continue

        try:
            publish_btn = page.locator(
                'ytcp-button#done-button:has-text("发布"), '
                'ytcp-button:has-text("Publish"), '
                'ytcp-button#done-button'
            ).first
            if publish_btn.is_enabled(timeout=2000):
                last_activity = time.time()
                break
        except PWTimeout:
            pass

        page.wait_for_timeout(2000)

    on_progress(83)
    on_log("上传完毕，设置公开范围...")

    # 公开范围 radio：PUBLIC / UNLISTED / PRIVATE
    _vis_label = {"PUBLIC": "公开", "UNLISTED": "不公开", "PRIVATE": "私享"}.get(visibility, visibility)
    try:
        page.wait_for_selector(
            f'tp-yt-paper-radio-button[name="{visibility}"], '
            f'ytcp-video-visibility-select tp-yt-paper-radio-button[name="{visibility}"]',
            timeout=10_000
        )
        page.click(
            f'tp-yt-paper-radio-button[name="{visibility}"], '
            f'ytcp-video-visibility-select tp-yt-paper-radio-button[name="{visibility}"]',
            timeout=8_000
        )
        page.wait_for_timeout(500)
        on_log(f"  ✓ 已选择{_vis_label}")
    except PWTimeout:
        on_log(f"  ⚠️ 未找到「{_vis_label}」选项，请手动选择")

    on_log("点击发布...")
    try:
        publish_btn = page.locator(
            'ytcp-button#done-button:has-text("发布"), '
            'ytcp-button:has-text("Publish"), '
            'ytcp-button#done-button'
        ).first
        publish_btn.click(timeout=15_000)
        page.wait_for_timeout(2000)
    except PWTimeout:
        on_log("⚠️ 未找到发布按钮")
        return None

    # 处理确认弹窗（"我们仍在检查你的视频" → 再次点击"发布"）
    try:
        confirm_btn = page.locator('ytcp-prechecks-warning-dialog ytcp-button#secondary-action-button')
        if confirm_btn.is_visible(timeout=5_000):
            on_log("检测到发布确认弹窗，点击确认发布...")
            confirm_btn.click(timeout=8_000)
            page.wait_for_timeout(1500)
            on_log("  ✓ 已确认发布")
    except PWTimeout:
        pass  # 没有弹窗，正常继续

    video_id = None
    _dialog_closed = False  # 标记弹窗是否已成功关闭（弹窗关闭=发布完成）

    # 处理"正在处理视频"弹窗：轮询等待，最多等 60 秒让弹窗出现
    # 背景：点"确认发布"后 YouTube 需要数秒才弹出此弹窗，且弹窗存在时 URL 不会跳转
    on_log("等待发布后弹窗或页面跳转...")
    _still_processing_deadline = time.time() + 60
    while time.time() < _still_processing_deadline:
        # ── 检测"视频发布时间"分享弹窗（上传很快时直接跳过检查阶段会出现此弹窗）──
        _share_info = page.evaluate("""() => {
            const d = document.querySelector('ytcp-video-share-dialog');
            if (!d || d.offsetParent === null) return null;
            // 尝试从分享链接提取 video_id
            const a = d.querySelector('a[href*="youtu.be"], a[href*="youtube.com/shorts/"], a[href*="watch?v="]');
            const inputEl = d.querySelector('input[type="text"], .text-input');
            const href = a ? a.href : (inputEl ? inputEl.value : null);
            return { href: href };
        }""")
        if _share_info:
            on_log("检测到发布成功分享弹窗，提取视频ID并关闭...")
            _href = _share_info.get("href") or ""
            if _href:
                m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})|shorts/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})", _href)
                if m:
                    video_id = m.group(1) or m.group(2) or m.group(3)
                    on_log(f"  从分享弹窗取到 video_id: {video_id}")
            # 关闭分享弹窗（点关闭按钮）
            closed = page.evaluate("""() => {
                const d = document.querySelector('ytcp-video-share-dialog');
                if (!d) return false;
                const btn = d.querySelector('button[aria-label="关闭"], button[aria-label="Close"], ytcp-button#close-button button, ytcp-button[id*="close"] button');
                if (btn) { btn.click(); return 'inner'; }
                const outer = d.querySelector('ytcp-button#close-button, ytcp-button[id*="close"]');
                if (outer) { outer.click(); return 'outer'; }
                return false;
            }""")
            on_log(f"  关闭分享弹窗: {closed}")
            page.wait_for_timeout(1500)
            on_log("  ✓ 已关闭分享弹窗，视频已发布")
            _dialog_closed = True
            break

        _dialog_visible = page.evaluate("""() => {
            const d = document.querySelector('ytcp-uploads-still-processing-dialog');
            return d && d.offsetParent !== null;
        }""")
        if _dialog_visible:
            on_log("检测到上传中弹窗，提取视频ID并关闭...")
            # 提取 video_id
            try:
                _href = page.evaluate("""() => {
                    const d = document.querySelector('ytcp-uploads-still-processing-dialog');
                    if (!d) return null;
                    const a = d.querySelector('a[href*="youtu.be"], a[href*="watch?v="]');
                    return a ? a.href : null;
                }""")
                if _href:
                    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})|v=([A-Za-z0-9_-]{11})", _href)
                    if m:
                        video_id = m.group(1) or m.group(2)
                        on_log(f"  从弹窗取到 video_id: {video_id}")
            except Exception:
                pass
            # 穿透 Shadow DOM 点关闭按钮
            closed = page.evaluate("""() => {
                const dialog = document.querySelector('ytcp-uploads-still-processing-dialog');
                if (!dialog) return false;
                const inner = dialog.querySelector('button[aria-label="关闭"], button[aria-label="Close"], ytcp-button#close-button button');
                if (inner) { inner.click(); return 'inner'; }
                const btn = dialog.querySelector('ytcp-button#close-button');
                if (btn) { btn.click(); return 'outer'; }
                return false;
            }""")
            on_log(f"  关闭弹窗点击结果: {closed}")
            page.wait_for_timeout(1500)
            on_log("  ✓ 已关闭上传中弹窗")
            _dialog_closed = True
            break  # 继续走下面的方法取 video_id
        # 检查 URL 是否已跳转（有时没有弹窗直接跳转）
        if re.search(r"studio\.youtube\.com/video/([A-Za-z0-9_-]{11})", page.url):
            break
        page.wait_for_timeout(1000)

    # 方法1：等待 URL 跳转到 /video/VIDEO_ID（弹窗已关闭则跳过此等待）
    if not video_id and not _dialog_closed:
        try:
            page.wait_for_url(
                re.compile(r"studio\.youtube\.com/video/([A-Za-z0-9_-]{11})"),
                timeout=1_800_000
            )
            m = re.search(r"/video/([A-Za-z0-9_-]{11})", page.url)
            if m:
                video_id = m.group(1)
        except PWTimeout:
            pass

    # 方法2：页面上的"查看视频"链接
    if not video_id:
        try:
            link = page.locator('a[href*="youtube.com/watch?v="]').first
            href = link.get_attribute("href", timeout=8_000)
            m = re.search(r"v=([A-Za-z0-9_-]{11})", href)
            if m:
                video_id = m.group(1)
        except PWTimeout:
            pass

    # 方法3：当前 URL
    if not video_id:
        m = re.search(r"/video/([A-Za-z0-9_-]{11})", page.url)
        if m:
            video_id = m.group(1)

    # 弹窗已关闭说明发布成功，即使取不到 video_id 也不算失败
    if not video_id and _dialog_closed:
        video_id = "published"

    return video_id


# ─────────────────────────────────────────────────────────────
# 广告位注入
# ─────────────────────────────────────────────────────────────

def _run_ad_helper(page, video_id: str, interval: int, start: int,
                   on_log: Callable, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout

    ad_url = f"https://studio.youtube.com/video/{video_id}/monetization/ads"
    on_log(f"打开广告位管理页: {ad_url}")
    page.goto(ad_url, timeout=timeout)
    page.wait_for_load_state("domcontentloaded", timeout=timeout)

    try:
        page.wait_for_selector(':text("中贴片广告位")', timeout=30_000)
    except PWTimeout:
        on_log("  ⚠️ 未找到中贴片广告位页面，请手动插入")
        return

    on_log(f"  广告位页面已就绪，开始自动插入（间隔={interval}s，起始={start}s）...")

    ad_script = r"""
(function(intervalSeconds, startTimeInput) {
    let isRunning = true;
    let currentStartTime = startTimeInput;
    let sameTimeCount = 0;
    let lastDetectedTime = null;

    function formatTime(totalSeconds) {
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = totalSeconds % 60;
        return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}:00`;
    }

    function findTimeInput() {
        const sels = [
            'input[type="text"][class*="ytcp-media-timestamp-input"]',
            'input.ytcp-media-timestamp-input',
            'input[maxlength="10"][type="text"]',
            'input[aria-label*="\u5c0f\u65f6"][aria-label*="\u5206\u949f"][aria-label*="\u79d2"]'
        ];
        for (const sel of sels) {
            const inputs = document.querySelectorAll(sel);
            for (let i = inputs.length - 1; i >= 0; i--) {
                if (inputs[i].offsetWidth > 0) return inputs[i];
            }
        }
        return null;
    }

    function getTimeFromPage(input) {
        try {
            const label = input.getAttribute('aria-label') || '';
            const m = label.match(/(\d+)\s*\u5c0f\u65f6\s*(\d+)\s*\u5206\u949f\s*(\d+)\s*\u79d2/);
            if (m) return +m[1]*3600 + +m[2]*60 + +m[3];
        } catch(e) {}
        return null;
    }

    function getTimeFromInput(input) {
        try {
            const parts = (input.value||'').trim().split(':');
            if (parts.length !== 4) return null;
            return +parts[0]*3600 + +parts[1]*60 + +parts[2];
        } catch(e) { return null; }
    }

    function setInputValue(input, value) {
        input.focus(); input.select();
        document.execCommand('insertText', false, value);
        ['input','change'].forEach(t => input.dispatchEvent(new Event(t, {bubbles:true})));
    }

    function clickInsertButton() {
        for (const btn of document.querySelectorAll('button,[role="button"],ytcp-button,div[class*="ytcpButtonShapeImpl"]')) {
            if ((btn.textContent||'').includes('\u63d2\u5165\u5e7f\u544a\u4f4d')) { btn.click(); return true; }
        }
        for (const btn of document.querySelectorAll('button,[role="button"]')) {
            const t = btn.textContent||'';
            if (t.includes('\u63d2\u5165') && !t.includes('\u4e86\u89e3\u8be6\u60c5')) { btn.click(); return true; }
        }
        return false;
    }

    function processInsert() {
        if (!isRunning) return;
        if (sameTimeCount >= 3) { window.__adHelperDone = true; return; }
        if (!document.body.innerText.includes('\u4e2d\u8d34\u7247\u5e7f\u544a\u4f4d')) {
            window.__adHelperDone = true; return;
        }
        const input = findTimeInput();
        if (!input) { setTimeout(processInsert, 200); return; }

        const pageTime = getTimeFromPage(input);
        if (pageTime !== null && (currentStartTime - intervalSeconds) > pageTime) {
            window.__adHelperDone = true; return;
        }

        const cur = getTimeFromInput(input);
        if (cur !== null) {
            if (lastDetectedTime === cur) sameTimeCount++;
            else { sameTimeCount = 0; lastDetectedTime = cur; }
            if (cur === currentStartTime) { currentStartTime += intervalSeconds; setTimeout(processInsert, 50); return; }
        }

        setInputValue(input, formatTime(currentStartTime));
        setTimeout(() => {
            if (getTimeFromInput(input) === currentStartTime) {
                clickInsertButton();
                currentStartTime += intervalSeconds;
                setTimeout(processInsert, 80);
            } else {
                setTimeout(processInsert, 150);
            }
        }, 40);
    }

    window.__adHelperDone = false;
    setTimeout(processInsert, 200);
})(%d, %d);
""" % (interval, start)

    page.evaluate(ad_script)
    on_log("  广告位脚本已注入，等待插入完成...")

    deadline = time.time() + 600
    while time.time() < deadline:
        if page.evaluate("() => !!window.__adHelperDone"):
            break
        page.wait_for_timeout(2000)

    on_log("  ✓ 广告位插入完成")

    try:
        save_btn = page.locator('ytcp-button:has-text("保存"), ytcp-button:has-text("Save")').first
        if save_btn.is_visible(timeout=5000):
            save_btn.click()
            page.wait_for_timeout(1500)
            on_log("  ✓ 广告位设置已保存")
    except Exception:
        on_log("  ⚠️ 未找到保存按钮，请手动保存")


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def _click_next(page, timeout: int):
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.locator(
            'ytcp-button#next-button:has-text("下一步"), '
            'ytcp-button#next-button:has-text("Next"), '
            'ytcp-button#next-button'
        ).first.click(timeout=timeout)
    except PWTimeout:
        page.click('ytcp-button:has-text("下一步"), ytcp-button:has-text("Next")',
                   timeout=timeout)


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _select_file_via_dialog(page, video_path: Path, on_log: Callable, timeout: int):
    """
    点击上传按钮后，系统原生文件对话框弹出，
    用 ctypes SendMessage 向对话框的文件名编辑框直接写入路径。
    connect_over_cdp 模式下不能传输 >50MB 文件，只能走原生对话框。
    若未检测到对话框返回 False，由上层决定是否重试整个上传流程。
    """
    import ctypes
    import ctypes.wintypes

    return _try_select_file_once(page, video_path, on_log, timeout)


def _close_stale_file_dialogs():
    """关闭所有残留的 #32770 文件选择对话框，防止重试时被旧对话框干扰。"""
    import ctypes
    import ctypes.wintypes
    WM_CLOSE = 0x0010
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    found = []

    def _enum(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_enum), 0)
    for hwnd in found:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _try_select_file_once(page, video_path: Path, on_log: Callable, timeout: int) -> bool:
    """尝试一次文件选择，成功返回 True"""
    import threading
    import ctypes
    import ctypes.wintypes
    import time as _time
    from playwright.sync_api import TimeoutError as PWTimeout

    file_str = str(video_path).replace("/", "\\")
    dialog_ok = threading.Event()
    dialog_found = [False]

    def _fill_dialog():
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        WM_SETTEXT = 0x000C
        BM_CLICK = 0x00F5

        for _ in range(150):
            _time.sleep(0.1)
            hwnd_found = []

            def enum_cb(hwnd, _):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "#32770" and user32.IsWindowVisible(hwnd):
                    hwnd_found.append(hwnd)
                return True

            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

            if hwnd_found:
                hwnd = hwnd_found[-1]
                _time.sleep(0.3)

                try:
                    user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                _time.sleep(0.3)

                # 主方法：WM_SETTEXT 直接写入 Edit 控件，不依赖全局键盘焦点
                edit_hwnd = None
                combo = user32.FindWindowExW(hwnd, None, "ComboBoxEx32", None)
                if combo:
                    combo2 = user32.FindWindowExW(combo, None, "ComboBox", None)
                    if combo2:
                        edit_hwnd = user32.FindWindowExW(combo2, None, "Edit", None)
                if not edit_hwnd:
                    edit_hwnd = user32.FindWindowExW(hwnd, None, "Edit", None)

                if edit_hwnd:
                    user32.SetFocus(edit_hwnd)
                    user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, file_str)
                    _time.sleep(0.2)
                    btn = user32.FindWindowExW(hwnd, None, "Button", None)
                    while btn:
                        buf = ctypes.create_unicode_buffer(64)
                        user32.GetWindowTextW(btn, buf, 64)
                        t_val = buf.value
                        if "Open" in t_val or "打开" in t_val or t_val.startswith("打"):
                            user32.SendMessageW(btn, BM_CLICK, 0, 0)
                            break
                        btn = user32.FindWindowExW(hwnd, btn, "Button", None)
                    else:
                        # 没找到按钮，用 Enter 键提交
                        VK_RETURN = 0x0D
                        user32.keybd_event(VK_RETURN, 0, 0, 0)
                        user32.keybd_event(VK_RETURN, 0, 2, 0)
                else:
                    # 极端 fallback：pyautogui（Edit 控件找不到时才用）
                    try:
                        import pyperclip
                        import pyautogui
                        pyperclip.copy(file_str)
                        _time.sleep(0.1)
                        pyautogui.hotkey("ctrl", "a")
                        _time.sleep(0.15)
                        pyautogui.hotkey("ctrl", "v")
                        _time.sleep(0.4)
                        pyautogui.press("enter")
                    except Exception:
                        pass

                dialog_found[0] = True
                dialog_ok.set()
                return

        dialog_ok.set()

    t = threading.Thread(target=_fill_dialog, daemon=True)
    t.start()

    _activate_chrome_window()
    _time.sleep(0.3)

    try:
        page.click('ytcp-button#select-files-button, #select-files-button', timeout=timeout)
    except PWTimeout:
        dialog_ok.set()
        on_log("  ✗ 未找到文件上传按钮")
        return False

    dialog_ok.wait(timeout=20)

    if not dialog_found[0]:
        on_log("  ✗ 文件选择对话框未出现（Chrome 在后台且无法激活，将触发重试）")
        return False

    try:
        page.wait_for_selector(
            'ytcp-video-metadata-editor #title-textarea',
            timeout=30_000
        )
        on_log("  ✓ 文件已选择")
        return True
    except PWTimeout:
        on_log("  ✗ 文件选择后标题框未出现，将触发重试")
        return False
