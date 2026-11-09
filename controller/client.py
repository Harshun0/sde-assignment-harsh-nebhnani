"""Thin HTTP client over the simulator API. See docs/SIMULATOR.md for the contract. """
from __future__ import annotations

import time

import httpx


class SimError(Exception):
    def __init__(self, status, body):
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body


class SimClient:
    def __init__(self, base_url: str, timeout: float = 120.0, retries: int = 5):
        self.c = httpx.Client(base_url=base_url, timeout=timeout)
        self.retries = retries

    def _req(self, method, path, **kw):
        last = None
        for attempt in range(self.retries):
            try:
                r = self.c.request(method, path, **kw)
            except httpx.TransportError as e:
                last = e
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code >= 500:
                last = SimError(r.status_code, r.text)
                time.sleep(min(2 ** attempt, 10))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except Exception:
                    body = r.text
                raise SimError(r.status_code, body)
            return r
        raise last

    def health(self):
        return self._req("GET", "/health").json()

    def sim_state(self):
        return self._req("GET", "/sim/state").json()

    def advance(self, minutes: int):
        return self._req("POST", "/sim/advance", json={"minutes": minutes}).json()

    def list_chunks(self, kind: str, after: int = -1):
        return self._req("GET", "/events/chunks", params={"kind": kind, "after": after}).json()

    def get_chunk_text(self, n: int) -> str:
        return self._req("GET", f"/events/chunks/{n}").text

    def workflows(self):
        return self._req("GET", "/workflows").json()

    def workflow(self, wid):
        return self._req("GET", f"/workflows/{wid}").json()

    def get_config(self, wid):
        return self._req("GET", f"/workflows/{wid}/config").json()

    def config_history(self, wid):
        return self._req("GET", f"/workflows/{wid}/config/history").json()

    def auto_apply(self, wid, body):
        return self._req("PUT", f"/workflows/{wid}/config", json=body)

    def request_approval(self, body):
        return self._req("POST", "/approvals", json=body)

    def get_approval(self, aid):
        return self._req("GET", f"/approvals/{aid}").json()

    def list_approvals(self, workflow_id=None, status=None):
        p = {}
        if workflow_id:
            p["workflow_id"] = workflow_id
        if status:
            p["status"] = status
        return self._req("GET", "/approvals", params=p).json()

    def apply_approval(self, wid, approval_id):
        return self._req("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id})

    def suggest_fixes(self, workflow_id, problem):
        return self._req("POST", "/llm/suggest-fixes", json={"workflow_id": workflow_id, "problem": problem}).json()

    def run_validation(self, workflow_id, prompt_version, n=200):
        return self._req("POST", "/validation/runs",
                          json={"workflow_id": workflow_id, "prompt_version": prompt_version, "n": n}).json()
