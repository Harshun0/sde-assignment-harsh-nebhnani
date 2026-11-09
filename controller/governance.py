"""Governance client (C9): proposal lifecycle, auto-apply routing, batching, crash-safe idempotency.

Every proposal is written to the local store with status='creating' *before* any HTTP
call. On restart we never re-decide blindly: we first check the simulator's own record
(GET /approvals for the approval path, GET /workflows/{id}/config/history for auto-apply,
matched by our proposal_id) before retrying or giving up. That's what makes a kill/restart
safe to double-submit nothing (C13).
"""
from __future__ import annotations

import json
import uuid

from . import configutil as cu
from .client import SimError


def new_proposal_id() -> str:
    return f"prop_{uuid.uuid4().hex[:16]}"


class Governance:
    def __init__(self, store, client):
        self.store = store
        self.client = client

    def within_fatigue_budget(self, wid: str, minute: int, max_per_24h: int, margin: int = 1) -> bool:
        return self.store.recent_proposal_count(wid, minute - 1440) < max(max_per_24h - margin, 1)

    def route(self, wf_row: dict, old_cfg: dict, new_cfg: dict) -> str:
        gov = wf_row["spec"]["governance"]
        dims = cu.changed_dims(old_cfg, new_cfg)
        pol = gov["auto_apply"]
        if not pol.get("enabled") or not dims:
            return "approval"
        if any(d not in pol.get("allowed_dims", []) for d in dims):
            return "approval"
        if cu.max_shift(old_cfg, new_cfg) > pol.get("max_shift", 0) + 1e-9:
            return "approval"
        return "auto_apply"

    def propose(self, wid: str, base_version: int, new_cfg: dict, change_summary: str, rationale: str,
                affected_leads: int, dry_run: dict, impact_score: float, minute: int, ts: str,
                evidence: dict | None = None, meta: dict | None = None) -> str:
        """Creates and submits a proposal. Returns the local proposal_id."""
        wf_row = self.store.get_workflow(wid)
        old_cfg = wf_row["current_config"]
        kind = self.route(wf_row, old_cfg, new_cfg)
        diff = cu.diff_summary(old_cfg, new_cfg)
        pid = new_proposal_id()
        payload = {"proposal_id": pid, "workflow_id": wid, "base_version": base_version,
                   "proposed_config": new_cfg, "change_summary": change_summary, "diff": diff,
                   "affected_leads": affected_leads, "dry_run": dry_run, "impact_score": impact_score,
                   "rationale": rationale}
        if evidence:
            payload["evidence"] = evidence
        self.store.create_proposal(pid, wid, kind, base_version, payload, minute, meta=meta)
        self.store.log_decision(minute, ts, wid, "proposal_created",
                                 f"{kind} proposal {pid}: {change_summary}",
                                 {"payload": payload, "kind": kind})
        self._submit(pid, kind, payload, minute, ts)
        return pid

    def _submit(self, pid: str, kind: str, payload: dict, minute: int = 0, ts: str = ""):
        wid = payload["workflow_id"]
        try:
            if kind == "auto_apply":
                r = self.client.auto_apply(wid, payload)
                body = r.json()
                self.store.mark_proposal(pid, "auto_applied")
                self.store.bump_version(wid, body["version"], payload["proposed_config"], "controller_auto",
                                         body.get("changed_dims", []), minute)
                self.store.log_decision(minute, ts, wid, "auto_applied",
                                         f"auto-applied v{body['version']}", body)
            else:
                r = self.client.request_approval(payload)
                body = r.json()
                self.store.mark_proposal(pid, "submitted", approval_id=body["approval_id"])
        except SimError as e:
            self.store.mark_proposal(pid, "failed")
            self.store.log_decision(minute, ts, wid, "proposal_failed", f"{kind} failed: {e}", {"error": str(e)})
            p = self.store.get_proposal(pid)
            if p:
                self._revert_experiment_meta(p, minute, ts)

    # ------------------------------------------------------------------ reconciliation
    def reconcile_all(self, minute: int, ts: str):
        """Call at startup and periodically: resolve every proposal that isn't terminal."""
        for p in self.store.pending_proposals():
            self._reconcile_one(p, minute, ts)

    def _reconcile_one(self, p: dict, minute: int, ts: str):
        wid, pid, kind = p["workflow_id"], p["proposal_id"], p["kind"]
        if p["status"] == "creating":
            # Did it actually reach the simulator before we crashed?
            found = self._find_remote(wid, pid, kind)
            if found:
                self.store.mark_proposal(pid, found["status"], approval_id=found.get("approval_id"))
            else:
                self._submit(pid, kind, json.loads(p["payload_json"]), minute, ts)
            return
        if p["status"] in ("submitted", "approved") and p["approval_id"]:
            a = self.client.get_approval(p["approval_id"])
            if a["status"] == "approved":
                try:
                    r = self.client.apply_approval(wid, p["approval_id"])
                    body = r.json()
                    self.store.mark_proposal(pid, "applied")
                    proposed_cfg = json.loads(p["payload_json"])["proposed_config"]
                    self.store.bump_version(wid, body["version"], proposed_cfg, "controller_approved",
                                             body.get("changed_dims", []), minute)
                    self.store.log_decision(minute, ts, wid, "approval_applied",
                                             f"applied approval {p['approval_id']} -> v{body['version']}", body)
                except SimError as e:
                    if e.status == 409:
                        self.store.mark_proposal(pid, "stale")
                        self.store.log_decision(minute, ts, wid, "apply_conflict",
                                                 "base changed since approval (likely a supervisor edit); "
                                                 "will re-propose with fresh data", {"error": str(e)})
                        self._revert_experiment_meta(p, minute, ts)
                    elif e.status == 410:
                        self.store.mark_proposal(pid, "lapsed")
                        self.store.log_decision(minute, ts, wid, "approval_lapsed",
                                                 "approved change lapsed before we applied it", {})
                        self._revert_experiment_meta(p, minute, ts)
            elif a["status"] in ("rejected", "expired"):
                self.store.mark_proposal(pid, a["status"])
                self.store.log_decision(minute, ts, wid, f"approval_{a['status']}",
                                         f"approval {p['approval_id']} {a['status']}: {a.get('reasons')}", a)
                self._revert_experiment_meta(p, minute, ts)

    def _revert_experiment_meta(self, p: dict, minute: int, ts: str):
        """A promotion that never actually landed must not be monitored as if it had (C7's decay
        check otherwise compares live traffic it never got against a 'promoted' baseline)."""
        meta = json.loads(p.get("meta_json") or "{}")
        for wid, cohort_id, dim in meta.get("exp_keys", []):
            self.store.set_experiment_state(wid, cohort_id, dim, "exploring")
            self.store.log_decision(minute, ts, wid, "experiment_reverted",
                                     f"{dim}/{cohort_id}: promotion did not land — back to exploring", {})

    def _find_remote(self, wid: str, pid: str, kind: str) -> dict | None:
        if kind == "approval":
            for a in self.client.list_approvals(workflow_id=wid):
                if a["proposal_id"] == pid:
                    return {"status": "submitted", "approval_id": a["approval_id"]}
            return None
        hist = self.client.config_history(wid)
        for v in hist:
            if v.get("proposal_id") == pid:
                return {"status": "auto_applied"}
        return None
