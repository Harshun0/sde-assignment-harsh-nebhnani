"""Variant experimentation on welcome_message / voice (C6), with decay/rollback (C7) folded in.

One experiment per (workflow, cohort, dim). Exploration starts uniform across the
workflow's allowed options for that dim. Once every arm has enough connected calls,
we rank by Wilson lower bound and test the leader against each other arm with a
pooled two-proportion z-test, Holm-Bonferroni corrected across the k-1 comparisons.
A promotion also re-checks the workflow's guardrail KPI before it's allowed through.

After promotion we keep accumulating stats for the winning arm. If a later window
drops significantly below the arm's own promotion-time rate, we treat it as decay
and propose reverting to the pre-promotion allocation, reopening exploration — this
is the "C7 folded into C6" piece: no separate machinery, same stats engine.
"""
from __future__ import annotations

from . import configutil as cu
from .stats import holm_bonferroni, two_proportion_z, wilson_lower_bound

DIMS = ["welcome_message", "voice"]
MIN_ARM_SAMPLE = 200        # connected (or dials, for a pickup-optimising workflow) per arm before testing
MIN_PROMOTE_EFFECT = 0.02   # min absolute rate improvement to bother promoting
MIN_MONITOR_SAMPLE = 200    # post-promotion sample before we'll call decay
DECAY_EFFECT = 0.02
ALPHA = 0.05
GUARDRAIL_ALPHA = 0.05


def _metric(objective: dict):
    """Returns (numerator_field, denominator_field) for ranking, and the guardrail pair."""
    if objective["primary"] == "pickup_rate":
        return ("connected", "dials"), ("engaged", "connected"), objective["guardrail"].get("engagement_rate_max_drop", 0.02)
    return ("engaged", "connected"), ("connected", "dials"), objective["guardrail"].get("pickup_rate_max_drop", 0.02)


def evaluate(store, client, wid: str, cohort_id: str, dim: str, wf_row: dict, minute: int, ts: str,
             changes: dict):
    """May add a pending config change for `wid` into `changes` (mutated in place)."""
    spec = wf_row["spec"]
    options = spec["options"][dim]
    if len(options) < 2:
        return
    rows = {r["option"]: r for r in store.exp_rows(wid, cohort_id=cohort_id, dim=dim)}
    if not rows:
        return
    (num_f, den_f), (g_num_f, g_den_f), guard_drop = _metric(spec["objective"])
    st = store.get_experiment_state(wid, cohort_id, dim)

    if st and st["status"] == "promoted":
        _check_decay(store, client, wid, cohort_id, dim, wf_row, rows, st, num_f, den_f, minute, ts, changes)
        return

    missing = [o for o in options if o not in rows]
    if missing or any(rows[o][den_f] < MIN_ARM_SAMPLE for o in options):
        _ensure_exploring(store, wid, cohort_id, dim, wf_row, changes)
        return

    ranked = sorted(options, key=lambda o: wilson_lower_bound(rows[o][num_f], rows[o][den_f]), reverse=True)
    leader = ranked[0]
    challengers = ranked[1:]
    pvals = [two_proportion_z(rows[leader][num_f], rows[leader][den_f],
                               rows[o][num_f], rows[o][den_f])[1] for o in challengers]
    sig = holm_bonferroni(pvals, ALPHA)
    leader_rate = rows[leader][num_f] / rows[leader][den_f]
    beats_all = all(sig) and all(leader_rate - rows[o][num_f] / rows[o][den_f] >= MIN_PROMOTE_EFFECT
                                  for o in challengers)
    if not beats_all:
        return  # inconclusive — keep exploring uniformly, collect more evidence

    guard_rate = rows[leader][g_num_f] / rows[leader][g_den_f] if rows[leader][g_den_f] else None
    current_best_guard = max((rows[o][g_num_f] / rows[o][g_den_f] for o in options if rows[o][g_den_f]),
                              default=None)
    if guard_rate is not None and current_best_guard is not None and current_best_guard - guard_rate > guard_drop:
        last_veto = (st or {}).get("last_veto_minute")
        if last_veto is None or minute - last_veto >= 1440:  # log at most once/day per cohort+dim
            store.log_decision(minute, ts, wid, "promotion_vetoed",
                                f"{dim}/{cohort_id}: {leader} wins on primary but would breach guardrail "
                                f"({guard_rate:.3f} vs {current_best_guard:.3f}, drop>{guard_drop})",
                                {"leader": leader, "guard_rate": guard_rate})
        store.note_veto(wid, cohort_id, dim, minute)
        return

    cfg = wf_row["current_config"]
    prior_alloc = cu.dist(cfg, cohort_id if cohort_id != "default" else "default", dim)
    snapshot = {"dials": rows[leader]["dials"], "connected": rows[leader]["connected"],
                "engaged": rows[leader]["engaged"]}
    store.set_experiment_state(wid, cohort_id, dim, "promoted", winner=leader, minute=minute,
                                rate=leader_rate, snapshot=snapshot, prior_alloc=prior_alloc)
    store.log_decision(minute, ts, wid, "experiment_promoted",
                        f"{dim}/{cohort_id}: promoting {leader} (rate={leader_rate:.3f}), "
                        f"beat {len(challengers)} challenger(s) at alpha={ALPHA}",
                        {"leader": leader, "rows": {o: dict(r) for o, r in rows.items()}})
    _queue_allocation(changes, wid, cohort_id, dim, {leader: 1.0},
                       f"{dim} winner for {cohort_id}: {leader}",
                       f"Significantly beat {len(challengers)} alternative(s) (Holm-corrected, alpha={ALPHA}) "
                       f"by >= {MIN_PROMOTE_EFFECT:.0%} on {spec['objective']['primary']}, "
                       f"guardrail respected.",
                       impact=min(0.3 + leader_rate - (sum(rows[o][num_f] for o in options) /
                                                        max(sum(rows[o][den_f] for o in options), 1)), 0.9),
                       exp_key=(wid, cohort_id, dim))


