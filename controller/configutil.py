"""Config diff/shift helpers, independent of `sim` (we only ever see configs via HTTP).

These mirror the *public* math in docs/SIMULATOR.md (total-variation shift, changed
dimensions) so we can decide approval-vs-auto-apply routing and write an honest diff
without an extra round trip. Nothing here depends on hidden scenario fields.
"""
from __future__ import annotations

import copy

AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    d = (cfg.get("allocations", {}).get(cohort) or {}).get(dim)
    if d is not None:
        return d
    return cfg["allocations"]["default"].get(dim, {})


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = []
    if old.get("cohorts") != new.get("cohorts"):
        ch.append("cohorts")
    keys = set(old.get("allocations", {})) | set(new.get("allocations", {}))
    for dim in AGENT_DIMS:
        if any(dist(old, k, dim) != dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    if old.get("schedule") != new.get("schedule"):
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old.get("cohorts", [])} | {c["id"] for c in new.get("cohorts", [])}
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = dist(old, k, dim), dist(new, k, dim)
            tv = 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))
            worst = max(worst, tv)
    return worst


def diff_summary(old: dict, new: dict) -> dict:
    """Free-form diff for the approval request: just the dims that moved, before/after."""
    out = {}
    if old.get("cohorts") != new.get("cohorts"):
        out["cohorts"] = {"before": old.get("cohorts"), "after": new.get("cohorts")}
    keys = set(old.get("allocations", {})) | set(new.get("allocations", {}))
    for dim in AGENT_DIMS:
        moved = {k: {"before": dist(old, k, dim), "after": dist(new, k, dim)}
                 for k in keys if dist(old, k, dim) != dist(new, k, dim)}
        if moved:
            out[dim] = moved
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"before": old.get("prompt_version"), "after": new.get("prompt_version")}
    if old.get("schedule") != new.get("schedule"):
        out["schedule"] = {"before": old.get("schedule"), "after": new.get("schedule")}
    return out


def clone(cfg: dict) -> dict:
    return copy.deepcopy(cfg)
