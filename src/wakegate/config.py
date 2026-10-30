from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import re

import yaml

from .condition import Predicate, compile_condition
from .types import Action

class _Yaml12Loader(yaml.SafeLoader):
    """YAML 1.2 core schema scalars. PyYAML follows YAML 1.1, where `off`, `on`, `yes` and `no` are
    booleans and `22:00` is the number 1320. In a rules file that silently changes what a condition
    means (`eq: on` would compare with true), so only true/false are booleans and numbers are plain."""


_Yaml12Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), list("-+0123456789")
)
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float", re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$"), list("-+0123456789.")
)


#: Limits a rule can opt out of. A bypass rule opts out of all of them.
EXEMPTIONS = ("cooldown", "quietHours", "budget")
_ACTIONS = ("wake", "skip", "batch")


@dataclass(frozen=True)
class Rule:
    name: str
    when: Predicate
    #: The condition as written in the config, kept so it can be shown back.
    condition: Any
    then: Action
    #: Limits this rule's wakes ignore (only for rules with then: wake).
    exempt: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayerSpec:
    """One entry of the `layers:` section."""

    name: str
    #: None for a built-in layer; the registered type for any other.
    type: str | None = None
    mode: str = "enforce"
    params: dict[str, Any] = field(default_factory=dict)


#: Where each kind of layer may sit: the order must not go back down.
#: dedupe, then bypass, then rules, then classifiers, then governors.
BUILTIN_LAYERS = {"dedupe": 0, "bypass": 1, "rules": 2, "cooldown": 4, "quiet_hours": 4, "budget": 4}
_CLASSIFIER_RANK = 3
#: A learned layer that skips events starves its own feedback, so enforcing one needs a canary at least this big.
MIN_CANARY_FOR_ENFORCED_CLASSIFIER = 0.02


@dataclass(frozen=True)
class Budget:
    max_wakes_per_day: int | None = None
    #: What to do with a wake that exceeds the budget. "batch" or "skip".
    over_budget: str = "batch"
    quiet_hours: tuple[str, str] | None = None
    #: IANA zone for quiet hours and the day boundary. None means the system zone.
    timezone: str | None = None


@dataclass(frozen=True)
class Config:
    #: Paths inside `data` that are ignored when fingerprinting (timestamps, counters...).
    ignore_paths: tuple[str, ...] = ()
    #: Skip events whose fingerprint equals the last one seen for the same key.
    dedupe: bool = True
    #: Events matching a bypass rule always wake. They skip cooldown, quiet hours and budget.
    bypass: tuple[Rule, ...] = ()
    #: First matching rule decides.
    rules: tuple[Rule, ...] = ()
    #: Used when no rule matches.
    default_action: Action = "wake"
    #: After waking for a key, further changes within this window are batched.
    cooldown_seconds: float = 0
    #: On every wake, also hand over the events batched earlier, so a batch is never stranded.
    flush_on_wake: bool = True
    #: "enforce" delivers verdicts; "shadow" wakes on everything and only records what the rules would have done.
    mode: str = "enforce"
    #: Fraction (0-1) of would-be skips, other than duplicates, delivered as a wake so misses can be measured.
    canary_rate: float = 0.0
    budget: Budget = field(default_factory=Budget)
    #: The ordered `layers:` section, or None to derive the pipeline from the other keys.
    layer_specs: tuple[LayerSpec, ...] | None = None

    def with_shadow(self) -> Config:
        """The same policy in shadow mode (a canary has no meaning there)."""
        return replace(self, mode="shadow", canary_rate=0.0)


def _as_action(v: Any, where: str) -> Action:
    if isinstance(v, str) and v in _ACTIONS:
        return v  # type: ignore[return-value]
    raise ValueError(f"{where}: expected one of {', '.join(_ACTIONS)}")


