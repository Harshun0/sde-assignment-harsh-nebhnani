"""Caller-ID health (C5): detect degradation, rotate weight away, explore safely back.

We never see the simulator's own health model — only our own rolling (dials, connected)
counts per caller_id. A DID is flagged when its pickup rate is significantly and
materially below the pool of the *other* DIDs on the same workflow. Once flagged we cut
its allocation to a small residual (never to zero — that's how we'd ever notice recovery)
and redistribute the rest proportionally. A flagged DID is periodically re-tested against
the pool; if the gap closes, we restore it.

We recompute the *desired* allocation from current health status every cycle and compare
it against the live config, rather than only acting on a healthy<->degraded transition —
a transition-only trigger would only ever try once, and if that one proposal expires or
gets rejected (ordinary governance noise), the bad DID would stay live allocation forever
with nothing to retry it.

Recovery is ramped, not all-or-nothing: the sim's own health model recovers load-based
penalties as a DID gets fewer dials (see docs/SIMULATOR.md's did_health), so restoring a
flagged DID straight to full weight the moment it looks better reloads it immediately and
re-triggers the flag. We ramp a "probe weight" up geometrically on each clean recheck
(classic circuit-breaker half-open pattern) instead of jumping straight back to uniform,
so a genuine recovery is confirmed at increasing traffic levels before we trust it.

Every read here is a *rolling-window* delta against a shared per-workflow checkpoint, not
the all-time cumulative (dials, connected) in did_stats. A DID with days of healthy history
before an incident would otherwise have its true recent rate diluted into a slow-moving
blended average — we saw this directly: rate crossed our significance threshold late and
erratically, and kept re-triggering the ramp because "enough new samples" was being judged
against the *lifetime* total, which is huge and satisfied instantly regardless of how stale
the window actually was. The checkpoint advances every cycle we act on, for every DID at
once, so all DIDs are always compared on the same recent slice of time.
"""
from __future__ import annotations

from . import configutil as cu
from .stats import two_proportion_z

MIN_SAMPLE = 150
ABS_DROP = 0.03   # the z-test (ALPHA below) carries the real rigor; this just filters trivial diffs
ALPHA = 0.01
RESIDUAL = 0.05
RECHECK_SAMPLE = 150


