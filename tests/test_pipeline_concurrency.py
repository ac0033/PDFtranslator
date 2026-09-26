"""Provider requests in flight together must not change what a run produces or spends."""
import threading
import time
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

from pdf_translation.budget import BudgetExceeded, estimate_cost
from pdf_translation.config import RunConfig
from pdf_translation.hashing import stable_hash
from pdf_translation.pipeline import (
    ProviderAttemptsExhausted, ProviderUnavailable, estimated_input_token_ceiling, make_request, run_units,
)
from pdf_translation.providers import ProviderCallError
from pdf_translation.schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore


def config(run_id: str, concurrency: int, budget: float = 1.0) -> RunConfig:
    return RunConfig(run_id=run_id, provider="deepseek", model_id="deepseek-flash", style_revision="s1", prompt_revision="p1",
                     adapter_revision="a1", pricing_revision="p", max_output_tokens=100, budget_usd=budget,
                     input_usd_per_million=1.0, output_usd_per_million=1.0, translation_concurrency=concurrency)


def units(count: int) -> list[TranslationUnit]:
    texts = [f"Section {n} keeps its number {n}." for n in range(count)]
    return [TranslationUnit(unit_id=f"u{n}", document_id="d1", ir_revision="r1", unit_type="paragraph", source_lang="en",
                            target_lang="zh-CN", source_text=text, source_hash=stable_hash(text)) for n, text in enumerate(texts)]


class SlowProvider:
    """Echoes the source after a pause and records how many calls overlapped."""

    def __init__(self, delay: float = 0.05, fail_unit: str | None = None):
        self.delay = delay
        self.fail_unit = fail_unit
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def translate(self, request):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if request.unit.unit_id == self.fail_unit:
                raise ProviderCallError("bad schema", category="invalid_structured_output",
                                        usage=Usage(input_tokens=10, output_tokens=5, usage_source="provider"))
            return TranslationResult(
                request_id=request.request_id, unit_id=request.unit.unit_id, finish="complete", model=request.model,
                draft=TranslationDraft(unit_id=request.unit.unit_id, translated_text=request.unit.source_text),
                usage=Usage(input_tokens=10, output_tokens=5, usage_source="provider"),
            )
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")


def test_concurrency_is_not_part_of_the_run_identity():
    # Raising the setting on a half-finished book must resume it, not refuse it.
    assert config("same", 1).config_hash == config("same", 16).config_hash
    legacy = config("same", 1).model_dump(mode="json")
    del legacy["translation_concurrency"]
    assert config("same", 1).config_hash == stable_hash(legacy)


def test_concurrent_run_overlaps_requests_and_matches_the_serial_result(tmp_path: Path, monkeypatch):
    serial, parallel = SlowProvider(), SlowProvider()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: serial)
    one = run_units(units(8), config("serial", 1), tmp_path)
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: parallel)
    many = run_units(units(8), config("parallel", 4), tmp_path)
    assert serial.max_active == 1 and parallel.max_active > 1
    assert one["states"] == many["states"] == {"accepted": 8}
    assert one["cost_usd"] == pytest.approx(many["cost_usd"])
    serial_store = StateStore(tmp_path / "serial" / "state.sqlite")
    parallel_store = StateStore(tmp_path / "parallel" / "state.sqlite")
    for item in units(8):
        assert (serial_store.result("serial", item.unit_id).draft.translated_text
                == parallel_store.result("parallel", item.unit_id).draft.translated_text)


def test_requests_in_flight_count_against_the_budget(tmp_path: Path, monkeypatch):
    probe = config("budget", 8)
    ceiling = estimate_cost(estimated_input_token_ceiling(make_request(units(1)[0], probe)), probe.max_output_tokens,
                            probe.input_usd_per_million, probe.output_usd_per_million)
    provider = SlowProvider(delay=0.1)
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    # Room for two reserved ceilings, not eight: without counting in-flight
    # requests, all eight threads would pass against the same empty ledger.
    with pytest.raises(BudgetExceeded):
        run_units(units(8), config("budget", 8, budget=ceiling * 2.5), tmp_path)
    assert provider.max_active <= 2


def test_account_failure_stops_requests_not_yet_sent(tmp_path: Path, monkeypatch):
    class Denied(SlowProvider):
        def translate(self, request):
            with self.lock:
                self.calls += 1
            time.sleep(0.05)
            response = httpx.Response(402, request=httpx.Request("POST", "https://api.deepseek.com/responses"))
            raise APIStatusError("Insufficient Balance", response=response, body=None)

    denied = Denied()
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: denied)
    with pytest.raises(ProviderUnavailable, match="HTTP 402"):
        run_units(units(12), config("denied", 2), tmp_path)
    # At most the requests already in flight when the first 402 came back.
    assert denied.calls <= 2 + 1


def test_one_exhausted_unit_does_not_hold_back_the_others(tmp_path: Path, monkeypatch):
    provider = SlowProvider(fail_unit="u3")
    monkeypatch.setattr("pdf_translation.pipeline.DeepSeekProvider", lambda **_: provider)
    with pytest.raises(ProviderAttemptsExhausted) as raised:
        run_units(units(6), config("exhaust", 3), tmp_path)
    assert raised.value.unit_id == "u3"
    store = StateStore(tmp_path / "exhaust" / "state.sqlite")
    assert all(store.state("exhaust", f"u{n}") == "accepted" for n in (0, 1, 2, 4, 5))
