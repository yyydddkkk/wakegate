from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

_MASK = 0xFFFFFFFF


def _normalize(value: Any) -> Any:
    """Sort keys and print whole floats as integers, so 1.0 and 1 hash alike (as in JSON from JS)."""
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canonical_json(value: Any) -> str:
    """JSON with sorted object keys, so equal data always serialises equally."""
    return json.dumps(_normalize(value), separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def omit_paths(data: Any, paths: list[str] | tuple[str, ...]) -> Any:
    """A deep copy of `data` with the given dotted paths removed."""
    if not paths or not isinstance(data, dict):
        return data
    out = copy.deepcopy(data)
    for path in paths:
        parts = path.split(".")
        cur: Any = out
        for p in parts[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
    return out


def _imul(a: int, b: int) -> int:
    return (a * b) & _MASK


def seeded_random(seed: int):
    """Small seeded PRNG (mulberry32), bit-identical to the TypeScript version, so samples are reproducible."""
    a = seed & _MASK

    def rnd() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & _MASK
        t = a
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul(t ^ (t >> 7), t | 61)) & _MASK)) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    return rnd