def evaluate(store, wid: str, wf_row: dict, minute: int, ts: str, changes: dict):
    rows = {r["caller_id"]: r for r in store.did_rows(wid)}
    cfg = wf_row["current_config"]
    # DID health is a property of the number pool, not of who's being called — unlike
    # welcome_message/voice we never want this to vary by cohort, so it's set everywhere.
    cohort_ids = ["default"] + [c["id"] for c in cfg.get("cohorts", [])]
    cur_alloc = cu.dist(cfg, "default", "caller_id")
    # only DIDs actually in play (nonzero weight) have any data to judge — a DID the business
    # never allocates to (e.g. held in reserve) would otherwise permanently block this check.
    options = [o for o in wf_row["spec"]["options"]["caller_id"] if cur_alloc.get(o, 0) > 0]
    if len(options) < 2 or any(o not in rows for o in options):
        return

    checkpoint = store.get_did_checkpoint(wid)
    delta = {}
    for did in options:
        snap_d, snap_c = checkpoint.get(did, [0, 0])
        delta[did] = (rows[did]["dials"] - snap_d, rows[did]["connected"] - snap_c)
    total_c = sum(c for _, c in delta.values())
    total_d = sum(d for d, _ in delta.values())
    if total_d < MIN_SAMPLE * len(options):
        return  # not enough new traffic since the last checkpoint to say anything

    probe_weights = {}
    for did in options:
        d_dials, d_conn = delta[did]
        rest_d, rest_c = total_d - d_dials, total_c - d_conn
        state = store.get_did_state(wid, did)
        status = state["status"] if state else "healthy"
        if d_dials < MIN_SAMPLE or rest_d < MIN_SAMPLE:
            if state and state.get("probe_weight"):
                probe_weights[did] = state["probe_weight"]
            continue
        rate, rest_rate = d_conn / d_dials, rest_c / rest_d
        _, p = two_proportion_z(d_conn, d_dials, rest_c, rest_d)
        degraded_now = p < ALPHA and rest_rate - rate >= ABS_DROP
        uniform_share = 1.0 / len(options)

        if status == "healthy" and degraded_now:
            store.set_did_state(wid, did, "degraded", minute, probe_weight=RESIDUAL)
            probe_weights[did] = RESIDUAL
            store.log_decision(minute, ts, wid, "caller_id_degraded",
                                f"{did}: recent-window pickup {rate:.3f} vs pool {rest_rate:.3f} "
                                f"(p={p:.4f}, n={d_dials}) — rotating down",
                                {"caller_id": did, "rate": rate, "pool_rate": rest_rate, "window_dials": d_dials})
        elif status in ("degraded", "recovering") and d_dials >= RECHECK_SAMPLE and degraded_now:
            store.set_did_state(wid, did, "degraded", minute, probe_weight=RESIDUAL)  # relapse — back to the floor
            probe_weights[did] = RESIDUAL
        elif status in ("degraded", "recovering") and d_dials >= RECHECK_SAMPLE and not degraded_now:
            cur_probe = (state.get("probe_weight") or RESIDUAL) if state else RESIDUAL
            next_probe = min(cur_probe * 2, uniform_share)
            if next_probe >= uniform_share - 1e-9:
                store.set_did_state(wid, did, "healthy", minute, probe_weight=None)
                store.log_decision(minute, ts, wid, "caller_id_recovered",
                                    f"{did}: recent-window pickup {rate:.3f} back in line with pool {rest_rate:.3f} "
                                    f"after ramped recheck — fully restoring", {"caller_id": did, "rate": rate})
            else:
                store.set_did_state(wid, did, "recovering", minute, probe_weight=next_probe)
                probe_weights[did] = next_probe
                store.log_decision(minute, ts, wid, "caller_id_ramping",
                                    f"{did}: clean recheck at weight {cur_probe:.3f}, raising probe to "
                                    f"{next_probe:.3f}", {"caller_id": did})
        elif status in ("degraded", "recovering"):
            probe_weights[did] = state.get("probe_weight") or RESIDUAL

    store.set_did_checkpoint(wid, {did: [rows[did]["dials"], rows[did]["connected"]] for did in options})

    degraded = {did for did in options
                if (store.get_did_state(wid, did) or {}).get("status") in ("degraded", "recovering")}
    desired = _desired_allocation(options, degraded, probe_weights)
    mismatched = [cid for cid in cohort_ids if not _close(desired, cu.dist(cfg, cid, "caller_id"))]
    if not mismatched:
        return  # live config already matches what health status calls for, in every cohort
    c = changes.setdefault(wid, {"summaries": [], "rationales": [], "impact": 0.0, "alloc": {}, "exp_keys": [],
                                  "force": False})
    for cid in cohort_ids:
        c["alloc"].setdefault(cid, {})["caller_id"] = desired
    c["summaries"].append("rotate caller_id allocation for health (all cohorts)")
    c["rationales"].append(f"Health status (degraded: {sorted(degraded) or 'none'}) doesn't match live "
                            f"allocation; re-proposing to bring it in line.")
    c["impact"] = max(c["impact"], 0.4)
    c["force"] = True


def _close(a: dict, b: dict, tol=1e-6) -> bool:
    keys = set(a) | set(b)
    return all(abs(a.get(k, 0) - b.get(k, 0)) < tol for k in keys)


def _desired_allocation(options, degraded, probe_weights):
    healthy = [o for o in options if o not in degraded]
    if not healthy:  # never strand every DID at residual — fall back to uniform
        return {o: 1.0 / len(options) for o in options}
    residual_total = sum(probe_weights.get(o, RESIDUAL) for o in degraded)
    share = (1.0 - residual_total) / len(healthy)
    out = {o: share for o in healthy}
    out.update({o: probe_weights.get(o, RESIDUAL) for o in degraded})
    return out
