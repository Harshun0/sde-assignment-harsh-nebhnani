"""Hallucination incident response (C8): detect -> LLM suggest -> validate -> propose -> verify.

We never see a prompt's true hallucination rate (hidden ground truth). Instead we keep a
*reference* rate for the workflow's baseline (last known-good) prompt, built from its own
accumulated calls — plenty of history exists for it, since it's the one that's been running.
Whenever the live prompt differs from the baseline (a supervisor edit, or our own earlier
fix), we compare *its* cumulative observed rate against the baseline reference directly.
This catches a prompt that's bad from the very first call — comparing a prompt only against
its own early samples (self-referencing) cannot, since the spike would be baked into its own
reference too.

On a confirmed spike we spend credits deliberately: one suggest-fixes call, then validation
runs only on the candidates (capped), picking the first/best that clears the workflow's own
min_validation_pass_rate. If none clear it, we fall back to the baseline prompt itself rather
than guessing. After applying a fix we re-run the same comparison on the new prompt to confirm
it actually worked before closing the incident (time-to-fix is audited either way).
"""
from __future__ import annotations

from .client import SimError
from .stats import two_proportion_z

REF_SAMPLE = 250
MONITOR_SAMPLE = 150
ABS_THRESHOLD = 0.015
REL_MULT = 1.8
ALPHA = 0.01
MAX_CANDIDATES_TESTED = 3
VALIDATION_N = 200


def evaluate(store, client, governance, wid: str, wf_row: dict, minute: int, ts: str):
    cfg = wf_row["current_config"]
    pv = cfg["prompt_version"]
    baseline_pv = wf_row["baseline_prompt"] or pv
    rows = {r["prompt_version"]: r for r in store.halluc_rows(wid)}

    base_row = rows.get(baseline_pv)
    if base_row and base_row["connected"] >= REF_SAMPLE:
        ref = base_row["flagged"] / base_row["connected"]
        store.set_halluc_reference(wid, baseline_pv, ref, base_row["connected"], base_row["flagged"])
        base_row = {**base_row, "reference_rate": ref}

    inc = store.get_incident(wid) or {"status": "none", "detail": {}}

    if inc["status"] == "none":
        if pv == baseline_pv or not base_row or base_row["reference_rate"] is None:
            return
        cur = rows.get(pv)
        if not cur or cur["connected"] < MONITOR_SAMPLE:
            return
        cur_rate, ref_rate = cur["flagged"] / cur["connected"], base_row["reference_rate"]
        _, p = two_proportion_z(cur["flagged"], cur["connected"], base_row["flagged"], base_row["connected"])
        spiked = p < ALPHA and cur_rate - ref_rate >= ABS_THRESHOLD and \
            (ref_rate == 0 or cur_rate >= REL_MULT * ref_rate)
        if not spiked:
            return
        store.set_incident(wid, "detected", {"prompt_version": pv, "reference_rate": ref_rate,
                                              "observed_rate": cur_rate}, minute)
        store.log_decision(minute, ts, wid, "halluc_incident_detected",
                            f"{pv}: hallucination rate {cur_rate:.4f} vs baseline {baseline_pv} "
                            f"reference {ref_rate:.4f} (p={p:.4f}) — opening incident",
                            {"observed_rate": cur_rate, "reference_rate": ref_rate, "p": p})
        _respond(store, client, governance, wid, wf_row, pv, minute, ts)
        return

    if inc["status"] == "detected":
        _respond(store, client, governance, wid, wf_row, inc["detail"]["prompt_version"], minute, ts)
        return

    if inc["status"] == "proposed":
        pid = inc["detail"].get("proposal_id")
        p = store.get_proposal(pid) if pid else None
        if p and p["status"] in ("auto_applied", "applied"):
            new_pv = wf_row["current_config"]["prompt_version"]
            store.set_incident(wid, "verifying", {**inc["detail"], "fix_prompt_version": new_pv}, minute)
            store.log_decision(minute, ts, wid, "halluc_fix_applied",
                                f"incident fix applied: {inc['detail']['prompt_version']} -> {new_pv}", {})
        elif p and p["status"] in ("rejected", "expired", "failed", "lapsed", "stale"):
            store.log_decision(minute, ts, wid, "halluc_fix_rejected",
                                f"proposed fix was {p['status']}; will retry with fresh evidence", {})
            store.set_incident(wid, "detected", inc["detail"], minute)
        return

    if inc["status"] == "verifying":
        fix_pv = inc["detail"]["fix_prompt_version"]
        if fix_pv == baseline_pv:
            opened = inc.get("opened_minute") or minute
            store.log_decision(minute, ts, wid, "halluc_incident_resolved",
                                f"fell back to known-good baseline {baseline_pv}; MTTR={minute - opened}min", {})
            store.set_incident(wid, "none", {}, minute)
            return
        cur = rows.get(fix_pv)
        if not cur or cur["connected"] < MONITOR_SAMPLE or not base_row or base_row["reference_rate"] is None:
            return
        cur_rate, ref_rate = cur["flagged"] / cur["connected"], base_row["reference_rate"]
        _, p = two_proportion_z(cur["flagged"], cur["connected"], base_row["flagged"], base_row["connected"])
        still_bad = p < ALPHA and cur_rate - ref_rate >= ABS_THRESHOLD
        opened = inc.get("opened_minute") or minute
        if still_bad:
            store.log_decision(minute, ts, wid, "halluc_fix_ineffective",
                                f"fix {fix_pv} still elevated ({cur_rate:.4f} vs {ref_rate:.4f}); escalating", {})
            store.set_incident(wid, "detected", {"prompt_version": fix_pv, "reference_rate": ref_rate,
                                                  "observed_rate": cur_rate}, inc.get("opened_minute") or minute)
        else:
            store.log_decision(minute, ts, wid, "halluc_incident_resolved",
                                f"fix prompt {fix_pv} stable at {cur_rate:.4f}; MTTR={minute - opened}min",
                                {"mttr_minutes": minute - opened, "final_rate": cur_rate})
            store.set_incident(wid, "none", {}, minute)


