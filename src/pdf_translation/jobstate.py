"""A job's on-disk state shared by the runner (writer) and the job service (reader).

* ``state.json`` - status, phase, current stage, which delivery a caller can
  take now, the list of deliveries, the AI OS record, cost, error;
* ``events.jsonl`` - numbered events, so a caller that lost its connection
  reads on from the last number it saw;
* ``deliveries/<version>/`` - the files of each published version
  (``usable``, ``optimized``, ``degraded``, ``revised``) with a
  ``manifest.json`` holding each file's name, size and SHA-256.

Published files are copies: later work on the job (optimisation, a revision)
never changes a version a caller may already have downloaded.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .hashing import sha256_file

STATE = "state.json"
EVENTS = "events.jsonl"
DELIVERIES = "deliveries"
EVENT_TYPES = ("job.accepted", "stage.changed", "repair.round", "delivery.usable", "delivery.optimized",
               "delivery.degraded", "delivery.revised", "job.failed", "job.finished", "job.cancelled")
#: Order of preference when choosing the version to recommend.
VERSION_RANK = {"optimized": 3, "revised": 2, "usable": 1, "degraded": 0}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _locked(path: Path, timeout: float = 10.0):
    """A cross-process lock held while one writer appends an event."""
    lock = path.with_suffix(path.suffix + ".lock")
    started = time.monotonic()
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - started > timeout:
                try:  # a writer that died holding the lock must not block the job forever
                    if time.time() - lock.stat().st_mtime > timeout:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                raise TimeoutError(f"cannot lock {lock}")
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(handle)
        lock.unlink(missing_ok=True)


def read_state(job_dir: Path) -> dict | None:
    try:
        return json.loads((job_dir / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def atomic_write(path: Path, text: str, *, attempts: int = 50) -> None:
    """Replace ``path`` with ``text`` in one step.

    On Windows the replace fails while another process has the file open for
    reading (the job service polls these files), so it is retried briefly
    instead of losing the write.
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


def write_state(job_dir: Path, state: dict) -> None:
    state["updated_at"] = now()
    atomic_write(job_dir / STATE, json.dumps(state, ensure_ascii=False, indent=2))


def append_event(job_dir: Path, kind: str, data: dict | None = None) -> dict:
    path = job_dir / EVENTS
    with _locked(path):
        seq = 0
        if path.is_file():
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                seq = json.loads(lines[-1])["seq"]
        event = {"seq": seq + 1, "type": kind, "at": now(), "job_id": job_dir.name, "data": data or {}}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def read_events(job_dir: Path, after: int = 0) -> list[dict]:
    path = job_dir / EVENTS
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            event = json.loads(line)
            if event["seq"] > after:
                events.append(event)
    return events


def last_seq(job_dir: Path) -> int:
    events = read_events(job_dir)
    return events[-1]["seq"] if events else 0


def publish(job_dir: Path, version: str, files: dict[str, Path | None], acceptance: dict | None, *,
            note: str | None = None) -> dict:
    """Copy one version's files into ``deliveries/<version>/`` and describe them."""
    target = job_dir / DELIVERIES / version
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    manifest: dict = {"version": version, "created_at": now(), "files": {}}
    for name, path in files.items():
        if path is None or not Path(path).is_file():
            continue
        destination = target / Path(path).name
        shutil.copy2(path, destination)
        manifest["files"][name] = {"file": destination.name, "bytes": destination.stat().st_size,
                                   "sha256": sha256_file(destination)}
    if acceptance is not None:
        (target / "acceptance-report.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["files"]["acceptance"] = {"file": "acceptance-report.json",
                                           "bytes": (target / "acceptance-report.json").stat().st_size,
                                           "sha256": sha256_file(target / "acceptance-report.json")}
        manifest["verdict"] = acceptance.get("verdict")
        manifest["major_problems"] = len(acceptance.get("major_problems", []))
        manifest["needs_human"] = sum(item.get("needs_human", 0) for item in acceptance.get("minor_issues", []))
    if note:
        manifest["note"] = note
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def deliveries(job_dir: Path) -> list[dict]:
    root = job_dir / DELIVERIES
    found = []
    for version in VERSION_RANK:
        path = root / version / "manifest.json"
        if path.is_file():
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
    return sorted(found, key=lambda entry: (entry.get("created_at", ""), VERSION_RANK[entry["version"]]))


def recommended(job_dir: Path) -> dict | None:
    """The version a caller should take: the state says which; otherwise the best-ranked one present."""
    state = read_state(job_dir) or {}
    available = {entry["version"]: entry for entry in deliveries(job_dir)}
    chosen = state.get("recommended")
    if chosen in available:
        return available[chosen]
    for version in sorted(available, key=lambda v: -VERSION_RANK[v]):
        return available[version]
    return None
