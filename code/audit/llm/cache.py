"""Content-addressed response cache. Keys ignore the model so a fallback answer is reused."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _stable(messages: list[dict]) -> list[dict]:
    """Replace inline image data with its digest so keys and stored requests stay small."""
    out = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    parts.append({"type": "image_url", "sha256": hashlib.sha256(url.encode()).hexdigest()})
                else:
                    parts.append(part)
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out


class ResponseCache:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(purpose: str, messages: list[dict], schema: dict) -> str:
        blob = json.dumps({"purpose": purpose, "messages": _stable(messages), "schema": schema},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, purpose: str, key: str) -> Path:
        return self.root / purpose / f"{key[:24]}.json"

    def get(self, purpose: str, key: str) -> dict | None:
        path = self._path(purpose, key)
        if path.exists():
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("key") == key:
                return entry
        return None

    def put(self, purpose: str, key: str, entry: dict) -> None:
        path = self._path(purpose, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, **entry}, indent=2, sort_keys=True, ensure_ascii=False),
                        encoding="utf-8")

    def stable_request(self, messages: list[dict]) -> list[dict]:
        return _stable(messages)
