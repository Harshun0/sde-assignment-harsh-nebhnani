"""The controller's main loop. See SUBMISSION.md for design rationale.

Usage:
    python -m controller.main <SIM_URL> [--db PATH] [--advance-minutes N]

Crash safety: every piece of durable state lives in the SQLite file at --db. On
startup we check the simulator's own scenario name against what we last saw; a
mismatch means a brand-new world (not a restart of the same one), and we start
fresh rather than replay stale decisions into it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import callerid, cohorts, configutil as cu, experiments, hallucination
from .client import SimClient, SimError
from .governance import Governance
from .ingest import Ingestor
from .store import Store

DIMS = experiments.DIMS


def bootstrap(store: Store, client: SimClient):
    for w in client.workflows():
        wid = w["workflow_id"]
        cfg = client.get_config(wid)
        if store.get_workflow(wid) is None:
            store.upsert_workflow(wid, w["tenant_id"], w, cfg["version"], cfg["config"])
            store.set_baseline_prompt(wid, cfg["config"]["prompt_version"])
        else:
            store.upsert_workflow(wid, w["tenant_id"], w, cfg["version"], cfg["config"])


def maybe_introduce_cohorts(store, wid, wf_row, minute, ts, governance):
    frozen = store.get_cohorts(wid)
    cfg = wf_row["current_config"]
    live_ids = {c["id"] for c in cfg.get("cohorts", [])}
    if not frozen:
        rows = cohorts.build_cohorts(store.feature_rows(wid), minute)
        if rows is None:
            return
        store.set_cohorts(wid, rows, minute)
        frozen = rows
        store.log_decision(minute, ts, wid, "cohorts_frozen",
                            f"froze {len(rows)} cohort(s) on feature {rows[0]['feature']}", {"cohorts": rows})
    missing = [c for c in frozen if c["id"] not in live_ids]
    if not missing:
        return
    new_cfg = cu.clone(cfg)
    new_cfg["cohorts"] = list(cfg.get("cohorts", [])) + [
        {"id": c["id"], "where": cohorts.cohort_where_clause(c["feature"], c["value"])} for c in missing]
    for dim in ("welcome_message", "voice", "caller_id"):
        default_dist = cu.dist(cfg, "default", dim)
        for c in missing:
            new_cfg["allocations"].setdefault(c["id"], {})[dim] = dict(default_dist)
    governance.propose(
        wid, wf_row["current_version"], new_cfg,
        f"introduce cohort(s) {[c['id'] for c in missing]}",
        "Significance screen found a feature split with a strong, well-powered effect "
        "(see decisions log 'cohorts_frozen'); introducing it as a cohort so experiments "
        "and detection can target it specifically. Allocations copied from default, so this "
        "changes targeting granularity, not current behaviour.",
        affected_leads=wf_row["spec"]["daily_batch"]["size"], dry_run={"cohorts": missing}, impact_score=0.2,
        minute=minute, ts=ts)


def run_workflow_decisions(store, client, governance, wid, minute, ts):
    wf_row = store.get_workflow(wid)
    if wf_row is None:
        return
    # safety-critical, independent lane: always ticks, even while a batched change is pending
    hallucination.evaluate(store, client, governance, wid, wf_row, minute, ts)
    if store.pending_proposals(wid):
        return  # one outstanding batched change at a time per workflow keeps approver fatigue honest

    maybe_introduce_cohorts(store, wid, wf_row, minute, ts, governance)
    if store.pending_proposals(wid):
        return
    wf_row = store.get_workflow(wid)

    gov_cfg = wf_row["spec"]["governance"]
    changes: dict = {}
    cfg = wf_row["current_config"]
    cohort_ids = ["default"] + [c["id"] for c in cfg.get("cohorts", [])]
    for dim in DIMS:
        for cid in cohort_ids:
            experiments.evaluate(store, client, wid, cid, dim, wf_row, minute, ts, changes)
    callerid.evaluate(store, wid, wf_row, minute, ts, changes)
    hallucination.evaluate(store, client, governance, wid, wf_row, minute, ts)

    c = changes.get(wid)
    if not c:
        return
    if not c.get("force") and not governance.within_fatigue_budget(wid, minute, gov_cfg["max_requests_per_24h"]):
        store.log_decision(minute, ts, wid, "change_deferred_fatigue",
                            f"deferring non-urgent change ({'; '.join(c['summaries'])}) to respect "
                            f"approver fatigue budget", {})
        return
    new_cfg = cu.clone(cfg)
    for cid, dims in c["alloc"].items():
        for dim, weights in dims.items():
            new_cfg["allocations"].setdefault(cid, {})[dim] = weights
    governance.propose(wid, wf_row["current_version"], new_cfg, "; ".join(c["summaries"]),
                        " | ".join(c["rationales"]), affected_leads=wf_row["spec"]["daily_batch"]["size"],
                        dry_run={"proposed_allocation_changes": c["alloc"], "basis": c["summaries"]},
                        impact_score=round(min(c["impact"], 1.0), 3), minute=minute, ts=ts,
                        meta={"exp_keys": c.get("exp_keys", [])})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "controller_state.db"))
    # Must stay well under the smallest governance apply_window_hours we might see (2h in every
    # public scenario) — otherwise an approval can be decided *and* lapse inside one advance() call,
    # with no reconcile checkpoint in between to catch it. 30min gives at least one checkpoint per hour.
    ap.add_argument("--advance-minutes", type=int, default=int(os.environ.get("ADVANCE_MINUTES", "30")))
    a = ap.parse_args(argv)

    client = SimClient(a.url)
    for _ in range(60):
        try:
            client.health()
            break
        except Exception:
            time.sleep(2)

    store = Store(a.db)
    scenario = client.sim_state()["scenario"]
    prev_scenario = store.get_meta("scenario")
    if prev_scenario is not None and prev_scenario != scenario:
        print(f"[controller] scenario changed ({prev_scenario} -> {scenario}); starting fresh state", file=sys.stderr)
        store.close()
        os.remove(a.db)
        for ext in ("-wal", "-shm"):
            p = a.db + ext
            if os.path.exists(p):
                os.remove(p)
        store = Store(a.db)
    store.set_meta("scenario", scenario)

    bootstrap(store, client)
    ingestor = Ingestor(store, client)
    governance = Governance(store, client)

    st = client.sim_state()
    minute, ts = st["sim_minute"], st["ts"]
    ingestor.catch_up("history", minute)
    governance.reconcile_all(minute, ts)

    while True:
        st = client.sim_state()
        if st["finished"]:
            break
        governance.reconcile_all(st["sim_minute"], st["ts"])
        try:
            adv = client.advance(a.advance_minutes)
        except SimError as e:
            if e.status == 409:
                break
            raise
        minute, ts = adv["sim_minute"], adv["ts"]
        ingestor.catch_up("events", minute)
        for wid in store.all_workflow_ids():
            run_workflow_decisions(store, client, governance, wid, minute, ts)
        governance.reconcile_all(minute, ts)
        print(f"[controller] {ts} chunks={len(adv['new_chunks'])} "
              f"credits={client.sim_state()['credits_remaining']}", flush=True)
        if adv["finished"]:
            break

    governance.reconcile_all(minute, ts)
    print("[controller] done:", client.sim_state())
    store.close()


if __name__ == "__main__":
    main()
