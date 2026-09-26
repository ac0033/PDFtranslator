"""Page-level review: find mismatches without a model, keep only fixes that are no worse."""
import json
from pathlib import Path

import pymupdf
import pytest

from pdf_translation.config import RunConfig
from pdf_translation.hashing import stable_hash
from pdf_translation.layout_review import (
    UnitView, hidden_segments, hidden_share, measure_fit, review_pages, validate_fix,
)
from pdf_translation.llm import LLMResponse
from pdf_translation.pipeline import make_request
from pdf_translation.quality import check_quality
from pdf_translation.schemas import TranslationDraft, TranslationResult, TranslationUnit, Usage
from pdf_translation.storage import StateStore

M1, M2 = "⟦P:bd-p0001-u0000-o000⟧", "⟦P:bd-p0001-u0000-o001⟧"


def config(**extra) -> RunConfig:
    values = dict(run_id="layout", provider="deepseek", model_id="m", style_revision="s", prompt_revision="p",
                  adapter_revision="a", pricing_revision="p", max_output_tokens=500, budget_usd=1.0,
                  input_usd_per_million=1.0, output_usd_per_million=1.0)
    return RunConfig(**{**values, **extra})


def unit(uid: str, text: str) -> TranslationUnit:
    return TranslationUnit(unit_id=uid, document_id="d", ir_revision="r", unit_type="paragraph", source_lang="en",
                           target_lang="zh-CN", source_text=text, source_hash=stable_hash(text))


def result(u: TranslationUnit, text: str) -> TranslationResult:
    request = make_request(u, config())
    return TranslationResult(request_id=request.request_id, unit_id=u.unit_id, finish="complete", model=request.model,
                             draft=TranslationDraft(unit_id=u.unit_id, translated_text=text),
                             usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0, usage_source="estimated"))


# --- validation ------------------------------------------------------------------

SOURCE = f"The gradient {M1} is clipped when it exceeds the threshold {M2} in every step of training."
OLD = f"当梯度{M1}在训练的每一步超过阈值{M2}时会被裁剪。"


@pytest.mark.parametrize("kind,new,reason", [
    ("mistranslation", f"当梯度{M1}超过阈值时会被裁剪。", "quality gate blocks"),
    ("symbol_misplaced", f"当梯度{M1} = 在训练的每一步超过阈值{M2}时会被裁剪。", "formula notation"),
    ("mistranslation", f"当梯度{M1}在训练的每一步超过阈值{M2}时会被裁剪（clipped when too large）。", "English back"),
    ("symbol_misplaced", f"梯度{M1}被裁剪{M2}。", "rewrites the wording"),
    ("mistranslation", f"梯度{M1}裁剪{M2}。", "drops too much"),
    ("too_long", f"当梯度{M1}在训练的每一步都超过阈值{M2}时就会被裁剪掉。", "not shorter"),
    ("other", OLD, "unchanged"),
])
def test_a_fix_that_is_worse_than_what_it_replaces_is_refused(kind, new, reason):
    u = unit("u", SOURCE)
    accepted, why, _ = validate_fix(u, result(u, OLD), new, kind)
    assert not accepted and reason in why


def test_a_real_fix_is_accepted():
    u = unit("u", SOURCE)
    moved = f"当梯度在训练的每一步超过阈值{M2}{M1}时会被裁剪。"
    assert validate_fix(u, result(u, OLD), moved, "symbol_misplaced")[0]
    shorter = f"梯度{M1}每步超过阈值{M2}即裁剪。"
    assert validate_fix(u, result(u, OLD), shorter, "too_long")[0]


def test_dropping_hidden_text_may_drop_its_numbers():
    u = unit("u", "Clip at 5. teacher forcing 1993")
    old = "在 5 处裁剪。教师强制 1993"
    assert not validate_fix(u, result(u, old), "在 5 处裁剪。", "mistranslation")[0]
    assert validate_fix(u, result(u, old), "在 5 处裁剪。", "extraction_garbage")[0]


# --- detection without a model -----------------------------------------------------

def test_off_page_text_is_told_apart_from_visible_text():
    visible, hidden = "ifgthresholdthen|endif", "teacherforcing|leyiswideaswehavealargeregion"
    source = f"⟦P:layout-a⟧if {M1} threshold then ⟦P:layout-b⟧teacher forcing."
    assert hidden_segments(source, visible, hidden) == ["teacher forcing."]
    assert hidden_share("ley is wide, as we have a large region", visible, hidden) == 1.0
    assert hidden_share("if the threshold then", visible, hidden) == 0.0


def _page(texts: list[tuple[float, float, str, float]], cjk: bool = False) -> pymupdf.Document:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    for x, y, text, size in texts:
        page.insert_text((x, y), text, fontsize=size, fontname="china-s" if cjk else "helv")
    return document


