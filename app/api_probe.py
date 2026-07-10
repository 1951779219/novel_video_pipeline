"""API connectivity and model discovery helpers for the desktop GUI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
import time
from typing import Any

import httpx
from app.utils.http import http_client
from app.utils.secrets import clean_api_key, redact_secret_text


DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "custom": "",
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://127.0.0.1:11434/v1",
    "claude": "https://api.anthropic.com/v1",
    "sdwebui": "http://127.0.0.1:7860",
    "comfyui": "http://127.0.0.1:8188",
    "voicevox": "http://127.0.0.1:50021",
}


@dataclass
class ProbeResult:
    name: str
    provider: str
    ok: bool
    endpoint: str = ""
    message: str = ""
    models: list[str] = field(default_factory=list)
    suggested_model: str = ""
    details: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [
            f"检测项: {self.name}",
            f"Provider: {self.provider}",
            f"状态: {'可用' if self.ok else '不可用/未确认'}",
        ]
        if self.endpoint:
            lines.append(f"Endpoint: {self.endpoint}")
        if self.message:
            lines.append(f"说明: {self.message}")
        if self.suggested_model:
            lines.append(f"建议模型: {self.suggested_model}")
        if self.models:
            lines.append("")
            lines.append(f"识别到 {len(self.models)} 个模型/声音，前 80 个：")
            lines.extend(f"- {model}" for model in self.models[:80])
            if len(self.models) > 80:
                lines.append(f"... 还有 {len(self.models) - 80} 个")
        if self.details:
            lines.append("")
            lines.extend(self.details)
        return "\n".join(lines)


def _base(provider: str, base_url: str, fallback: str = "") -> str:
    value = str(base_url or "").strip().rstrip("/")
    if value:
        return value
    return fallback or DEFAULT_BASES.get(str(provider or "").lower(), "")


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _bearer_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = clean_api_key(api_key)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        body = redact_secret_text(response.text[:500].replace("\n", " "))
        return f"HTTP {response.status_code}: {body}"
    return redact_secret_text(exc)


def _extract_model_ids(data: Any) -> list[str]:
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            rows = data["data"]
        elif isinstance(data.get("models"), list):
            rows = data["models"]
        else:
            rows = []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    models: list[str] = []
    for row in rows:
        if isinstance(row, str):
            models.append(row)
        elif isinstance(row, dict):
            value = row.get("id") or row.get("name") or row.get("model") or row.get("model_id")
            if value:
                models.append(str(value))
    return sorted(dict.fromkeys(models))


def _prefer_model(models: list[str], current: str, keywords: list[str]) -> str:
    current = str(current or "").strip()
    if current:
        return current
    lower_rows = [(model, model.lower()) for model in models]
    for keyword in keywords:
        for model, lower in lower_rows:
            if keyword in lower:
                return model
    return models[0] if models else ""


def _openai_models(base_url: str, api_key: str, extra_headers: dict[str, str] | None = None) -> tuple[list[str], str]:
    headers = _bearer_headers(api_key)
    if extra_headers:
        headers.update(extra_headers)
    endpoint = _join(base_url, "/models")
    with http_client(endpoint, timeout=20.0) as client:
        response = client.get(endpoint, headers=headers)
        response.raise_for_status()
        return _extract_model_ids(response.json()), endpoint


def _openai_chat_smoke(base_url: str, api_key: str, model: str) -> str:
    endpoint = _join(base_url, "/chat/completions")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    with http_client(endpoint, timeout=30.0) as client:
        response = client.post(endpoint, headers=_bearer_headers(api_key), json=payload)
        response.raise_for_status()
    return endpoint


def probe_llm(provider: str, base_url: str, api_key: str, model: str) -> ProbeResult:
    provider = str(provider or "openai").lower()
    base = _base(provider, base_url, DEFAULT_BASES.get(provider, ""))
    if provider != "ollama" and not base:
        return ProbeResult("分镜 LLM", provider, False, message="请先填写 Base URL/中转地址")
    if provider in {"openai", "custom", "deepseek", "gemini"} and not api_key:
        return ProbeResult("分镜 LLM", provider, False, endpoint=base, message="该接口通常需要 API Key")

    if provider == "ollama":
        return _probe_ollama(base, model)
    if provider == "claude":
        return _probe_claude(base, api_key, model)

    details: list[str] = []
    models: list[str] = []
    endpoint = base
    try:
        models, endpoint = _openai_models(base, api_key)
    except Exception as exc:
        details.append(f"/models 失败: {_http_error(exc)}")

    suggested = _prefer_model(models, model, ["gpt", "deepseek", "qwen", "glm", "doubao", "moonshot", "yi-", "llama", "gemini"])
    if suggested:
        try:
            endpoint = _openai_chat_smoke(base, api_key, suggested)
            return ProbeResult("分镜 LLM", provider, True, endpoint, "模型列表和 chat/completions 均可用", models, suggested, details)
        except Exception as exc:
            details.append(f"chat/completions 失败: {_http_error(exc)}")
            return ProbeResult("分镜 LLM", provider, bool(models), endpoint, "模型列表可访问，但当前模型未通过极小 chat 测试", models, suggested, details)

    return ProbeResult("分镜 LLM", provider, bool(models), endpoint, "模型列表可访问，但没有可测试的模型名" if models else "未能识别模型", models, "", details)


def _probe_claude(base_url: str, api_key: str, model: str) -> ProbeResult:
    api_key = clean_api_key(api_key)
    if not api_key:
        return ProbeResult("分镜 LLM", "claude", False, endpoint=base_url, message="Claude 接口需要 API Key")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    details: list[str] = []
    models: list[str] = []
    endpoint = _join(base_url, "/models")
    try:
        with http_client(endpoint, timeout=20.0) as client:
            response = client.get(endpoint, headers=headers)
            response.raise_for_status()
            models = _extract_model_ids(response.json())
    except Exception as exc:
        details.append(f"/models 失败: {_http_error(exc)}")

    suggested = _prefer_model(models, model, ["claude"])
    if suggested:
        try:
            payload = {
                "model": suggested,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            endpoint = _join(base_url, "/messages")
            with http_client(endpoint, timeout=30.0) as client:
                response = client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
            return ProbeResult("分镜 LLM", "claude", True, endpoint, "模型列表和 messages 均可用", models, suggested, details)
        except Exception as exc:
            details.append(f"messages 失败: {_http_error(exc)}")
    return ProbeResult("分镜 LLM", "claude", bool(models), endpoint, "模型列表可访问" if models else "未能确认 Claude 接口", models, suggested, details)


def _probe_ollama(base_url: str, model: str) -> ProbeResult:
    base = base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    details: list[str] = []
    models: list[str] = []
    endpoint = _join(root, "/api/tags")
    try:
        with http_client(endpoint, timeout=10.0) as client:
            response = client.get(endpoint)
            response.raise_for_status()
            data = response.json()
            models = _extract_model_ids(data.get("models", []))
    except Exception as exc:
        details.append(f"/api/tags 失败: {_http_error(exc)}")
        try:
            models, endpoint = _openai_models(base, "")
        except Exception as second:
            details.append(f"/v1/models 失败: {_http_error(second)}")
    suggested = _prefer_model(models, model, ["qwen", "llama", "deepseek", "gemma", "mistral"])
    return ProbeResult("分镜 LLM", "ollama", bool(models), endpoint, "Ollama 可访问" if models else "未检测到 Ollama 模型", models, suggested, details)


def probe_image(provider: str, base_url: str, api_key: str, model: str, name: str = "图片 API") -> ProbeResult:
    api_key = clean_api_key(api_key)
    provider = str(provider or "placeholder").lower()
    if provider in {"same_as_image", "同配图", "同图片"}:
        provider = "same_as_image"
    if provider == "placeholder":
        return ProbeResult(name, provider, True, message="placeholder 不需要外部 API")
    if provider in {"openai", "custom"}:
        base = _base(provider, base_url, DEFAULT_BASES["openai"] if provider == "openai" else "")
        if not base:
            return ProbeResult(name, provider, False, message="请先填写 Base URL/中转地址")
        if not api_key:
            return ProbeResult(name, provider, False, endpoint=base, message="该接口通常需要 API Key")
        return _probe_openai_image_models(name, provider, base, api_key, model)
    if provider == "sdwebui":
        return _probe_sdwebui(base_url, name)
    if provider == "comfyui":
        return _probe_comfyui(base_url, name)
    if provider == "replicate":
        return _probe_replicate(api_key, model, name)
    if provider == "aliyun":
        return _probe_aliyun(api_key, model, name)
    return ProbeResult(name, provider, False, message="未知或暂未支持检测的图片 Provider")


def _probe_openai_image_models(name: str, provider: str, base: str, api_key: str, model: str) -> ProbeResult:
    details: list[str] = ["检测不会实际生成图片，避免消耗出图额度。"]
    try:
        models, endpoint = _openai_models(base, api_key)
    except Exception as exc:
        return ProbeResult(name, provider, False, _join(base, "/models"), f"/models 不可用: {_http_error(exc)}", details=details)
    suggested = _prefer_model(models, model, ["gpt-image", "dall", "image", "flux", "sd", "stable", "wanx"])
    message = "模型列表可访问"
    if model and models and model not in models:
        message = "模型列表可访问，但当前填写的模型名不在列表中"
    return ProbeResult(name, provider, True, endpoint, message, models, suggested, details)


def _probe_sdwebui(base_url: str, name: str) -> ProbeResult:
    base = _base("sdwebui", base_url)
    endpoint = _join(base, "/sdapi/v1/sd-models")
    try:
        with http_client(endpoint, timeout=20.0) as client:
            response = client.get(endpoint)
            response.raise_for_status()
            data = response.json()
        models = sorted(str(item.get("model_name") or item.get("title") or "") for item in data if item)
        models = [m for m in models if m]
        return ProbeResult(name, "sdwebui", True, endpoint, "SD WebUI 可访问", models, models[0] if models else "")
    except Exception as exc:
        return ProbeResult(name, "sdwebui", False, endpoint, _http_error(exc))


def _probe_comfyui(base_url: str, name: str) -> ProbeResult:
    base = _base("comfyui", base_url)
    details: list[str] = []
    models: list[str] = []
    endpoint = _join(base, "/system_stats")
    try:
        with http_client(endpoint, timeout=20.0) as client:
            response = client.get(endpoint)
            response.raise_for_status()
            details.append("system_stats 可访问")
            object_info = client.get(_join(base, "/object_info"))
            object_info.raise_for_status()
            data = object_info.json()
        loader = data.get("CheckpointLoaderSimple") or data.get("UNETLoader") or {}
        required = (loader.get("input") or {}).get("required") or {}
        for value in required.values():
            if isinstance(value, list) and value and isinstance(value[0], list):
                models.extend(str(item) for item in value[0])
                break
        models = sorted(dict.fromkeys(models))
        return ProbeResult(name, "comfyui", True, endpoint, "ComfyUI 可访问", models, models[0] if models else "", details)
    except Exception as exc:
        return ProbeResult(name, "comfyui", False, endpoint, _http_error(exc), models, "", details)


def _probe_replicate(api_key: str, model: str, name: str) -> ProbeResult:
    api_key = clean_api_key(api_key)
    endpoint = "https://api.replicate.com/v1/models"
    if not api_key:
        return ProbeResult(name, "replicate", False, endpoint, "Replicate 需要 API Token")
    try:
        with http_client(endpoint, timeout=20.0) as client:
            response = client.get(endpoint, headers={"Authorization": f"Token {api_key}"})
            response.raise_for_status()
            data = response.json()
        rows = data.get("results") if isinstance(data, dict) else []
        models = []
        for row in rows or []:
            owner = (row.get("owner") or "").strip()
            model_name = (row.get("name") or "").strip()
            if owner and model_name:
                models.append(f"{owner}/{model_name}")
        suggested = _prefer_model(models, model, ["flux", "sdxl", "stable", "image"])
        return ProbeResult(name, "replicate", True, endpoint, "Replicate API 可访问；当前出图仍需要填写可用 version/model", models, suggested)
    except Exception as exc:
        return ProbeResult(name, "replicate", False, endpoint, _http_error(exc))


def _probe_aliyun(api_key: str, model: str, name: str) -> ProbeResult:
    api_key = clean_api_key(api_key)
    if not api_key:
        return ProbeResult(name, "aliyun", False, "https://dashscope.aliyuncs.com", "阿里云 DashScope 需要 API Key")
    models = ["wanx-v1"]
    suggested = model or "wanx-v1"
    return ProbeResult(name, "aliyun", True, "https://dashscope.aliyuncs.com", "DashScope 文生图接口没有稳定通用的模型列表检测；已确认配置具备 API Key，运行时会做真实调用", models, suggested)


def probe_tts(provider: str, base_url: str, api_key: str, voice: str, model: str) -> ProbeResult:
    api_key = clean_api_key(api_key)
    provider = str(provider or "edge").lower()
    if provider == "silent":
        return ProbeResult("TTS API", provider, True, message="silent 是本地静音占位，不需要外部 API")
    if provider == "edge":
        return _probe_edge_tts(voice)
    if provider == "voicevox":
        return _probe_voicevox(base_url)
    if provider in {"openai", "custom"}:
        base = _base("openai", base_url, DEFAULT_BASES["openai"])
        if not api_key:
            return ProbeResult("TTS API", provider, False, endpoint=base, message="OpenAI TTS/自定义 TTS 通常需要 API Key")
        try:
            models, endpoint = _openai_models(base, api_key)
            suggested = _prefer_model(models, model, ["tts", "speech", "audio", "gpt-4o-mini-tts"])
            return ProbeResult("TTS API", provider, True, endpoint, "模型列表可访问；检测不会实际生成音频，避免消耗额度", models, suggested)
        except Exception as exc:
            return ProbeResult("TTS API", provider, False, _join(base, "/models"), _http_error(exc))
    if provider == "elevenlabs":
        return _probe_elevenlabs(base_url, api_key, voice)
    if provider == "azure":
        return _probe_azure_tts(api_key)
    return ProbeResult("TTS API", provider, False, message="未知或暂未支持检测的 TTS Provider")


def _probe_edge_tts(voice: str) -> ProbeResult:
    voice = str(voice or "zh-CN-XiaoxiaoNeural").strip()
    try:
        import edge_tts
    except Exception as exc:
        return ProbeResult("TTS API", "edge", False, message=f"edge-tts 未安装或导入失败: {exc}")

    async def _run() -> tuple[list[str], Path, float]:
        start = time.perf_counter()
        voices = await edge_tts.list_voices()
        voice_names = sorted(str(item.get("ShortName") or "") for item in voices if item.get("ShortName"))
        test_voice = voice if voice in voice_names else (voice_names[0] if voice_names else voice)
        out = Path(tempfile.gettempdir()) / f"novel_video_edge_probe_{int(time.time())}.mp3"
        text = "这是一个语音合成检测。"
        await edge_tts.Communicate(text, voice=test_voice, rate="+0%").save(str(out))
        elapsed = time.perf_counter() - start
        return voice_names, out, elapsed

    try:
        models, out, elapsed = asyncio.run(_run())
        size = out.stat().st_size if out.exists() else 0
        out.unlink(missing_ok=True)
        if size <= 0:
            raise RuntimeError("生成文件为空")
        return ProbeResult(
            "TTS API",
            "edge",
            True,
            "Microsoft Edge online TTS",
            f"edge-tts 不需要 API Key；当前声音可合成，测试耗时 {elapsed:.2f}s",
            models,
            voice if voice in models else (models[0] if models else ""),
        )
    except Exception as exc:
        return ProbeResult(
            "TTS API",
            "edge",
            False,
            "Microsoft Edge online TTS",
            f"edge-tts 不需要 API Key，但当前声音/网络未通过真实合成: {exc}",
        )


def _probe_voicevox(base_url: str) -> ProbeResult:
    base = _base("voicevox", base_url)
    endpoint = _join(base, "/speakers")
    try:
        with http_client(endpoint, timeout=20.0) as client:
            version_resp = client.get(_join(base, "/version"))
            version = version_resp.text.strip() if version_resp.status_code == 200 else ""
            response = client.get(endpoint)
            response.raise_for_status()
            data = response.json()
        models = []
        for speaker in data:
            for style in speaker.get("styles", []):
                models.append(f'{style.get("id")} {speaker.get("name")} / {style.get("name")}')
        details = [f"version: {version}"] if version else []
        return ProbeResult("TTS API", "voicevox", True, endpoint, "VOICEVOX 可访问", models, models[0] if models else "", details)
    except Exception as exc:
        return ProbeResult("TTS API", "voicevox", False, endpoint, _http_error(exc))


def _probe_elevenlabs(base_url: str, api_key: str, voice: str) -> ProbeResult:
    base = _base("elevenlabs", base_url, "https://api.elevenlabs.io/v1")
    endpoint = _join(base, "/voices")
    if not api_key:
        return ProbeResult("TTS API", "elevenlabs", False, endpoint, "ElevenLabs 需要 API Key")
    try:
        headers = {"xi-api-key": api_key}
        with http_client(endpoint, timeout=20.0) as client:
            response = client.get(endpoint, headers=headers)
            response.raise_for_status()
            data = response.json()
        models = [f'{v.get("voice_id")} {v.get("name")}' for v in data.get("voices", []) if v.get("voice_id")]
        suggested = voice if voice else (models[0].split(" ", 1)[0] if models else "")
        return ProbeResult("TTS API", "elevenlabs", True, endpoint, "ElevenLabs voices 可访问", models, suggested)
    except Exception as exc:
        return ProbeResult("TTS API", "elevenlabs", False, endpoint, _http_error(exc))


def _probe_azure_tts(api_key: str) -> ProbeResult:
    if not api_key:
        return ProbeResult("TTS API", "azure", False, message="Azure TTS 需要 API Key；region 当前在高级参数中配置")
    return ProbeResult("TTS API", "azure", True, message="已填写 API Key。完整 voices/list 检测需要 region 配置入口，后续可继续补。")
