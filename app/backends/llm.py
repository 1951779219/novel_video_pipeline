"""LLM backend：用于把日语小说段落转换为 SD 文生图 prompt。
所有 provider 都走 OpenAI 兼容接口（绝大多数都支持），ollama 也支持 /v1。
"""
import httpx


class LLMBackend:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str,
                 system_prompt: str, style_suffix: str = ""):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.style_suffix = style_suffix

    def storyboard(self, segment_text: str) -> str:
        """输入一段小说文本，返回一个英文 SD prompt。"""
        if self.provider == "claude":
            return self._claude(segment_text)
        return self._openai_compat(segment_text)

    def _openai_compat(self, segment_text: str) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": segment_text},
            ],
            "temperature": 0.7,
            "max_tokens": 200,
        }
        r = httpx.post(url, json=payload, headers=headers, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        prompt = data["choices"][0]["message"]["content"].strip()
        if self.style_suffix and self.style_suffix.lower() not in prompt.lower():
            prompt = f"{prompt}, {self.style_suffix}"
        return prompt

    def _claude(self, segment_text: str) -> str:
        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 200,
            "system": self.system_prompt,
            "messages": [{"role": "user", "content": segment_text}],
        }
        r = httpx.post(url, json=payload, headers=headers, timeout=60.0)
        r.raise_for_status()
        data = r.json()
        prompt = data["content"][0]["text"].strip()
        if self.style_suffix and self.style_suffix.lower() not in prompt.lower():
            prompt = f"{prompt}, {self.style_suffix}"
        return prompt