def test_a_translation_shrunk_to_fit_its_box_is_measured_against_its_own_page():
    rows = [(60, 200 + 60 * i, "English words in this paragraph", 10) for i in range(4)]
    source = _page(rows)
    chinese = "这是一段被正常排版的中文译文内容共计二十多个汉字左右吧"
    rendered = _page([(60, 200, chinese, 7), (60, 260, chinese, 7), (60, 320, chinese, 7), (60, 380, chinese, 5.5)], cjk=True)
    header = {"bbox": [50, 20, 550, 40], "unit_id": "header"}
    records = [{"bbox": [50, 185 + 60 * i, 550, 210 + 60 * i], "unit_id": f"u{i}"} for i in range(4)] + [header]
    fit = measure_fit(source[0], rendered[0], records)
    assert set(fit) == {"u3"}  # the page sets Chinese at 0.7 everywhere; only u3 is squeezed below that
    assert fit["u3"]["target_chars"] < fit["u3"]["current_chars"]


# --- the review loop, with a fake model -------------------------------------------

class FakeModel:
    protocol, api_key_env = "fake", None

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def complete(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=json.dumps({"problems": reply}), usage=Usage(input_tokens=100, output_tokens=50, usage_source="provider"))


class Refused(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.fixture
def book(tmp_path: Path):
    units = {f"u{n}": unit(f"u{n}", SOURCE) for n in range(1, 4)}
    store = StateStore(tmp_path / "state.sqlite")
    store.begin_run("layout", "i", "c")
    for u in units.values():
        store.prepare("layout", u.unit_id, f"k-{u.unit_id}")
        r = result(u, OLD)
        store.commit("layout", r, check_quality(u, r), f"k-{u.unit_id}")
    source = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    doc.save(source)
    for n in range(1, 4):
        (tmp_path / f"page-{n:04d}").mkdir()
        single = pymupdf.open()
        single.new_page()
        single.save(tmp_path / f"page-{n:04d}" / "book.zh.mono.pdf")
    pages = {n: [UnitView(f"u{n}", SOURCE, SOURCE, OLD)] for n in range(1, 4)}
    return pages, dict(store=store, source_pdf=source, capture=tmp_path, stem="book", units=units)


def fix(uid, severity="wrong_meaning"):
    return {"unit_id": uid, "kind": "symbol_misplaced", "severity": severity, "explanation": "moved",
            "fixed_translation": f"当梯度在训练的每一步超过阈值{M2}{M1}时会被裁剪。"}


def test_review_applies_serious_fixes_ignores_minor_ones_and_books_every_call(book):
    replies = [[fix("u1")], [fix("u2", "minor")], [fix("u9")]]  # u9 is not on page 3
    report = review_pages(book[0], client=FakeModel(replies), config=config(), **book[1])
    accepted = {d.unit_id: d.accepted for d in report.decisions}
    assert accepted == {"u1": True, "u2": False} and report.calls == 3
    assert book[1]["store"].spent("layout") == pytest.approx(3 * (100 + 50) / 1_000_000)


def test_an_account_refusal_stops_the_remaining_pages(book):
    model = FakeModel([Refused(402)])
    report = review_pages(book[0], client=model, config=config(), **book[1])
    assert len(model.requests) == 1 and "HTTP 402" in report.stopped
    assert all("not reviewed" in report.errors[n] for n in (2, 3))


def test_the_budget_stops_review_before_it_is_exceeded(book):
    model = FakeModel([[]])
    report = review_pages(book[0], client=model, config=config(budget_usd=0.000001), **book[1])
    assert not model.requests and "budget" in report.stopped


def test_a_model_that_cannot_read_images_is_asked_again_with_text_only(book):
    class NoImages(Exception):
        status_code = 400

    model = FakeModel([NoImages("image input is not supported"), [], [], []])
    report = review_pages(book[0], client=model, config=config(), **book[1])
    assert model.requests[0].images and not model.requests[1].images
    assert report.vision is False and not report.errors


CODE_SOURCE = f"⟦P:layout-a⟧if {M1} threshold then ⟦P:layout-b⟧teacher forcing. ⟦P:layout-c⟧end if"
CODE_OLD = f"⟦P:layout-a⟧如果 {M1} 阈值，则 教师强制。⟦P:layout-b⟧⟦P:layout-c⟧"


def test_code_may_get_its_own_keywords_back_but_nothing_invented_or_hidden():
    u = unit("u", CODE_SOURCE)
    current = result(u, CODE_OLD)
    hidden = ["teacher forcing."]
    restored = f"⟦P:layout-a⟧if {M1} threshold then ⟦P:layout-b⟧⟦P:layout-c⟧end if"
    assert validate_fix(u, current, restored, "code_translated", hidden)[0]
    kept_hidden = f"⟦P:layout-a⟧if {M1} threshold then ⟦P:layout-b⟧teacher forcing ⟦P:layout-c⟧end if"
    assert "not visible" in validate_fix(u, current, kept_hidden, "extraction_garbage", hidden)[1]
    invented = f"⟦P:layout-a⟧if {M1} threshold then clip ⟦P:layout-b⟧⟦P:layout-c⟧end if"
    assert "does not have" in validate_fix(u, current, invented, "code_translated", hidden)[1]
