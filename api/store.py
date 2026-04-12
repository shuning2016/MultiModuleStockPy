"""
Persistence layer — abstracted store with three backends:

  1. File store  (default / local dev)  → ./data/*.json
  2. Vercel KV   (production)           → set KV_REST_API_URL + KV_REST_API_TOKEN env vars
  3. Memory      (fallback / testing)   → pure in-process dict

Backend is selected automatically:
  - KV_REST_API_URL set  → Vercel KV
  - otherwise            → File store (creates ./data/ dir)
  - DATA_DIR=":memory:"  → Memory (useful for tests)

Public API (same for all backends):
  kv_get(key)          → value | None
  kv_set(key, value)   → None
  kv_del(key)          → None
  kv_keys(prefix)      → list[str]
"""

from __future__ import annotations
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

# ── Choose backend ────────────────────────────────────────────────────────────
_KV_URL   = os.environ.get("KV_REST_API_URL", "")
_KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")
_DATA_DIR = os.environ.get("DATA_DIR", "")

if _DATA_DIR == ":memory:":
    _BACKEND = "memory"
elif _KV_URL and _KV_TOKEN:
    _BACKEND = "vercel_kv"
else:
    _BACKEND = "file"

# ─────────────────────────────────────────────────────────────────────────────
# Memory backend
# ─────────────────────────────────────────────────────────────────────────────
_MEM: dict[str, str] = {}
_MEM_LOCK = threading.Lock()


def _mem_get(key: str) -> Optional[str]:
    with _MEM_LOCK:
        return _MEM.get(key)

def _mem_set(key: str, value: str) -> None:
    with _MEM_LOCK:
        _MEM[key] = value

def _mem_del(key: str) -> None:
    with _MEM_LOCK:
        _MEM.pop(key, None)

def _mem_keys(prefix: str = "") -> list[str]:
    with _MEM_LOCK:
        return [k for k in _MEM if k.startswith(prefix)]


# ─────────────────────────────────────────────────────────────────────────────
# File backend  (local dev / Vercel tmp — ephemeral on Vercel but fine for dev)
# ─────────────────────────────────────────────────────────────────────────────
def _data_dir() -> Path:
    base = _DATA_DIR if _DATA_DIR and _DATA_DIR != ":memory:" else "data"
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p

def _safe_filename(key: str) -> str:
    """Convert a KV key like 'state:grok' → 'state__grok.json'"""
    return re.sub(r"[^a-zA-Z0-9_\-]", "__", key) + ".json"

def _file_path(key: str) -> Path:
    return _data_dir() / _safe_filename(key)

def _file_get(key: str) -> Optional[str]:
    p = _file_path(key)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return None
    return None

def _file_set(key: str, value: str) -> None:
    _file_path(key).write_text(value, encoding="utf-8")

def _file_del(key: str) -> None:
    p = _file_path(key)
    if p.exists():
        p.unlink()

def _file_keys(prefix: str = "") -> list[str]:
    d = _data_dir()
    results = []
    for p in d.glob("*.json"):
        # Reverse-engineer the key from filename
        stem = p.stem  # without .json
        key = stem.replace("__", ":")  # best-effort reverse
        if key.startswith(prefix):
            results.append(key)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Vercel KV backend  (REST API — no native SDK needed)
# ─────────────────────────────────────────────────────────────────────────────
def _kv_get(key: str) -> Optional[str]:
    import urllib.request
    url = f"{_KV_URL}/get/{_kv_encode(key)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_KV_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("result")
    except Exception:
        return None

def _kv_set(key: str, value: str) -> None:
    import urllib.request
    url = f"{_KV_URL}/set/{_kv_encode(key)}"
    body = json.dumps(value).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {_KV_TOKEN}",
                 "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def _kv_del(key: str) -> None:
    import urllib.request
    url = f"{_KV_URL}/del/{_kv_encode(key)}"
    req = urllib.request.Request(url, method="POST",
                                  headers={"Authorization": f"Bearer {_KV_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def _kv_keys(prefix: str = "") -> list[str]:
    import urllib.request
    url = f"{_KV_URL}/keys/{_kv_encode(prefix)}*"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_KV_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("result", [])
    except Exception:
        return []

def _kv_encode(key: str) -> str:
    """URL-encode a KV key."""
    import urllib.parse
    return urllib.parse.quote(key, safe="")


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────────────
def kv_get(key: str) -> Optional[str]:
    if _BACKEND == "vercel_kv": return _kv_get(key)
    if _BACKEND == "file":      return _file_get(key)
    return _mem_get(key)

def kv_set(key: str, value: str) -> None:
    if _BACKEND == "vercel_kv": _kv_set(key, value)
    elif _BACKEND == "file":    _file_set(key, value)
    else:                       _mem_set(key, value)

def kv_del(key: str) -> None:
    if _BACKEND == "vercel_kv": _kv_del(key)
    elif _BACKEND == "file":    _file_del(key)
    else:                       _mem_del(key)

def kv_keys(prefix: str = "") -> list[str]:
    if _BACKEND == "vercel_kv": return _kv_keys(prefix)
    if _BACKEND == "file":      return _file_keys(prefix)
    return _mem_keys(prefix)


# ─────────────────────────────────────────────────────────────────────────────
# Typed helpers  (JSON serialise/deserialise)
# ─────────────────────────────────────────────────────────────────────────────
def store_get(key: str, default: Any = None) -> Any:
    raw = kv_get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default

def store_set(key: str, value: Any) -> None:
    kv_set(key, json.dumps(value, ensure_ascii=False))

def store_del(key: str) -> None:
    kv_del(key)

def store_keys(prefix: str = "") -> list[str]:
    return kv_keys(prefix)


def backend_info() -> dict:
    return {
        "backend": _BACKEND,
        "kv_url_set": bool(_KV_URL),
        "data_dir": str(_data_dir()) if _BACKEND == "file" else None,
    }
