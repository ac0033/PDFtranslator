"""Turn quality-gate warnings into a short list a human still has to look at.

A run raises far more warnings than it has defects. On val.pdf the gate raised
136 number warnings covering 5 real errors and 44 marker-order warnings
covering 1. Reading all of them by hand is what kept delivery attended.

Nothing here dismisses a warning on a hunch. A warning is dismissed only when

* **a rule proves it is an artifact of the check itself** - the number check
  reads its inventory with ``(?<![A-Za-z0-9_])`` lookarounds, so a digit glued
  to a letter is invisible on the English side and visible next to Chinese:
  ``Figure2.1`` yields nothing while ``图2.1`` yields ``2.1``. Re-reading both
  sides with a glued-digit tolerant pattern makes that class disappear. Against
  the 88 human verdicts in the val corpus this dismisses 116 of 136 warnings
  and silences none of the 5 real errors; tests/test_triage.py asserts it; or
* **a human already judged it harmless** - recorded in a :class:`ReviewLedger`
  keyed by result hash, so any retranslation brings the warning back.

This module also raises three signals the gate never had, because the defects
they catch are the ones val needed humans to find:

``retained_english``
    English words left in Chinese prose, grouped by term, because that is how
    the decision is actually made: ``rollout`` is wrong everywhere or nowhere.
``untranslated_unit``
    A unit whose Chinese came back mostly Latin, excluding bibliography-shaped
    sources, which stay English on purpose.
``leaked_markup``
    Markup tags present in the translation but not the source.

Two rules were tried and rejected, recorded here so they are not retried:

* Retained English cannot be triaged by document-level consistency. A term the
  model systematically failed to translate is as consistent as one kept on
  purpose: in val, ``rollout`` (a real omission) survived in 91% of its
  occurrences while ``for`` (a Julia keyword, correctly kept) survived in 4%.
* Marker order had 1 real case in 44. No rule can be validated on one positive,
  so none is offered and every marker-order warning stays ``needs_human``.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

#: The gate catches invented markup while a retry can still fix it; this layer
#: catches what reached the page anyway.
from .quality import MARKUP_TAG

#: The quality gate's own number pattern, repeated so the artifact rule can be
#: explained against it.
STRICT_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[+-]?(?:\d+(?:\.\d+)?%?)(?![A-Za-z0-9_])")
#: Same numbers, but still counted when glued to ASCII letters.
GLUED_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)*%?")

_THOUSANDS_GAP = re.compile(r"(?<=\d)[    ](?=\d{3}(?!\d))")
_PERCENT_WORD = re.compile(r"(\d+(?:\.\d+)?)\s*(?:percent|per cent)", re.I)
_DECADE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})(\d0)s(?![A-Za-z0-9])")

MARKER = re.compile(r"⟦P:[^⟧]+⟧")
ENGLISH_WORD = re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z-]{2,}(?![A-Za-z])")
URL_LIKE = re.compile(r"https?://\S+|\b[\w-]+\.(?:com|org|edu|net|gov|io)\b|\bcc-by[\w-]*", re.I)
ROMAN_NUMERAL = re.compile(r"^[ivxlcdm]+$")
CJK = re.compile(r"[一-鿿]")
QUOTED = re.compile(r"[“\"]([^“”\"]{3,}?)[”\"]")
SINGLE_LETTER = re.compile(r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])")
LOWER_WORD = re.compile(r"(?<![A-Za-z])[a-z]+(?![A-Za-z])")
#: Lower-case English function words: every English sentence has them; names,
#: code, formulas and example strings almost never do.
FUNCTION_WORDS = frozenset(
    "the of and to in is are was were be been that this these those for with as on by an which from at or not "
    "we it its can will has have our their into than then when where there each such also".split())
LATIN = re.compile(r"[A-Za-z]")

_AUTHOR_INITIALS = re.compile(r"\b[A-Z]\.\s?(?:[A-Z]\.\s?)*[A-Z][a-z]{2,}")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_QUOTED_TITLE = re.compile(r"[“”\"].{10,}?[“”\"]", re.S)
_COPYRIGHT = re.compile(r"^\s*(?:©|\(c\))", re.I)

NEEDS_HUMAN = "needs_human"
DISMISSED_BY_RULE = "dismissed_by_rule"
DISMISSED_BY_LEDGER = "dismissed_by_ledger"
#: A model said this is not a defect. Counted, annotated, and by default still
#: left on the human list: a model asserting there is no problem is not evidence
#: that there is none (AGENTS.md section 5).
DISMISSED_BY_MODEL = "dismissed_by_model"

#: A term seen in this share of all units is document furniture - a running
#: footer, the authors' names - not a translation defect.
DEFAULT_TERM_DOCUMENT_FREQUENCY = 0.02
#: Below this many units a share means nothing: in a ten-unit document every
#: term that appears at all clears any percentage cutoff.
MIN_UNITS_FOR_FREQUENCY = 200
#: And a term has to actually recur before it can be called furniture.
MIN_OCCURRENCES_FOR_FURNITURE = 5
#: Above this Latin share a unit is not Chinese prose with a leaked word; it is
#: either a reference or an untranslated unit, and is judged as one.
DEFAULT_PROSE_LATIN_SHARE = 0.40
#: At or above this Latin share a unit did not come back translated.
DEFAULT_UNTRANSLATED_LATIN_SHARE = 0.50
#: Below this much Latin text the source unit is too short to judge.
MIN_SOURCE_LATIN_CHARS = 40
#: Function words an untranslated passage must still contain.
MIN_FUNCTION_WORDS = 2

SAMPLE_SIZE = 10


def _normalise_numbers(text: str) -> str:
    return _PERCENT_WORD.sub(r"\1%", _THOUSANDS_GAP.sub("", text))


def _number_spellings(text: str) -> list[str]:
    """Source spellings Chinese renders with different digits.

    ``1940s`` becomes ``20世纪40年代``: the century is the leading digits plus
    one and the decade survives. Offering that as an alternative reading lets
    the inventories match without loosening the comparison itself.
    """
    spellings = [text]
    if _DECADE.search(text):
        spellings.append(_DECADE.sub(lambda m: f" {int(m.group(1)) + 1} {m.group(2)} ", text))
    return spellings


def number_inventory(text: str, *, glued: bool = True) -> Counter:
    pattern = GLUED_NUMBER if glued else STRICT_NUMBER
    return Counter(pattern.findall(_normalise_numbers(text)))


def explain_number_warning(source_text: str, translated_text: str) -> str | None:
    """Name the tokenisation artifact behind a number warning, if there is one.

    Returns the rule name, or None when the digits genuinely differ and a human
    has to look. The glued reading only ever adds numbers the strict reading
    could not see, so it cannot mask an inventory that really changed.
    """
    target = number_inventory(translated_text)
    for index, spelling in enumerate(_number_spellings(source_text)):
        if number_inventory(spelling) == target:
            return "glued_digit_tokenisation" if index == 0 else "decade_expansion"
    if _numbers_carried_as_chinese(source_text, translated_text):
        return "chinese_numerals"
    return None


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_NUMBER = re.compile(r"[零〇一二两三四五六七八九十百千万]+")
_CN_MONEY = re.compile(r"([零〇一二两三四五六七八九十百千万]+)(?:元|块)([一二三四五六七八九])(?:角|毛)(?:([一二三四五六七八九])分)?")
_MONTHS = {name: number for number, name in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_EN_UNITS = {word: n for n, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_EN_TENS = {word: 10 * n for n, word in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if word != "_"}
_EN_NUMBER = re.compile(r"\b(" + "|".join([*_EN_TENS, *_EN_UNITS]) + r")(?:[- ](" + "|".join(_EN_UNITS) + r"))?\b", re.I)
_MONTH_WORD = re.compile(r"\b(" + "|".join(_MONTHS) + r")\b", re.I)


def chinese_numeral_value(text: str) -> int | None:
    """``八`` -> 8, ``三十`` -> 30, ``二十一`` -> 21, ``一百零五`` -> 105; None when it is not a plain number."""
    total, section, digit = 0, 0, None
    for char in text:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit == 10000:
                total += (section + (digit or 0)) * unit
                section, digit = 0, None
            else:
                section += (1 if digit is None else digit) * unit
                digit = None
        else:
            return None
    return total + section + (digit or 0)


def _chinese_numbers(text: str) -> Counter:
    found: Counter = Counter()
    rest = text
    for match in _CN_MONEY.finditer(text):  # 一块八毛七分 -> 1.87
        whole = chinese_numeral_value(match.group(1))
        if whole is not None:
            found[f"{whole}.{_CN_DIGITS[match.group(2)]}{_CN_DIGITS[match.group(3)] if match.group(3) else ''}"] += 1
    rest = _CN_MONEY.sub(" ", text)
    for match in _CN_NUMBER.finditer(rest):
        value = chinese_numeral_value(match.group(0))
        if value is not None:
            found[str(value)] += 1
    return found


def _english_number_words(text: str) -> Counter:
    found: Counter = Counter()
    for match in _EN_NUMBER.finditer(text):
        first, second = match.group(1).lower(), (match.group(2) or "").lower()
        found[str(_EN_TENS.get(first, _EN_UNITS.get(first, 0)) + _EN_UNITS.get(second, 0))] += 1
    for match in _MONTH_WORD.finditer(text):
        found[str(_MONTHS[match.group(1).lower()])] += 1
    return found


def _numbers_carried_as_chinese(source_text: str, translated_text: str) -> bool:
    """Every source number is in the translation, as digits or as a Chinese numeral, and nothing was invented.

    Chinese writes ``$8`` as ``八美元`` and ``January 1`` as ``1月1日``; a check
    that only counts Arabic digits calls both a changed number. Two conditions
    keep a real mistake visible: each digit number of the source must be found
    in the translation (Chinese numerals are read as their values), and each
    digit number the translation adds must come from the source's own words
    (``Twenty`` -> 20, ``January`` -> 1). ``$8`` translated as ``九美元`` fails
    the first; ``8`` translated as ``9`` fails both.
    """
    source_digits = number_inventory(source_text)
    target_digits = number_inventory(translated_text)
    if source_digits == target_digits:
        return False  # nothing to explain
    available = target_digits + _chinese_numbers(translated_text)
    if any(available[number] < count for number, count in source_digits.items()):
        return False
    allowed = source_digits + _english_number_words(source_text)
    return all(allowed[number] >= count for number, count in target_digits.items())


def prose(text: str) -> str:
    """Strip what is preserved by design: protected markers and URLs."""
    return URL_LIKE.sub(" ", MARKER.sub(" ", text or ""))


def latin_share(text: str) -> float:
    latin, cjk = len(LATIN.findall(text)), len(CJK.findall(text))
    return latin / (latin + cjk) if latin + cjk else 1.0


def looks_like_reference(source_text: str) -> bool:
    """Is this source unit a bibliography entry or a copyright line?

    Judged on the source, whose shape does not depend on the target language,
    so a reference that stays English is not mistaken for a failed translation.
    """
    text = MARKER.sub(" ", source_text or "")
    if _COPYRIGHT.search(text):
        return True
    signals = (bool(_AUTHOR_INITIALS.search(text)), bool(_YEAR.search(text)), bool(_QUOTED_TITLE.search(text)))
    return sum(signals) >= 2


class DocumentProfile:
    """How often each English word survives into this document's translation.

    A running footer or an author's name appears on hundreds of pages; a term
    the translator dropped appears on a few. The cutoff is a share of units, so
    it calibrates itself to the document instead of to a hand-written list.
    """

    def __init__(self, rows: list[dict], *, frequency_cutoff: float = DEFAULT_TERM_DOCUMENT_FREQUENCY):
        self.total_units = len(rows)
        self.frequency_cutoff = frequency_cutoff
        counts: Counter = Counter()
        for row in rows:
            target = prose(row.get("translated_text") or "")
            if not CJK.search(target):
                continue
            counts.update({word.casefold() for word in ENGLISH_WORD.findall(target)})
        self.document_frequency = counts

    def is_furniture(self, term: str) -> bool:
        if self.total_units < MIN_UNITS_FOR_FREQUENCY:
            return False
        occurrences = self.document_frequency.get(term, 0)
        if occurrences < MIN_OCCURRENCES_FOR_FURNITURE:
            return False
        return occurrences / self.total_units > self.frequency_cutoff


def scan_retained_english(
    source_text: str,
    translated_text: str,
    profile: DocumentProfile,
    retained_terms: set[str],
    *,
    prose_latin_share: float = DEFAULT_PROSE_LATIN_SHARE,
) -> list[str]:
    """List English terms left in Chinese prose that nobody chose to keep.

    Skipped: acronyms the source spells in capitals, Roman numerals, document
    furniture, terms an operator has approved, and units too Latin to be prose
    - those are judged by :func:`find_untranslated_units` instead.
    """
    target = prose(translated_text)
    if not CJK.search(target) or latin_share(target) > prose_latin_share:
        return []
    source = MARKER.sub(" ", source_text or "")
    acronyms = {word.casefold() for word in ENGLISH_WORD.findall(source) if word.isupper()}
    terms: list[str] = []
    for word in ENGLISH_WORD.findall(target):
        key = word.casefold()
        if key in terms or key in acronyms or key in retained_terms:
            continue
        if ROMAN_NUMERAL.match(key) or profile.is_furniture(key):
            continue
        terms.append(key)
    return terms


def _quoted_in_source(target: str, source: str) -> str:
    """Drop quoted passages the source quotes too: an example sentence stays English on purpose."""
    quoted_source = {_quote_key(m.group(1)) for m in QUOTED.finditer(source)}
    return QUOTED.sub(lambda m: " " if _quote_key(m.group(1)) in quoted_source else m.group(0), target)


def _quote_key(text: str) -> str:
    # English puts the comma inside the quotes ("that,"), Chinese outside.
    return " ".join(text.split()).strip(" ,.;:!?，。；：！？")


def unit_is_untranslated(source_text: str, translated_text: str, *, latin_cutoff: float = DEFAULT_UNTRANSLATED_LATIN_SHARE) -> bool:
    """Did a prose unit come back in the source language?

    Requires enough English in the source to judge, and excludes bibliography
    entries, which stay English deliberately.

    Only English *sentences* count. What stays in Latin letters by design -
    names and acronyms the source capitalises, single-letter variables and
    initials, code, formulas, example strings, and sentences the source itself
    quotes - carries no function words, so a unit is reported only when its
    remaining English holds at least :data:`MIN_FUNCTION_WORDS` of them and
    still outweighs the Chinese. SQL keywords are upper-case and do not count.
    """
    source = MARKER.sub(" ", source_text or "")
    if looks_like_reference(source_text):
        return False
    if len(LATIN.findall(source)) < MIN_SOURCE_LATIN_CHARS:
        return _short_sentence_left_english(source, translated_text)
    target = _quoted_in_source(prose(translated_text), source)
    if CJK.search(target):
        # "课程讲师：Christopher Manning", "双向RNN": words the source writes
        # capitalised are not leftover English once the unit is in Chinese.
        proper = {word for word in ENGLISH_WORD.findall(source) if word[0].isupper()}
        target = ENGLISH_WORD.sub(lambda m: " " if m.group(0) in proper else m.group(0), target)
    target = SINGLE_LETTER.sub(" ", target)
    if sum(1 for word in LOWER_WORD.findall(target) if word in FUNCTION_WORDS) < MIN_FUNCTION_WORDS:
        return False
    return latin_share(target) >= latin_cutoff


#: A short unit is judged only when its translation has no Chinese at all and still
#: reads as an English sentence: a function word among this many lower-case words.
#: Headings and names are capitalised, so they never reach the count.
SHORT_MIN_FUNCTION_WORDS = 1
SHORT_MIN_LOWER_WORDS = 3


def _short_sentence_left_english(source: str, translated_text: str) -> bool:
    """A short unit (a list item, a caption) that came back as an English sentence.

    Too short for the share test, so only the unambiguous case counts: not one
    Chinese character in the translation, and what is left still carries
    English function words. A heading or a name ("Neural Networks",
    "Christopher Manning") has none and is never reported.
    """
    target = _quoted_in_source(prose(translated_text), source)
    if CJK.search(target):
        return False
    words = LOWER_WORD.findall(SINGLE_LETTER.sub(" ", target))
    function_words = sum(1 for word in words if word in FUNCTION_WORDS)
    return function_words >= SHORT_MIN_FUNCTION_WORDS and len(words) >= SHORT_MIN_LOWER_WORDS


def leaked_markup(source_text: str, translated_text: str) -> list[str]:
    """Markup tags the translation introduced. These render literally."""
    introduced = set(MARKUP_TAG.findall(translated_text or "")) - set(MARKUP_TAG.findall(source_text or ""))
    return sorted(introduced)


class ReviewLedger:
    """Human verdicts on individual warnings, valid only while the text stands.

    Keyed by unit and result hash: a retranslated unit has a new hash, so its
    dismissals lapse and the warning returns. A ledger that outlived the text
    it was written about would hide new defects.
    """

    def __init__(self, entries: list[dict] | None = None, retained_terms: set[str] | None = None):
        self.entries = list(entries or [])
        self.retained_terms = {term.casefold() for term in (retained_terms or set())}
        self._index = {
            (entry["unit_id"], entry.get("result_hash"), entry["category"]): entry
            for entry in self.entries
            if entry.get("verdict") == "dismiss"
        }

    @classmethod
    def load(cls, path: Path | None) -> "ReviewLedger":
        if path is None or not Path(path).is_file():
            return cls([])
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload.get("entries", []), set(payload.get("retained_terms", [])))

    @staticmethod
    def is_model_verdict(entry: dict) -> bool:
        return str(entry.get("reviewed_by") or "").startswith("model:")

    def dump(self, path: Path, *, note: str = "") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "note": note or "Human verdicts on quality warnings. A dismissal holds only while result_hash matches.",
            "retained_terms": sorted(self.retained_terms),
            "entries": self.entries,
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def dismissal(self, unit_id: str, result_hash: str | None, category: str) -> dict | None:
        return self._index.get((unit_id, result_hash, category))

    def stale_entries(self, rows: list[dict]) -> list[dict]:
        """Dismissals whose unit still exists but whose text has moved on."""
        current = {row["unit_id"]: row.get("result_hash") for row in rows}
        return [
            entry for entry in self.entries
            if entry.get("verdict") == "dismiss"
            and entry["unit_id"] in current
            and current[entry["unit_id"]] != entry.get("result_hash")
        ]


def _bucket(categories: dict, name: str) -> dict:
    return categories.setdefault(name, {
        "total": 0, DISMISSED_BY_RULE: 0, DISMISSED_BY_LEDGER: 0, DISMISSED_BY_MODEL: 0,
        NEEDS_HUMAN: 0, "rules": {},
    })


def triage_rows(
    rows: list[dict],
    *,
    ledger: ReviewLedger | None = None,
    profile: DocumentProfile | None = None,
    trust_model_verdicts: bool = False,
) -> dict:
    """Sort every warning into dismissed or needs_human, and add the new signals.

    ``residual`` is what a person actually has to read, and what delivery
    thresholds should be judged against instead of the raw warning counts.

    A model verdict from the ledger annotates its warning with the model's
    reason and quotes but, unless ``trust_model_verdicts`` is set, leaves the
    warning on the human list. It shortens the reading, not the list.
    """
    ledger = ledger or ReviewLedger([])
    profile = profile or DocumentProfile(rows)
    categories: dict[str, dict] = {}
    residual: list[dict] = []
    term_pages: dict[str, set[int]] = {}
    term_units: Counter = Counter()

    def dismissed(entry: dict, row: dict, category: str) -> tuple[bool, dict | None]:
        """Apply a ledger verdict. Returns (warning is cleared, model annotation)."""
        found = ledger.dismissal(row["unit_id"], row.get("result_hash"), category)
        if found is None:
            return False, None
        if not ReviewLedger.is_model_verdict(found):
            entry[DISMISSED_BY_LEDGER] += 1
            return True, None
        entry[DISMISSED_BY_MODEL] += 1
        annotation = {key: found[key] for key in ("reason", "evidence_source", "evidence_target", "reviewed_by") if key in found}
        if trust_model_verdicts:
            return True, annotation
        return False, annotation

    def record(row: dict, category: str, **extra) -> None:
        entry = _bucket(categories, category)
        entry["total"] += 1
        cleared, annotation = dismissed(entry, row, category)
        if cleared:
            return
        entry[NEEDS_HUMAN] += 1
        item = {
            "unit_id": row["unit_id"], "category": category,
            "pages": row.get("source_pages") or [], "result_hash": row.get("result_hash"), **extra,
        }
        if annotation:
            item["model_verdict"] = annotation
        residual.append(item)

    for row in rows:
        source_text = row.get("source_text") or ""
        translated_text = row.get("translated_text") or ""
        pages = row.get("source_pages") or []
        seen: set[str] = set()
        for issue in row.get("issues") or []:
            category = issue.get("category", "unknown")
            if issue.get("severity") == "critical" or category in seen:
                continue
            seen.add(category)
            entry = _bucket(categories, category)
            entry["total"] += 1
            cleared, annotation = dismissed(entry, row, category)
            if cleared:
                continue
            rule = explain_number_warning(source_text, translated_text) if category == "number" else None
            if rule and annotation is None:
                entry[DISMISSED_BY_RULE] += 1
                entry["rules"][rule] = entry["rules"].get(rule, 0) + 1
                continue
            entry[NEEDS_HUMAN] += 1
            item = {
                "unit_id": row["unit_id"], "category": category,
                "pages": pages, "result_hash": row.get("result_hash"),
            }
            if annotation:
                item["model_verdict"] = annotation
            residual.append(item)

        if unit_is_untranslated(source_text, translated_text):
            record(row, "untranslated_unit", latin_share=round(latin_share(prose(translated_text)), 3))
        tags = leaked_markup(source_text, translated_text)
        if tags:
            record(row, "leaked_markup", tags=tags)
        terms = scan_retained_english(source_text, translated_text, profile, ledger.retained_terms)
        if terms:
            record(row, "retained_english", terms=terms)
            for term in terms:
                term_units[term] += 1
                term_pages.setdefault(term, set()).update(pages)

    return {
        "categories": categories,
        "residual": residual,
        "retained_english_terms": [
            {"term": term, "units": count, "sample_pages": sorted(term_pages[term])[:SAMPLE_SIZE]}
            for term, count in term_units.most_common()
        ],
        "stale_ledger_entries": ledger.stale_entries(rows),
        "profile": {"total_units": profile.total_units, "term_frequency_cutoff": profile.frequency_cutoff},
    }
