"""Client for a remote ``pdftrans serve``: the same job calls, over HTTP."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx


class RemoteError(RuntimeError):
    def __init__(self, status: int, error: dict | str):
        self.status = status
        self.error = error if isinstance(error, dict) else {"code": "error", "message": str(error)}
        super().__init__(f"server refused ({status}): {self.error.get('code')}: {self.error.get('message')}"
                         + (f" ({self.error['hint']})" if self.error.get("hint") else ""))


class RemoteJobs:
    def __init__(self, server: str, token: str | None = None, timeout: float = 600.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=server.rstrip("/"), headers=headers, timeout=timeout)

    def _json(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            try:
                error = response.json().get("error", response.text)
            except ValueError:
                error = response.text
            raise RemoteError(response.status_code, error)
        return response.json()

    def submit(self, source: Path | str, request: dict, *, api_keys: dict[str, str] | None = None,
               callback_secret: str | None = None, idempotency_key: str | None = None) -> dict:
        """Submit a local file (sent inline) or a URL (fetched by the server)."""
        headers = {header: api_keys[role] for role, header in (("translation", "X-LLM-API-Key"),
                                                               ("agent", "X-Agent-API-Key"), ("jev", "X-Jev-API-Key"))
                   if api_keys and api_keys.get(role)}
        if callback_secret:
            headers["X-Callback-Secret"] = callback_secret
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            body = {"source": {"url": source}, **request}
        else:
            path = Path(source)
            body = {"source": {"base64": base64.b64encode(path.read_bytes()).decode("ascii"), "filename": path.name},
                    **request}
        return self._json(self.http.post("/v1/jobs", json=body, headers=headers))

    # v0.10 names, kept for callers written against them
    def submit_file(self, pdf: Path, options: dict, *, api_key: str | None = None) -> dict:
        headers = {"Content-Type": "application/pdf", "X-Job-Options": json.dumps(options)}
        if api_key:
            headers["X-LLM-API-Key"] = api_key
        return self._json(self.http.post("/v1/jobs", params={"filename": pdf.name}, content=pdf.read_bytes(), headers=headers))

    def submit_url(self, url: str, options: dict, *, api_key: str | None = None) -> dict:
        headers = {"X-LLM-API-Key": api_key} if api_key else {}
        return self._json(self.http.post("/v1/jobs", json={"pdf_url": url, "options": options}, headers=headers))

    def get(self, job_id: str) -> dict:
        return self._json(self.http.get(f"/v1/jobs/{job_id}"))

    def events(self, job_id: str, after: int = 0, wait: float = 0.0) -> dict:
        return self._json(self.http.get(f"/v1/jobs/{job_id}/events", params={"after": after, "wait": wait},
                                        timeout=max(30.0, wait + 30)))

    def list(self, limit: int = 20) -> list[dict]:
        return self._json(self.http.get("/v1/jobs", params={"limit": limit}))["jobs"]

    def cancel(self, job_id: str) -> dict:
        return self._json(self.http.post(f"/v1/jobs/{job_id}/cancel"))

    def resume(self, job_id: str, *, api_key: str | None = None) -> dict:
        headers = {"X-LLM-API-Key": api_key} if api_key else {}
        return self._json(self.http.post(f"/v1/jobs/{job_id}/resume", headers=headers))

    def profile(self) -> dict:
        return self._json(self.http.get("/v1/profile"))

    def set_profile(self, translation: dict | None, agent: dict | None, client_id: str | None = None) -> dict:
        body = {k: v for k, v in (("translation", translation), ("agent", agent), ("client_id", client_id)) if v}
        return self._json(self.http.put("/v1/profile", json=body))

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        return self._json(self.http.get(f"/v1/jobs/{job_id}/log", params={"lines": lines}))["log"]

    def units(self, job_id: str, *, pages: list[int] | None = None, unit_ids: list[str] | None = None,
              with_issues: bool = False, states: list[str] | None = None, limit: int = 200) -> dict:
        params = [("page", p) for p in pages or []] + [("unit_id", u) for u in unit_ids or []]
        params += [("state", st) for st in states or []] + [("with_issues", int(with_issues)), ("limit", limit)]
        return self._json(self.http.get(f"/v1/jobs/{job_id}/units", params=params))

    def assist(self, job_id: str, task: str, *, api_key: str | None = None, **arguments) -> dict:
        headers = {"X-LLM-API-Key": api_key} if api_key else {}
        body = {"task": task, **{k: v for k, v in arguments.items() if v is not None}}
        return self._json(self.http.post(f"/v1/jobs/{job_id}/assist", json=body, headers=headers))

    def revise(self, job_id: str, fixes: list[dict], editor: str) -> dict:
        return self._json(self.http.post(f"/v1/jobs/{job_id}/revise", json={"fixes": fixes, "editor": editor}))

    def download(self, job_id: str, name: str, destination: Path, version: str | None = None) -> Path:
        route = f"/v1/jobs/{job_id}/files/{version + '/' if version else ''}{name}"
        with self.http.stream("GET", route) as response:
            if response.status_code >= 400:
                response.read()
                self._json(response)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        return destination