def _compile_rules(raw: Any, where: str, forced: Action | None = None) -> tuple[Rule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{where}: expected a list")
    out: list[Rule] = []
    for i, r in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(r, dict):
            raise ValueError(f"{at}: expected an object")
        name = r.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f'{at}: "name" is required (it appears in the audit log)')
        then = forced or _as_action(r.get("then"), f"{at}.then")
        exempt: tuple[str, ...] = ()
        if "exempt" in r and r["exempt"] is not None:
            if forced:
                raise ValueError(f'{at}: bypass rules already ignore every limit; "exempt" is for rules')
            if then != "wake":
                raise ValueError(f"{at}.exempt: only rules with then: wake can be exempt")
            ex = r["exempt"]
            if not isinstance(ex, list) or any(x not in EXEMPTIONS for x in ex):
                raise ValueError(f"{at}.exempt: expected a list of {', '.join(EXEMPTIONS)}")
            exempt = tuple(ex)
        out.append(Rule(name, compile_condition(r.get("when"), f"{at}.when"), r.get("when"), then, exempt))
    return tuple(out)


def _is_hhmm(v: Any) -> bool:
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":":
        return False
    h, m = v[:2], v[3:]
    return h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_config(text: str) -> Config:
    raw = yaml.load(text, Loader=_Yaml12Loader) or {}  # noqa: S506 - a SafeLoader subclass
    if not isinstance(raw, dict):
        raise ValueError("config: expected a mapping at the top level")
    if raw.get("version") not in (None, 1):
        raise ValueError(f"unsupported config version {raw['version']}")

    b = raw.get("budget") or {}
    max_wakes = None
    if "maxWakesPerDay" in b:
        v = b["maxWakesPerDay"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("budget.maxWakesPerDay: expected a non-negative integer")
        max_wakes = v
    over = b.get("overBudget", "batch")
    if over not in ("batch", "skip"):
        raise ValueError('budget.overBudget: expected "batch" or "skip"')
    quiet = None
    if "quietHours" in b:
        q = b["quietHours"] or {}
        if not (_is_hhmm(q.get("start")) and _is_hhmm(q.get("end"))):
            raise ValueError('budget.quietHours: expected {start: "HH:MM", end: "HH:MM"}')
        quiet = (q["start"], q["end"])
    tz = None
    if "timezone" in b:
        tz = b["timezone"]
        if not isinstance(tz, str):
            raise ValueError("budget.timezone: expected a string")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f'budget.timezone: unknown zone "{tz}"') from None

    cooldown = raw.get("cooldownSeconds", 0)
    if not _is_number(cooldown) or cooldown < 0:
        raise ValueError("cooldownSeconds: expected a non-negative number")
    ignore = raw.get("ignorePaths") or []
    if not isinstance(ignore, list) or any(not isinstance(p, str) for p in ignore):
        raise ValueError("ignorePaths: expected a list of strings")

    mode = raw.get("mode", "enforce")
    if mode not in ("enforce", "shadow"):
        raise ValueError('mode: expected "enforce" or "shadow"')
    canary = raw.get("canary") or {}
    rate = canary.get("rate", 0)
    if not _is_number(rate) or rate < 0 or rate > 1:
        raise ValueError("canary.rate: expected a number between 0 and 1")
    if mode == "shadow" and rate > 0:
        raise ValueError("canary.rate has no effect in shadow mode (everything already wakes)")

    config = Config(
        ignore_paths=tuple(ignore),
        dedupe=True if raw.get("dedupe") is None else bool(raw["dedupe"]),
        bypass=_compile_rules(raw.get("bypass"), "bypass", "wake"),
        rules=_compile_rules(raw.get("rules"), "rules"),
        default_action=_as_action(raw["default"], "default") if "default" in raw else "wake",
        cooldown_seconds=cooldown,
        flush_on_wake=True if raw.get("flushOnWake") is None else bool(raw["flushOnWake"]),
        mode=mode,
        canary_rate=float(rate),
        budget=Budget(max_wakes, over, quiet, tz),
    )
    specs = _parse_layer_specs(raw.get("layers"))
    if specs is not None:
        _validate_layers(specs, config)
        config = replace(config, layer_specs=specs)
    return config


