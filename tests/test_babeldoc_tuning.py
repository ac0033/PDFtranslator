"""The per-process BabelDOC tuning must save time without changing what it finds."""
import pytest

from pdf_translation import babeldoc_adapter


@pytest.fixture
def fresh_tuning(monkeypatch):
    from babeldoc.assets import assets
    monkeypatch.setattr(babeldoc_adapter, "_BABELDOC_TUNED", False)
    monkeypatch.setattr(assets, "get_font_and_metadata", assets.get_font_and_metadata)
    return assets


def test_font_lookup_is_verified_once_per_font(fresh_tuning, monkeypatch):
    calls = []
    monkeypatch.setattr(fresh_tuning, "get_font_and_metadata", lambda name: calls.append(name) or (f"/fonts/{name}", {"ascent": 1}))
    babeldoc_adapter._tune_babeldoc_for_local_translators()
    for _ in range(3):
        assert fresh_tuning.get_font_and_metadata("a.ttf") == ("/fonts/a.ttf", {"ascent": 1})
    fresh_tuning.get_font_and_metadata("b.ttf")
    assert calls == ["a.ttf", "b.ttf"]


def test_a_failed_font_lookup_is_not_remembered(fresh_tuning, monkeypatch):
    attempts = []

    def flaky(name):
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("download failed")
        return "/fonts/a.ttf", {}

    monkeypatch.setattr(fresh_tuning, "get_font_and_metadata", flaky)
    babeldoc_adapter._tune_babeldoc_for_local_translators()
    with pytest.raises(RuntimeError):
        fresh_tuning.get_font_and_metadata("a.ttf")
    assert fresh_tuning.get_font_and_metadata("a.ttf") == ("/fonts/a.ttf", {})
    assert len(attempts) == 2


def test_capture_asks_babeldoc_for_no_pdfs_and_rendering_still_gets_them(monkeypatch, tmp_path):
    seen = []

    class Recorded(Exception):
        pass

    def fake_config(**kwargs):
        seen.append({key: kwargs[key] for key in ("no_mono", "no_dual", "skip_clean")})
        raise Recorded

    monkeypatch.setattr("babeldoc.format.pdf.translation_config.TranslationConfig", fake_config)
    monkeypatch.setattr(babeldoc_adapter, "_tune_babeldoc_for_local_translators", lambda: None)
    for emit in (False, True):
        with pytest.raises(Recorded):
            babeldoc_adapter._run_babeldoc_page(tmp_path / "x.pdf", 0, tmp_path / "out", object(), layout=object(), emit_pdf=emit)
    assert seen == [{"no_mono": True, "no_dual": True, "skip_clean": True},
                    {"no_mono": False, "no_dual": False, "skip_clean": False}]
