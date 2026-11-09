"""SQLite-backed state: the controller's only durable memory.

Every write that matters for crash-safety happens inside one transaction:
ingestion cursor + aggregate deltas commit together, and a proposal's local
row is written *before* the HTTP call that creates it on the simulator side,
so a crash can never leave us unsure whether a proposal was sent.

This file is the audit store (C12): `decisions` is the append-only log that
answers "why did workflow X change at T".
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS cursor (
    kind TEXT PRIMARY KEY,       -- 'history' | 'events'
    last_chunk INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow (
    workflow_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    spec_json TEXT NOT NULL,     -- GET /workflows/{id} response, cached
    current_version INTEGER NOT NULL DEFAULT 1,
    current_config_json TEXT,
    baseline_prompt TEXT
);

-- single-feature screen used to build the shared cohort object (C2)
CREATE TABLE IF NOT EXISTS feature_stats (
    workflow_id TEXT, feature TEXT, value TEXT,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, feature, value)
);

-- the shared cohort object: same rows read by detection, experiments, and
-- (if we had one) the supervisor view.
CREATE TABLE IF NOT EXISTS cohort (
    workflow_id TEXT, cohort_id TEXT,
    feature TEXT, value TEXT,
    created_minute INTEGER,
    PRIMARY KEY (workflow_id, cohort_id)
);

-- experiment arm performance per (workflow, cohort, dimension, option)
CREATE TABLE IF NOT EXISTS exp_stats (
    workflow_id TEXT, cohort_id TEXT, dim TEXT, option TEXT,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    engaged INTEGER NOT NULL DEFAULT 0,
    first_seen_minute INTEGER, last_seen_minute INTEGER,
    PRIMARY KEY (workflow_id, cohort_id, dim, option)
);

CREATE TABLE IF NOT EXISTS experiment_state (
    workflow_id TEXT, cohort_id TEXT, dim TEXT,
    status TEXT NOT NULL,              -- exploring | promoted
    winner TEXT,
    promoted_at_minute INTEGER,
    promoted_rate REAL,
    snapshot_json TEXT,                -- winner arm's cumulative (dials,connected,engaged) at promotion
    prior_alloc_json TEXT,             -- this cohort's allocation for `dim` just before promotion (for rollback)
    last_veto_minute INTEGER,          -- rate-limits repeat 'guardrail would be breached' log spam
    PRIMARY KEY (workflow_id, cohort_id, dim)
);

-- caller-id rolling health
CREATE TABLE IF NOT EXISTS did_stats (
    workflow_id TEXT, caller_id TEXT,
    dials INTEGER NOT NULL DEFAULT 0,
    connected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, caller_id)
);
CREATE TABLE IF NOT EXISTS did_checkpoint (
    workflow_id TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL        -- {caller_id: [dials, connected]} cumulative totals as of last checkpoint
);
CREATE TABLE IF NOT EXISTS did_state (
    workflow_id TEXT, caller_id TEXT,
    status TEXT NOT NULL,              -- healthy | degraded | recovering
    flagged_at_minute INTEGER,
    probe_weight REAL,                 -- current ramp-up weight while degraded/recovering (circuit-breaker)
    PRIMARY KEY (workflow_id, caller_id)
);

-- hallucination rolling stats, keyed by the prompt_version that was live
CREATE TABLE IF NOT EXISTS halluc_stats (
    workflow_id TEXT, prompt_version TEXT,
    connected INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    reference_rate REAL,               -- established once, early in this prompt's life
    snap_connected INTEGER, snap_flagged INTEGER,
    PRIMARY KEY (workflow_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS incident_state (
    workflow_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- none | detected | proposed | verifying
    detail_json TEXT,
    opened_minute INTEGER
);

-- proposals: written before the HTTP call that submits them (idempotency)
CREATE TABLE IF NOT EXISTS proposal (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT, kind TEXT,        -- approval | auto_apply
    base_version INTEGER,
    payload_json TEXT,
    status TEXT NOT NULL,               -- creating | submitted | approved | rejected | expired | lapsed | applied | auto_applied | failed
    approval_id TEXT,
    created_minute INTEGER,
    updated_minute INTEGER,
    meta_json TEXT                      -- local-only bookkeeping (e.g. which experiment_state keys this touches)
);

CREATE TABLE IF NOT EXISTS applied_version (
    workflow_id TEXT, version INTEGER,
    actor TEXT, dims_json TEXT, applied_minute INTEGER,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_minute INTEGER, ts TEXT,
    workflow_id TEXT, kind TEXT,
    summary TEXT, detail_json TEXT
);

CREATE TABLE IF NOT EXISTS lead_features (
    lead_id TEXT PRIMARY KEY, workflow_id TEXT,
    lang TEXT, city_tier TEXT, age_band TEXT, value_band TEXT
);

CREATE TABLE IF NOT EXISTS validation_run (
    run_id TEXT PRIMARY KEY, workflow_id TEXT, prompt_version TEXT,
    pass_rate REAL, n INTEGER, created_minute INTEGER
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # ---- meta / cursor ----------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value)))

    def last_chunk(self, kind: str) -> int:
        row = self.db.execute("SELECT last_chunk FROM cursor WHERE kind=?", (kind,)).fetchone()
        return row["last_chunk"] if row else -1

    # ---- workflow cache -----------------------------------------------------
    def upsert_workflow(self, wid, tenant_id, spec, version, config):
        self.db.execute(
            "INSERT INTO workflow(workflow_id,tenant_id,spec_json,current_version,current_config_json) "
            "VALUES (?,?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET "
            "spec_json=excluded.spec_json, current_version=excluded.current_version, "
            "current_config_json=excluded.current_config_json",
            (wid, tenant_id, json.dumps(spec), version, json.dumps(config)))

    def bump_version(self, wid, version, config, actor, dims, minute):
        self.db.execute("BEGIN")
        try:
            self.db.execute("INSERT INTO applied_version(workflow_id,version,actor,dims_json,applied_minute) "
                             "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                             (wid, version, actor, json.dumps(dims), minute))
            self.db.execute("UPDATE workflow SET current_version=?, current_config_json=? "
                             "WHERE workflow_id=? AND current_version<?", (version, json.dumps(config), wid, version))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def set_baseline_prompt(self, wid, prompt_version):
        self.db.execute("UPDATE workflow SET baseline_prompt=? WHERE workflow_id=?", (prompt_version, wid))

    def get_workflow(self, wid) -> dict | None:
        row = self.db.execute("SELECT * FROM workflow WHERE workflow_id=?", (wid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["spec"] = json.loads(d.pop("spec_json"))
        d["current_config"] = json.loads(d.pop("current_config_json"))
        return d

    def all_workflow_ids(self) -> list[str]:
        return [r["workflow_id"] for r in self.db.execute("SELECT workflow_id FROM workflow")]

    # ---- one atomic ingestion commit --------------------------------------
    def commit_chunk(self, kind: str, chunk_no: int, feature_deltas, exp_deltas, did_deltas,
                      halluc_deltas, config_changes, approval_decisions, lead_feature_rows=()):
        """All of this chunk's effects land in one transaction, or none do."""
        self.db.execute("BEGIN")
        try:
            for lid, wid, lang, ct, ab, vb in lead_feature_rows:
                self.db.execute("INSERT INTO lead_features(lead_id,workflow_id,lang,city_tier,age_band,"
                                 "value_band) VALUES (?,?,?,?,?,?) ON CONFLICT(lead_id) DO NOTHING",
                                 (lid, wid, lang, ct, ab, vb))
            for (wid, feat, val), (d, c, e) in feature_deltas.items():
                self.db.execute(
                    "INSERT INTO feature_stats(workflow_id,feature,value,dials,connected,engaged) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(workflow_id,feature,value) DO UPDATE SET "
                    "dials=dials+excluded.dials, connected=connected+excluded.connected, "
                    "engaged=engaged+excluded.engaged", (wid, feat, val, d, c, e))
            for (wid, coh, dim, opt), (d, c, e, minute) in exp_deltas.items():
                self.db.execute(
                    "INSERT INTO exp_stats(workflow_id,cohort_id,dim,option,dials,connected,engaged,"
                    "first_seen_minute,last_seen_minute) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(workflow_id,cohort_id,dim,option) DO UPDATE SET "
                    "dials=dials+excluded.dials, connected=connected+excluded.connected, "
                    "engaged=engaged+excluded.engaged, last_seen_minute=excluded.last_seen_minute",
                    (wid, coh, dim, opt, d, c, e, minute, minute))
            for (wid, did), (d, c) in did_deltas.items():
                self.db.execute(
                    "INSERT INTO did_stats(workflow_id,caller_id,dials,connected) VALUES (?,?,?,?) "
                    "ON CONFLICT(workflow_id,caller_id) DO UPDATE SET "
                    "dials=dials+excluded.dials, connected=connected+excluded.connected", (wid, did, d, c))
            for (wid, pv), (conn, flagged) in halluc_deltas.items():
                self.db.execute(
                    "INSERT INTO halluc_stats(workflow_id,prompt_version,connected,flagged) VALUES (?,?,?,?) "
                    "ON CONFLICT(workflow_id,prompt_version) DO UPDATE SET "
                    "connected=connected+excluded.connected, flagged=flagged+excluded.flagged",
                    (wid, pv, conn, flagged))
            for wid, version, actor, dims, minute, config in config_changes:
                self.db.execute(
                    "INSERT INTO applied_version(workflow_id,version,actor,dims_json,applied_minute) "
                    "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (wid, version, actor, json.dumps(dims), minute))
                self.db.execute("UPDATE workflow SET current_version=?, current_config_json=? "
                                 "WHERE workflow_id=? AND current_version<?",
                                 (version, json.dumps(config), wid, version))
            # NB: approval_decided events are informational only here — the Governance
            # reconciler is the sole writer of proposal.status, to avoid a race between
            # this event-driven update and its own poll-and-apply loop (see governance.py).
            self.db.execute("INSERT INTO cursor(kind,last_chunk) VALUES (?,?) "
                             "ON CONFLICT(kind) DO UPDATE SET last_chunk=excluded.last_chunk", (kind, chunk_no))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def load_lead_features(self) -> dict:
        out = {}
        for r in self.db.execute("SELECT lead_id, lang, city_tier, age_band, value_band FROM lead_features"):
            out[r["lead_id"]] = (r["lang"], r["city_tier"], r["age_band"], r["value_band"])
        return out

    # ---- reads for detection/experiment modules ----------------------------
    def feature_rows(self, wid) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM feature_stats WHERE workflow_id=?", (wid,)))

    def exp_rows(self, wid, cohort_id=None, dim=None) -> list[sqlite3.Row]:
        q, p = "SELECT * FROM exp_stats WHERE workflow_id=?", [wid]
        if cohort_id:
            q += " AND cohort_id=?"; p.append(cohort_id)
        if dim:
            q += " AND dim=?"; p.append(dim)
        return list(self.db.execute(q, p))

    def did_rows(self, wid) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM did_stats WHERE workflow_id=?", (wid,)))

    def get_did_checkpoint(self, wid) -> dict:
        row = self.db.execute("SELECT snapshot_json FROM did_checkpoint WHERE workflow_id=?", (wid,)).fetchone()
        return json.loads(row["snapshot_json"]) if row else {}

    def set_did_checkpoint(self, wid, snapshot: dict):
        self.db.execute("INSERT INTO did_checkpoint(workflow_id,snapshot_json) VALUES (?,?) "
                         "ON CONFLICT(workflow_id) DO UPDATE SET snapshot_json=excluded.snapshot_json",
                         (wid, json.dumps(snapshot)))

    def halluc_rows(self, wid) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM halluc_stats WHERE workflow_id=?", (wid,)))

    def set_halluc_reference(self, wid, pv, reference_rate, snap_connected, snap_flagged):
        self.db.execute("UPDATE halluc_stats SET reference_rate=?, snap_connected=?, snap_flagged=? "
                         "WHERE workflow_id=? AND prompt_version=?",
                         (reference_rate, snap_connected, snap_flagged, wid, pv))

    # ---- cohorts ------------------------------------------------------------
    def set_cohorts(self, wid, cohorts: list[dict], minute: int):
        self.db.execute("DELETE FROM cohort WHERE workflow_id=?", (wid,))
        for c in cohorts:
            self.db.execute("INSERT INTO cohort(workflow_id,cohort_id,feature,value,created_minute) "
                             "VALUES (?,?,?,?,?)", (wid, c["id"], c["feature"], c["value"], minute))

    def get_cohorts(self, wid) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM cohort WHERE workflow_id=?", (wid,))]
        for r in rows:
            r["id"] = r["cohort_id"]
        return rows

    # ---- experiment state -----------------------------------------------
    def note_veto(self, wid, cohort_id, dim, minute):
        self.db.execute(
            "INSERT INTO experiment_state(workflow_id,cohort_id,dim,status,last_veto_minute) "
            "VALUES (?,?,?,'exploring',?) ON CONFLICT(workflow_id,cohort_id,dim) DO UPDATE SET "
            "last_veto_minute=excluded.last_veto_minute", (wid, cohort_id, dim, minute))

    def get_experiment_state(self, wid, cohort_id, dim) -> dict | None:
        row = self.db.execute("SELECT * FROM experiment_state WHERE workflow_id=? AND cohort_id=? AND dim=?",
                               (wid, cohort_id, dim)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["snapshot"] = json.loads(d["snapshot_json"]) if d.get("snapshot_json") else None
        d["prior_alloc"] = json.loads(d["prior_alloc_json"]) if d.get("prior_alloc_json") else None
        return d

    def set_experiment_state(self, wid, cohort_id, dim, status, winner=None, minute=None, rate=None,
                              snapshot=None, prior_alloc=None):
        self.db.execute(
            "INSERT INTO experiment_state(workflow_id,cohort_id,dim,status,winner,promoted_at_minute,"
            "promoted_rate,snapshot_json,prior_alloc_json) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workflow_id,cohort_id,dim) DO UPDATE SET "
            "status=excluded.status, winner=excluded.winner, promoted_at_minute=excluded.promoted_at_minute, "
            "promoted_rate=excluded.promoted_rate, snapshot_json=excluded.snapshot_json, "
            "prior_alloc_json=excluded.prior_alloc_json",
            (wid, cohort_id, dim, status, winner, minute, rate,
             json.dumps(snapshot) if snapshot is not None else None,
             json.dumps(prior_alloc) if prior_alloc is not None else None))

    # ---- caller-id state ---------------------------------------------------
    def get_did_state(self, wid, did) -> dict | None:
        row = self.db.execute("SELECT * FROM did_state WHERE workflow_id=? AND caller_id=?", (wid, did)).fetchone()
        return dict(row) if row else None

    def set_did_state(self, wid, did, status, minute, probe_weight=None):
        self.db.execute(
            "INSERT INTO did_state(workflow_id,caller_id,status,flagged_at_minute,probe_weight) "
            "VALUES (?,?,?,?,?) ON CONFLICT(workflow_id,caller_id) DO UPDATE SET status=excluded.status, "
            "flagged_at_minute=excluded.flagged_at_minute, probe_weight=excluded.probe_weight",
            (wid, did, status, minute, probe_weight))

    # ---- incident state -----------------------------------------------------
    def get_incident(self, wid) -> dict | None:
        row = self.db.execute("SELECT * FROM incident_state WHERE workflow_id=?", (wid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        return d

    def set_incident(self, wid, status, detail, minute):
        self.db.execute("INSERT INTO incident_state(workflow_id,status,detail_json,opened_minute) "
                         "VALUES (?,?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET status=excluded.status, "
                         "detail_json=excluded.detail_json", (wid, status, json.dumps(detail), minute))

    # ---- proposals -----------------------------------------------------------
    def create_proposal(self, proposal_id, wid, kind, base_version, payload, minute, meta=None):
        self.db.execute("INSERT INTO proposal(proposal_id,workflow_id,kind,base_version,payload_json,status,"
                         "created_minute,updated_minute,meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (proposal_id, wid, kind, base_version, json.dumps(payload), "creating", minute, minute,
                          json.dumps(meta or {})))

    def mark_proposal(self, proposal_id, status, approval_id=None):
        self.db.execute("UPDATE proposal SET status=?, approval_id=COALESCE(?, approval_id) WHERE proposal_id=?",
                         (status, approval_id, proposal_id))

    def get_proposal(self, proposal_id) -> dict | None:
        row = self.db.execute("SELECT * FROM proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta_json") or "{}")
        return d

    def pending_proposals(self, wid=None) -> list[dict]:
        q = "SELECT * FROM proposal WHERE status IN ('creating','submitted','approved')"
        p = []
        if wid:
            q += " AND workflow_id=?"; p.append(wid)
        return [dict(r) for r in self.db.execute(q, p)]

    def recent_proposal_count(self, wid, since_minute) -> int:
        row = self.db.execute("SELECT COUNT(*) c FROM proposal WHERE workflow_id=? AND created_minute>=? "
                               "AND status!='failed'", (wid, since_minute)).fetchone()
        return row["c"]

    # ---- validation runs -----------------------------------------------------
    def save_validation(self, run_id, wid, pv, pass_rate, n, minute):
        self.db.execute("INSERT INTO validation_run(run_id,workflow_id,prompt_version,pass_rate,n,created_minute) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (run_id, wid, pv, pass_rate, n, minute))

    # ---- audit log (C12) -----------------------------------------------------
    def log_decision(self, minute, ts, wid, kind, summary, detail: dict):
        self.db.execute("INSERT INTO decisions(sim_minute,ts,workflow_id,kind,summary,detail_json) "
                         "VALUES (?,?,?,?,?,?)", (minute, ts, wid, kind, summary, json.dumps(detail)))

    def why(self, wid, minute_from=None, minute_to=None) -> list[dict]:
        q, p = "SELECT * FROM decisions WHERE workflow_id=?", [wid]
        if minute_from is not None:
            q += " AND sim_minute>=?"; p.append(minute_from)
        if minute_to is not None:
            q += " AND sim_minute<=?"; p.append(minute_to)
        q += " ORDER BY sim_minute ASC"
        return [dict(r) for r in self.db.execute(q, p)]

    def close(self):
        self.db.close()