def _ensure_exploring(store, wid, cohort_id, dim, wf_row, changes):
    st = store.get_experiment_state(wid, cohort_id, dim)
    if st and st["status"] == "exploring":
        return  # already uniform; nothing to propose
    options = wf_row["spec"]["options"][dim]
    cfg = wf_row["current_config"]
    cur = cu.dist(cfg, cohort_id if cohort_id != "default" else "default", dim)
    uniform = {o: 1.0 / len(options) for o in options}
    if cur == uniform:
        store.set_experiment_state(wid, cohort_id, dim, "exploring")
        return
    store.set_experiment_state(wid, cohort_id, dim, "exploring")
    _queue_allocation(changes, wid, cohort_id, dim, uniform,
                       f"start exploring {dim} for {cohort_id}",
                       f"No arm has {MIN_ARM_SAMPLE} samples yet; split traffic uniformly across "
                       f"{len(options)} options to gather evidence.", impact=0.1)


def _check_decay(store, client, wid, cohort_id, dim, wf_row, rows, st, num_f, den_f, minute, ts, changes):
    winner = st["winner"]
    if winner not in rows:
        return
    snap = st["snapshot"]
    cur = rows[winner]
    post_num = cur[num_f] - snap.get(num_f, 0)
    post_den = cur[den_f] - snap.get(den_f, 0)
    if post_den < MIN_MONITOR_SAMPLE:
        return
    pre_num, pre_den = snap.get(num_f, 0), snap.get(den_f, 0)
    if pre_den == 0:
        return
    z, p = two_proportion_z(post_num, post_den, pre_num, pre_den)
    post_rate, pre_rate = post_num / post_den, pre_num / pre_den
    if p < 0.01 and pre_rate - post_rate >= DECAY_EFFECT:
        store.log_decision(minute, ts, wid, "decay_detected",
                            f"{dim}/{cohort_id}: winner {winner} decayed {pre_rate:.3f} -> {post_rate:.3f}, "
                            f"rolling back", {"pre_rate": pre_rate, "post_rate": post_rate, "p": p})
        _queue_allocation(changes, wid, cohort_id, dim, st["prior_alloc"],
                           f"roll back {dim} for {cohort_id}: {winner} decayed",
                           f"Post-promotion rate fell from {pre_rate:.3f} to {post_rate:.3f} "
                           f"(p={p:.4f}); reverting to pre-promotion allocation and reopening exploration.",
                           impact=0.5, force=True)
        store.set_experiment_state(wid, cohort_id, dim, "exploring")


def _queue_allocation(changes: dict, wid, cohort_id, dim, weights, summary, rationale, impact, exp_key=None,
                       force=False):
    c = changes.setdefault(wid, {"summaries": [], "rationales": [], "impact": 0.0, "alloc": {}, "exp_keys": [],
                                  "force": False})
    c["alloc"].setdefault(cohort_id, {})[dim] = weights
    c["summaries"].append(summary)
    c["rationales"].append(rationale)
    c["impact"] = max(c["impact"], impact)
    c["force"] = c["force"] or force
    if exp_key:
        c["exp_keys"].append(list(exp_key))
