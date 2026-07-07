"""LLM backend for storyboard and structured analysis calls."""
from __future__ import annotations

import httpx


class LLMBackend:
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        style_suffix: str = "",
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.style_suffix = style_suffix

    def storyboard(self, segment_text: str) -> str:
        """Convert one novel segment into one image prompt."""
        text = self.complete(self.system_prompt, segment_text, max_tokens=200, temperature=0.7)
        if self.style_suffix and self.style_suffix.lower() not in text.lower():
            text = f"{text}, {self.style_suffix}"
        return text

    def complete(
        self,
        system_prompt: str,
        user_text: str,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.2,
    ) -> str:
        """Generic chat completion used by analysis stages that need structured JSON."""
        if self.provider == "claude":
            return self._claude_complete(system_prompt, user_text, max_tokens=max_tokens, temperature=temperature)
        return self._openai_complete(system_prompt, user_text, max_tokens=max_tokens, temperature=temperature)

    def _openai_complete(self, system_prompt: str, user_text: str, *, max_tokens: int, temperature: float) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        r = httpx.post(url, json=payload, headers=headers, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    def _claude_complete(self, system_prompt: str, user_text: str, *, max_tokens: int, temperature: float) -> str:
        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_text}],
        }
        r = httpx.post(url, json=payload, headers=headers, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"].strip()
