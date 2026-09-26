"""Client for a remote ``pdftrans serve``: the same job calls, over HTTP."""
from __future__ import annotations

import json
from pathlib import Path

import httpx


class RemoteJobs:
    def __init__(self, server: str, token: str | None = None, timeout: float = 600.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=server.rstrip("/"), headers=headers, timeout=timeout)

    def _json(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise RuntimeError(f"server refused ({response.status_code}): {message}")
        return response.json()

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

    def list(self, limit: int = 20) -> list[dict]:
        return self._json(self.http.get("/v1/jobs", params={"limit": limit}))["jobs"]

    def cancel(self, job_id: str) -> dict:
        return self._json(self.http.post(f"/v1/jobs/{job_id}/cancel"))

    def log_tail(self, job_id: str, lines: int = 40) -> str:
        return self._json(self.http.get(f"/v1/jobs/{job_id}/log", params={"lines": lines}))["log"]

    def download(self, job_id: str, name: str, destination: Path) -> Path:
        with self.http.stream("GET", f"/v1/jobs/{job_id}/files/{name}") as response:
            if response.status_code >= 400:
                response.read()
                self._json(response)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        return destination
