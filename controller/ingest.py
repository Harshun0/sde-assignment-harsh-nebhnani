"""Event ingestion (C1): idempotent, restartable, backlog-safe.

Chunks are the unit of idempotency — a chunk is either fully folded into the
store (stats + cursor, one SQLite transaction) or not at all, so re-fetching
a chunk after a crash is always safe. `lead_features` is an in-memory cache
(rebuilt once at startup from the same table we persist it to) used only to
label calls with raw features for cohort screening (C2); call events already
carry their own `cohort_id`/`variant`, so experiment and caller-ID stats never
need the join.
"""
from __future__ import annotations

import json

from . import configutil as cu


class Ingestor:
    def __init__(self, store, client):
        self.store = store
        self.client = client
        self.lead_features = store.load_lead_features()

    def catch_up(self, kind: str, sim_minute: int) -> int:
        """Fetch and fold every chunk of `kind` after our cursor. Returns #chunks processed."""
        after = self.store.last_chunk(kind)
        chunks = self.client.list_chunks(kind, after=after)
        n = 0
        for meta in chunks:
            self._process_chunk(kind, meta["chunk"], sim_minute)
            n += 1
        return n

    def _process_chunk(self, kind: str, chunk_no: int, sim_minute: int):
        text = self.client.get_chunk_text(chunk_no)
        feature_deltas: dict = {}
        exp_deltas: dict = {}
        did_deltas: dict = {}
        halluc_deltas: dict = {}
        config_changes: list = []
        approval_decisions: list = []
        lead_feature_rows: list = []
        config_change_wids: set[str] = set()

        for line in text.splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            t = ev["type"]
            if t == "lead":
                wid, lid = ev["workflow_id"], ev["lead_id"]
                f = ev["features"]
                row = (f["lang"], f["city_tier"], f["age_band"], f["value_band"])
                if lid not in self.lead_features:
                    self.lead_features[lid] = row
                    lead_feature_rows.append((lid, wid, *row))
            elif t == "call":
                self._fold_call(ev, sim_minute, feature_deltas, exp_deltas, did_deltas, halluc_deltas)
            elif t == "config_changed":
                config_change_wids.add(ev["workflow_id"])
            elif t == "approval_decided":
                approval_decisions.append((ev["approval_id"], ev["status"], ev.get("reasons")))

        for wid in config_change_wids:
            cfg = self.client.get_config(wid)
            prev = self.store.get_workflow(wid)
            dims = cu.changed_dims(prev["current_config"], cfg["config"]) if prev else []
            config_changes.append((wid, cfg["version"], cfg["actor"], dims, sim_minute, cfg["config"]))

        self.store.commit_chunk(kind, chunk_no, feature_deltas, exp_deltas, did_deltas, halluc_deltas,
                                 config_changes, approval_decisions, lead_feature_rows)

    def _fold_call(self, ev, sim_minute, feature_deltas, exp_deltas, did_deltas, halluc_deltas):
        wid, lid = ev["workflow_id"], ev["lead_id"]
        conn, eng = int(ev["connected"]), int(ev["engaged"])
        feats = self.lead_features.get(lid)
        if feats:
            for feat_name, val in zip(("lang", "city_tier", "age_band", "value_band"), feats):
                key = (wid, feat_name, val)
                d, c, e = feature_deltas.get(key, (0, 0, 0))
                feature_deltas[key] = (d + 1, c + conn, e + eng)

        cohort_id = ev.get("cohort_id") or "default"
        var = ev["variant"]
        for dim in ("welcome_message", "voice"):
            key = (wid, cohort_id, dim, var[dim])
            d, c, e, _ = exp_deltas.get(key, (0, 0, 0, 0))
            exp_deltas[key] = (d + 1, c + conn, e + eng, sim_minute)

        dkey = (wid, var["caller_id"])
        d, c = did_deltas.get(dkey, (0, 0))
        did_deltas[dkey] = (d + 1, c + conn)

        if ev.get("qa"):
            hkey = (wid, var["prompt_version"])
            hc, hf = halluc_deltas.get(hkey, (0, 0))
            halluc_deltas[hkey] = (hc + 1, hf + int(ev["qa"]["hallucination_flag"]))
