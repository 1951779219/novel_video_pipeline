"""Generate the online update manifest JSON."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, dest="zip_path")
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default="latest.json")
    parser.add_argument("--notes", default="")
    parser.add_argument("--mandatory", action="store_true")
    args = parser.parse_args()

    zip_path = Path(args.zip_path)
    manifest = {
        "version": args.version,
        "url": args.url,
        "sha256": sha256(zip_path),
        "notes": args.notes,
        "mandatory": bool(args.mandatory),
    }
    out = Path(args.out)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
