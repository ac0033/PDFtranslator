"""Run a long BabelDOC job as bounded chunks in disposable processes.

BabelDOC 0.6.4 fails random memory allocations in a long Windows process -
``RuntimeError: code=2: realloc (N bytes) failed`` - and the root cause is not
fixed. The mitigation that works is to give every chunk a fresh interpreter and
to retry a chunk whose process died.

Rendering learned this first; capture then hit exactly the same wall on a new
book, having only ever been run on one. So the loop lives here, where both use
it, rather than being written twice and fixed once.

Two properties matter for an unattended run and are the reason this is not just
a for-loop over subprocesses:

* **A chunk that exhausts its retries does not stop the run.** The remaining
  chunks still execute, so one bad page costs one chunk rather than the book,
  and the caller is told exactly what is missing.
* **Resume is the caller's job, and it is cheap.** Each chunk writes its own
  output and skips itself when that output exists, so a retry is the same
  command again. Nothing here tracks state that could disagree with the disk.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys

DEFAULT_MAX_RETRIES = 3
#: Loading the layout model and capturing one chunk needs roughly this much.
#: Measured, not guessed: a run with 366 MB of headroom died inside OpenBLAS
#: before it read a page, and one with 1563 MB captured two pages fine. 2 GB is
#: that floor plus margin for a denser chunk - and it is only a default, since
#: refusing a job that would have worked is its own kind of failure. Callers
#: that know their workload can lower it.
REQUIRED_COMMIT_BYTES = 2 * 1024 * 1024 * 1024


def chunk_ranges(first: int, last: int, chunk_size: int) -> list[tuple[int, int]]:
    """Split an inclusive one-based span into bounded chunks."""
    if chunk_size < 1:
        raise ValueError("chunk size must be at least 1")
    return [(start, min(start + chunk_size - 1, last)) for start in range(first, last + 1, chunk_size)]


def drive_chunks(chunks: list[tuple[int, int]], run_chunk, max_retries: int = DEFAULT_MAX_RETRIES,
                 workers: int = 1) -> list[dict]:
    """Run each chunk, retrying a failure, and never abandon the rest.

    ``run_chunk(first, last)`` returns a process exit code. With ``workers`` > 1
    that many chunks run at once; chunks write disjoint pages, so the order they
    finish in does not matter, and outcomes are still reported in chunk order.
    """
    if max_retries < 1:
        raise ValueError("max retries must be at least 1")
    if workers < 1:
        raise ValueError("workers must be at least 1")

    def drive(chunk: tuple[int, int]) -> dict:
        first, last = chunk
        attempts = 0
        exit_code = None
        while attempts < max_retries:
            attempts += 1
            exit_code = run_chunk(first, last)
            if exit_code == 0:
                break
        return {
            "from_page": first, "to_page": last, "attempts": attempts,
            "exit_code": exit_code, "ok": exit_code == 0,
        }

    if workers == 1 or len(chunks) <= 1:
        return [drive(chunk) for chunk in chunks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(drive, chunks))


def affordable_workers(requested: int, required_bytes: int = REQUIRED_COMMIT_BYTES) -> int:
    """How many chunk processes the machine can hold at once, at most ``requested``.

    Every process loads its own layout model, so each needs the full per-chunk
    headroom. Asking for more than fit trades a faster start for a crash inside
    OpenBLAS partway through; this quietly runs fewer instead.
    """
    available = available_commit_bytes()
    if available is None:
        return requested
    return max(1, min(requested, available // required_bytes))


def run_subprocess_chunks(
    chunks: list[tuple[int, int]],
    command_for: callable,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    announce: bool = True,
    workers: int = 1,
) -> list[dict]:
    """Drive chunks where each one is a fresh process.

    ``command_for(first, last)`` returns the argv for that chunk.
    """
    def run_chunk(first: int, last: int) -> int:
        if announce:
            print(f"=== chunk {first}-{last} ===", flush=True)
        return subprocess.run(command_for(first, last)).returncode

    return drive_chunks(chunks, run_chunk, max_retries=max_retries, workers=workers)


def summarise(outcomes: list[dict], missing: list[int] | None = None) -> dict:
    """Describe a driven run the way an unattended caller needs to read it."""
    summary = {
        "chunks": outcomes,
        "failed_chunks": [outcome for outcome in outcomes if not outcome["ok"]],
        "retried_chunks": [outcome for outcome in outcomes if outcome["attempts"] > 1],
    }
    if missing is not None:
        summary["missing_pages"] = missing
    return summary


def available_commit_bytes() -> int | None:
    """How much memory the OS will still promise, or None when unknown.

    Windows fails an allocation against the *commit limit*, not free RAM, so a
    machine with gigabytes free still refuses to start anything once commit is
    exhausted. That is what the check has to read.

    Returns None rather than a guess on a platform we cannot measure: skipping
    the preflight is honest, inventing a number is not.
    """
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        # ullAvailPageFile is the headroom against the commit limit.
        return int(status.ullAvailPageFile)
    # Linux: what the kernel can still hand out (MemAvailable counts reclaimable cache; free pages alone
    # undercount a machine that has read files), and inside a container no more than its cgroup still allows.
    available = _meminfo_available()
    if available is None:
        try:
            available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError, OSError):
            return None
    headroom = _cgroup_headroom()
    return min(available, headroom) if headroom is not None else available


def _meminfo_available(path: str = "/proc/meminfo") -> int | None:
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cgroup_headroom(root: str = "/sys/fs/cgroup") -> int | None:
    """cgroup v2 memory.max minus memory.current; None without a limit (or without cgroup v2)."""
    try:
        with open(os.path.join(root, "memory.max"), encoding="ascii") as handle:
            limit = handle.read().strip()
        if limit == "max":
            return None
        with open(os.path.join(root, "memory.current"), encoding="ascii") as handle:
            current = int(handle.read().strip())
        return max(0, int(limit) - current)
    except (OSError, ValueError):
        return None


def preflight_memory(required_bytes: int = REQUIRED_COMMIT_BYTES) -> dict:
    """Refuse to start a long job the machine cannot finish.

    Without this the run dies minutes later inside a numerical library -
    ``OpenBLAS error: Memory allocation still failed after 10 retries`` - which
    names neither the cause nor the cure. An unattended run should say what is
    wrong while someone can still act on it.
    """
    available = available_commit_bytes()
    if available is None:
        return {"checked": False, "reason": "commit headroom is not measurable on this platform"}
    return {
        "checked": True,
        "available_mb": round(available / 1024 / 1024),
        "required_mb": round(required_bytes / 1024 / 1024),
        "ok": available >= required_bytes,
    }


def require_memory(required_bytes: int = REQUIRED_COMMIT_BYTES) -> dict:
    result = preflight_memory(required_bytes)
    if result.get("checked") and not result["ok"]:
        raise MemoryError(
            f"only {result['available_mb']} MB of commit headroom, need {result['required_mb']} MB. "
            "Free memory before starting: a long BabelDOC run dies inside OpenBLAS with an error "
            "that names neither the cause nor the cure."
        )
    return result