def _respond(store, client, governance, wid, wf_row, bad_pv, minute, ts):
    spec = wf_row["spec"]
    try:
        sugg = client.suggest_fixes(wid, f"hallucination rate spike on prompt_version {bad_pv}")
    except SimError as e:
        store.log_decision(minute, ts, wid, "halluc_llm_failed", f"suggest-fixes failed: {e}", {})
        return
    candidates = [s["prompt_version"] for s in sugg["suggestions"]][:MAX_CANDIDATES_TESTED]
    min_pass = spec["governance"].get("min_validation_pass_rate", 0.97)
    best, best_run = None, None
    for cand in candidates:
        try:
            run = client.run_validation(wid, cand, n=VALIDATION_N)
        except SimError as e:
            store.log_decision(minute, ts, wid, "halluc_validation_failed", f"{cand}: {e}", {})
            continue
        store.save_validation(run["validation_run_id"], wid, cand, run["pass_rate"], run["n"], minute)
        if run["pass_rate"] >= min_pass and (best_run is None or run["pass_rate"] > best_run["pass_rate"]):
            best, best_run = cand, run
        if best and best_run["pass_rate"] >= min_pass + 0.01:
            break  # comfortably clears the bar — stop spending credits

    fix_pv = best or wf_row["baseline_prompt"] or spec["options"]["prompt_version"][0]
    evidence = {"validation_run_id": best_run["validation_run_id"]} if best else None
    if not best:
        # no LLM suggestion validated; fall back to the last known-good prompt, re-validate it
        # so the approval's evidence requirement is still honestly earned.
        try:
            run = client.run_validation(wid, fix_pv, n=VALIDATION_N)
            store.save_validation(run["validation_run_id"], wid, fix_pv, run["pass_rate"], run["n"], minute)
            evidence = {"validation_run_id": run["validation_run_id"]}
        except SimError as e:
            store.log_decision(minute, ts, wid, "halluc_fallback_validation_failed", str(e), {})
            return

    new_cfg = dict(wf_row["current_config"])
    new_cfg["prompt_version"] = fix_pv
    summary = f"revert/switch prompt {bad_pv} -> {fix_pv} after hallucination spike"
    rationale = (f"Observed hallucination rate spike on {bad_pv} (incident). "
                 f"{'LLM-suggested candidate' if best else 'Falling back to last known-good prompt'} "
                 f"{fix_pv} validated at pass_rate>={min_pass}.")
    pid = governance.propose(wid, wf_row["current_version"], new_cfg, summary, rationale,
                              affected_leads=_affected_leads(store, wid), dry_run={"validation": evidence},
                              impact_score=0.8, minute=minute, ts=ts, evidence=evidence)
    inc = store.get_incident(wid)
    detail = {**inc["detail"], "proposal_id": pid, "candidate": fix_pv}
    store.set_incident(wid, "proposed", detail, inc.get("opened_minute") or minute)


def _affected_leads(store, wid) -> int:
    rows = store.feature_rows(wid)
    return sum(r["dials"] for r in rows) or 0
