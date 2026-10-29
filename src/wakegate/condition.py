"""Declarative conditions. No code execution: a condition is data, so a config
file can be reviewed and audited without trusting it.

  {all: [c, ...]} | {any: [c, ...]} | {not: c}
  {path: "data.actor", eq | ne | in | matches | contains | exists | gt | gte | lt | lte: ...}
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .hashing import get_path

Predicate = Callable[[dict[str, Any]], bool]

_LEAF_OPS = ("eq", "ne", "in", "matches", "contains", "exists", "gt", "gte", "lt", "lte")
_MAX_REGEX_INPUT = 10_000


def _strict_eq(a: Any, b: Any) -> bool:
    """JS-style ===: True is not 1, and a string is not a number."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) != isinstance(b, (int, float)):
        return False
    return a == b


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def compile_condition(raw: Any, where: str) -> Predicate:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: condition must be an object")

    if "all" in raw or "any" in raw:
        kind = "all" if "all" in raw else "any"
        items = raw[kind]
        if not isinstance(items, list) or not items:
            raise ValueError(f"{where}.{kind}: expected a non-empty list")
        subs = [compile_condition(c, f"{where}.{kind}[{i}]") for i, c in enumerate(items)]
        if kind == "all":
            return lambda e: all(p(e) for p in subs)
        return lambda e: any(p(e) for p in subs)
    if "not" in raw:
        sub = compile_condition(raw["not"], f"{where}.not")
        return lambda e: not sub(e)

    path = raw.get("path")
    if not isinstance(path, str):
        raise ValueError(f'{where}: expected "path" or all/any/not')
    unknown = [k for k in raw if k != "path" and k not in _LEAF_OPS]
    if unknown:
        raise ValueError(f'{where}: unknown key "{unknown[0]}"')
    ops = [op for op in _LEAF_OPS if op in raw]
    if len(ops) != 1:
        raise ValueError(f"{where}: exactly one operator required ({', '.join(_LEAF_OPS)})")
    op = ops[0]
    arg = raw[op]

    def get(e: dict[str, Any]) -> Any:
        return get_path(e, path)

    if op == "eq":
        return lambda e: _strict_eq(get(e), arg)
    if op == "ne":
        return lambda e: not _strict_eq(get(e), arg)
    if op == "in":
        if not isinstance(arg, list):
            raise ValueError(f"{where}.in: expected a list")
        return lambda e: any(_strict_eq(get(e), x) for x in arg)
    if op == "matches":
        if not isinstance(arg, str):
            raise ValueError(f"{where}.matches: expected a string")
        regex = re.compile(arg)

        def matches(e: dict[str, Any]) -> bool:
            v = get(e)
            return isinstance(v, str) and len(v) <= _MAX_REGEX_INPUT and regex.search(v) is not None

        return matches
    if op == "contains":
        if not isinstance(arg, str):
            raise ValueError(f"{where}.contains: expected a string")

        def contains(e: dict[str, Any]) -> bool:
            v = get(e)
            if isinstance(v, str):
                return arg in v
            return isinstance(v, list) and any(_strict_eq(x, arg) for x in v)

        return contains
    if op == "exists":
        return lambda e: (get(e) is not None) == bool(arg)

    if not _is_number(arg):
        raise ValueError(f"{where}.{op}: expected a number")
    cmp = {
        "gt": lambda a: a > arg,
        "gte": lambda a: a >= arg,
        "lt": lambda a: a < arg,
        "lte": lambda a: a <= arg,
    }[op]

    def numeric(e: dict[str, Any]) -> bool:
        v = get(e)
        return _is_number(v) and cmp(v)

    return numeric