def _parse_layer_specs(raw: Any) -> tuple[LayerSpec, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("layers: expected a non-empty list")
    specs: list[LayerSpec] = []
    for i, item in enumerate(raw):
        at = f"layers[{i}]"
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ValueError(f"{at}: expected a layer name or an object with name, type, mode, params")
        unknown = [k for k in item if k not in ("name", "type", "mode", "params")]
        if unknown:
            raise ValueError(f'{at}: unknown key "{unknown[0]}"')
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f'{at}: "name" is required')
        mode = item.get("mode", "enforce")
        if mode not in ("off", "shadow", "enforce"):
            raise ValueError(f'{at}.mode: expected "off", "shadow" or "enforce"')
        params = item.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"{at}.params: expected a mapping")
        specs.append(LayerSpec(name, item.get("type"), mode, params))
    return tuple(specs)


def _validate_layers(specs: tuple[LayerSpec, ...], c: Config) -> None:
    from .registry import layer_type

    seen: set[str] = set()
    rank_so_far = -1
    for i, s in enumerate(specs):
        at = f"layers[{i}] ({s.name})"
        if s.name in seen:
            raise ValueError(f'{at}: duplicate layer name "{s.name}"')
        seen.add(s.name)

        if s.type is None:
            if s.name not in BUILTIN_LAYERS:
                raise ValueError(f'{at}: "{s.name}" is not a built-in layer ({", ".join(BUILTIN_LAYERS)}); give it a `type` to use a registered one')
            if s.params:
                raise ValueError(f"{at}: built-in layers take no params; their settings are the top-level keys")
            rank = BUILTIN_LAYERS[s.name]
            missing = {
                "dedupe": None if c.dedupe else "dedupe is set to false",
                "bypass": None if c.bypass else "there are no bypass rules",
                "cooldown": None if c.cooldown_seconds > 0 else "cooldownSeconds is not set",
                "quiet_hours": None if c.budget.quiet_hours else "budget.quietHours is not set",
                "budget": None if c.budget.max_wakes_per_day is not None else "budget.maxWakesPerDay is not set",
            }.get(s.name)
            if missing and s.mode != "off":
                raise ValueError(f"{at}: listed, but {missing}")
        else:
            cls = layer_type(s.type)  # raises with the list of registered types
            rank = _CLASSIFIER_RANK
            if s.mode == "enforce" and c.canary_rate < MIN_CANARY_FOR_ENFORCED_CLASSIFIER:
                raise ValueError(
                    f"{at}: a classifier in enforce mode needs a canary of at least {MIN_CANARY_FOR_ENFORCED_CLASSIFIER:.0%} "
                    "(canary.rate), otherwise the events it skips are never seen again and it cannot be checked. "
                    "Run it in shadow mode first."
                )
            del cls
        if rank < rank_so_far:
            raise ValueError(
                f"{at}: out of order. The order must be dedupe, bypass, rules, then classifiers, then cooldown/quiet_hours/budget"
            )
        rank_so_far = rank
    if "rules" not in seen:
        raise ValueError('layers: "rules" is required (it applies the default action)')


def load_config(path: str | Path) -> Config:
    return parse_config(Path(path).read_text(encoding="utf-8"))


def describe_config(c: Config) -> dict[str, Any]:
    """The active policy as plain data, for showing to a person or an agent."""

    def rule(r: Rule) -> dict[str, Any]:
        d: dict[str, Any] = {"name": r.name, "when": r.condition, "then": r.then}
        if r.exempt:
            d["exempt"] = list(r.exempt)
        return d

    budget: dict[str, Any] = {"overBudget": c.budget.over_budget}
    if c.budget.max_wakes_per_day is not None:
        budget["maxWakesPerDay"] = c.budget.max_wakes_per_day
    if c.budget.quiet_hours:
        budget["quietHours"] = {"start": c.budget.quiet_hours[0], "end": c.budget.quiet_hours[1]}
    if c.budget.timezone:
        budget["timezone"] = c.budget.timezone
    return {
        "dedupe": c.dedupe,
        "ignorePaths": list(c.ignore_paths),
        "bypass": [rule(r) for r in c.bypass],
        "rules": [rule(r) for r in c.rules],
        "default": c.default_action,
        "cooldownSeconds": c.cooldown_seconds,
        "flushOnWake": c.flush_on_wake,
        "mode": c.mode,
        "canaryRate": c.canary_rate,
        "budget": budget,
    }
