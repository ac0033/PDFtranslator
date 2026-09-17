"""The loop that keeps a crashing BabelDOC from costing a whole book."""
import pytest

from pdf_translation.chunk_driver import chunk_ranges, drive_chunks, run_subprocess_chunks, summarise


def test_chunk_ranges_cover_every_page_with_a_ragged_tail():
    assert chunk_ranges(1, 25, 10) == [(1, 10), (11, 20), (21, 25)]
    assert chunk_ranges(1, 10, 10) == [(1, 10)]
    assert chunk_ranges(7, 9, 10) == [(7, 9)]
    with pytest.raises(ValueError):
        chunk_ranges(1, 10, 0)


def test_driver_retries_a_crashed_chunk_and_continues_past_an_exhausted_one():
    calls = []

    def run_chunk(first, last):
        calls.append((first, last))
        if first == 11:
            return 0 if calls.count((11, 20)) >= 2 else 1
        if first == 21:
            return 1
        return 0

    outcomes = drive_chunks([(1, 10), (11, 20), (21, 30), (31, 40)], run_chunk, max_retries=3)
    assert [o['attempts'] for o in outcomes] == [1, 2, 3, 1]
    assert [o['ok'] for o in outcomes] == [True, True, False, True]
    # An exhausted chunk must not abort the run: page 31-40 still ran.
    assert (31, 40) in calls


def test_driver_rejects_a_retry_budget_that_never_runs():
    with pytest.raises(ValueError):
        drive_chunks([(1, 10)], lambda first, last: 0, max_retries=0)


def test_a_summary_names_what_failed_and_what_was_retried():
    outcomes = drive_chunks([(1, 10), (11, 20)], lambda first, last: 0 if first == 1 else 1, max_retries=2)
    summary = summarise(outcomes, missing=[11, 12])
    assert [c["from_page"] for c in summary["failed_chunks"]] == [11]
    assert [c["from_page"] for c in summary["retried_chunks"]] == [11]
    assert summary["missing_pages"] == [11, 12]


def test_each_chunk_gets_its_own_process(monkeypatch):
    """Isolation is the whole mitigation: one interpreter per chunk."""
    from pdf_translation import chunk_driver

    seen = []

    class Completed:
        returncode = 0

    monkeypatch.setattr(chunk_driver.subprocess, "run", lambda cmd, *a, **k: (seen.append(cmd), Completed())[1])
    outcomes = run_subprocess_chunks([(1, 10), (11, 20)], lambda f, l: ["cmd", str(f), str(l)], announce=False)
    assert seen == [["cmd", "1", "10"], ["cmd", "11", "20"]]
    assert all(o["ok"] for o in outcomes)


def test_a_job_the_machine_cannot_finish_is_refused_before_it_starts(monkeypatch):
    """Without this the run dies minutes later inside OpenBLAS, naming nothing."""
    from pdf_translation import chunk_driver

    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: 300 * 1024 * 1024)
    result = chunk_driver.preflight_memory()
    assert result['checked'] and not result['ok']
    assert result['available_mb'] == 300
    with pytest.raises(MemoryError, match='commit headroom'):
        chunk_driver.require_memory()


def test_enough_headroom_passes_quietly(monkeypatch):
    from pdf_translation import chunk_driver

    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: 8 * 1024 * 1024 * 1024)
    assert chunk_driver.require_memory()['ok'] is True


def test_an_unmeasurable_platform_skips_rather_than_guesses(monkeypatch):
    from pdf_translation import chunk_driver

    monkeypatch.setattr(chunk_driver, 'available_commit_bytes', lambda: None)
    result = chunk_driver.require_memory()
    assert result['checked'] is False
    assert 'not measurable' in result['reason']
