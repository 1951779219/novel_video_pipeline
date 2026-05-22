"""TTS backend：统一接口 synth(text, out_path) -> seconds
支持：
  - edge       (microsoft edge-tts，免费、零配置)
  - voicevox   (本地，HTTP API http://127.0.0.1:50021)
  - openai     (OpenAI TTS，需 API key)
  - azure      (Azure Cognitive Services TTS)
  - elevenlabs (ElevenLabs)
  - custom     (自定义 OpenAI 兼容 audio/speech 端点)
"""
import asyncio
import json
import wave
import contextlib
from pathlib import Path

import httpx


class TTSBackend:
    def __init__(self, provider: str, voice: str, rate: str = "+0%",
                 api_key: str = "", base_url: str = "", extra: dict | None = None,
                 volume: float | str = 1.0):
        self.provider = provider
        self.voice = voice
        self.rate = rate
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.extra = extra or {}
        self.volume = _volume_multiplier(volume)

    def synth(self, text: str, out_path: Path) -> float:
        """合成音频写入 out_path（mp3 或 wav），返回时长（秒）。"""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        method = getattr(self, f"_synth_{self.provider}", None)
        if method is None:
            raise ValueError(f"未知 TTS provider: {self.provider}")
        method(text, out_path)
        if self.provider != "silent":
            self._apply_volume(out_path)
        return _audio_duration(out_path)

    def _apply_volume(self, out_path: Path):
        if abs(float(self.volume) - 1.0) < 0.001:
            return
        import subprocess
        from app.utils.ffmpeg import ffmpeg_path

        tmp = out_path.with_name(out_path.stem + ".volume.tmp" + out_path.suffix)
        tmp.unlink(missing_ok=True)
        suffix = out_path.suffix.lower()
        audio_codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if suffix == ".mp3" else ["-c:a", "pcm_s16le"]
        try:
            subprocess.run(
                [
                    ffmpeg_path(),
                    "-y",
                    "-i",
                    str(out_path),
                    "-filter:a",
                    f"volume={float(self.volume):.4f}",
                    *audio_codec,
                    str(tmp),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            tmp.replace(out_path)
        finally:
            tmp.unlink(missing_ok=True)

    # ── edge-tts（免费） ─────────────────────────────────
    def _synth_edge(self, text: str, out_path: Path):
        import edge_tts
        async def _run():
            comm = edge_tts.Communicate(text, voice=self.voice, rate=self.rate)
            await comm.save(str(out_path))
        asyncio.run(_run())

    # ── voicevox（本地） ─────────────────────────────────
    def _synth_voicevox(self, text: str, out_path: Path):
        base = self.base_url or "http://127.0.0.1:50021"
        speaker = int(self.voice) if str(self.voice).isdigit() else 1
        # 1) audio_query
        q = httpx.post(f"{base}/audio_query", params={"text": text, "speaker": speaker}, timeout=60.0)
        q.raise_for_status()
        # 2) synthesis
        s = httpx.post(f"{base}/synthesis", params={"speaker": speaker},
                       headers={"Content-Type": "application/json"},
                       content=q.content, timeout=120.0)
        s.raise_for_status()
        out_path.write_bytes(s.content)

    # ── OpenAI TTS ───────────────────────────────────────
    def _synth_openai(self, text: str, out_path: Path):
        base = self.base_url or "https://api.openai.com/v1"
        url = f"{base}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.extra.get("model", "tts-1"),
            "input": text,
            "voice": self.voice or "alloy",
            "response_format": "mp3",
        }
        instructions = self.extra.get("instructions") or self.extra.get("emotion") or ""
        if instructions:
            payload["instructions"] = str(instructions)
        r = httpx.post(url, json=payload, headers=headers, timeout=120.0)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── Azure ────────────────────────────────────────────
    def _synth_azure(self, text: str, out_path: Path):
        region = self.extra.get("region", "eastus")
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        ssml = (
            f'<speak version="1.0" xml:lang="ja-JP">'
            f'<voice name="{self.voice}"><prosody rate="{self.rate}">'
            f'{_xml_escape(text)}</prosody></voice></speak>'
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        }
        r = httpx.post(url, content=ssml.encode("utf-8"), headers=headers, timeout=120.0)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── ElevenLabs ───────────────────────────────────────
    def _synth_elevenlabs(self, text: str, out_path: Path):
        base = self.base_url or "https://api.elevenlabs.io/v1"
        url = f"{base}/text-to-speech/{self.voice}"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        payload = {"text": text, "model_id": self.extra.get("model", "eleven_multilingual_v2")}
        r = httpx.post(url, json=payload, headers=headers, timeout=120.0)
        r.raise_for_status()
        out_path.write_bytes(r.content)

    # ── 自定义（OpenAI 兼容 /audio/speech） ──────────────
    def _synth_custom(self, text: str, out_path: Path):
        self._synth_openai(text, out_path)

    def _synth_silent(self, text: str, out_path: Path):
        """Generate silent audio for dry-runs and recovery placeholders."""
        import subprocess
        from app.utils.ffmpeg import ffmpeg_path

        compact = "".join(str(text or "").split())
        duration = max(1.2, min(45.0, len(compact) / 4.2))
        subprocess.run(
            [
                ffmpeg_path(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(out_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ─────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────

def _volume_multiplier(value) -> float:
    text = str(value if value is not None else "1.0").strip()
    if not text:
        return 1.0
    try:
        if text.endswith("%"):
            raw = float(text[:-1])
            multiplier = 1.0 + raw / 100.0 if text.startswith(("+", "-")) else raw / 100.0
        else:
            multiplier = float(text)
    except Exception:
        multiplier = 1.0
    return max(0.0, min(5.0, multiplier))


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _audio_duration(path: Path) -> float:
    """用 ffprobe 读时长（秒）。"""
    import subprocess
    from app.concurrency import media_probe_slot
    from app.utils.ffmpeg import ffprobe_path
    try:
        with media_probe_slot(action="ffprobe"):
            r = subprocess.run(
                [ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=20,
            )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────
# 内置 voice 列表（UI 下拉用，非完整）
# ─────────────────────────────────────────────────────────
EDGE_MULTI_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuMaanNeural",
    "zh-TW-HsiaoChenNeural",
    "ja-JP-NanamiNeural",
    "ja-JP-KeitaNeural",
    "ja-JP-AoiNeural",
    "ja-JP-DaichiNeural",
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
    "ko-KR-SunHiNeural",
    "ko-KR-InJoonNeural",
    "es-ES-ElviraNeural",
    "fr-FR-DeniseNeural",
    "de-DE-KatjaNeural",
]

EDGE_JA_VOICES = EDGE_MULTI_VOICES
