"""Short-lived subprocess entrypoint for one TTS segment."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from app.backends.tts import TTSBackend


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        print("usage: python -m app.tts_worker payload.json result.json", file=sys.stderr)
        return 2

    payload_path = Path(args[0])
    result_path = Path(args[1])
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        backend = TTSBackend(
            provider=str(payload.get("provider") or "edge"),
            voice=str(payload.get("voice") or ""),
            rate=str(payload.get("rate") or "+0%"),
            api_key=str(payload.get("api_key") or ""),
            base_url=str(payload.get("base_url") or ""),
            extra=payload.get("extra") if isinstance(payload.get("extra"), dict) else {},
            volume=payload.get("volume", 1.0),
        )
        duration = backend.synth(str(payload.get("text") or ""), Path(str(payload.get("out_path") or "")))
        result = {"ok": True, "duration": float(duration)}
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
        try:
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
