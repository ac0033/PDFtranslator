"""TypeSafe Jev: typed, calibrated judgments for the decision node.

Jev answers narrow questions with a probability instead of generated text.
The decision node asks it two kinds of question:

* ``warning_is_real`` (a Noul): is this automatic warning a real defect a
  reader would be hurt by, or a false alarm of the check? Used to decide which
  flagged units deserve repair effort, never to clear a warning in the
  acceptance report (a model saying there is no problem is not evidence).
* ``repair_action`` (a Choice): which of the actions available for this
  problem fits it best. A low-confidence answer sends the problem to the pi
  agent instead.

The key comes from ``TYPESAFE_API_KEY`` in the server's environment (or a
per-job header). Without it the decision node falls back to its rule table
and the triage rules that already exist; the job is otherwise unaffected.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
KEY_ENV = "TYPESAFE_API_KEY"
#: Below this confidence a repair choice is not trusted and the problem goes to the agent.
CHOICE_CONFIDENCE = 0.6
#: At or above this probability a warning is treated as a real defect worth repairing.
REAL_PROBLEM = 0.5
STATE_LIMIT = 6000


class JevError(RuntimeError):
    pass


@dataclass
class JevUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class JevClient:
    def __init__(self, api_key: str, *, url: str = API_URL, model: str = MODEL, timeout: float = 60.0, transport=None):
        self.url = url
        self.model = model
        self.usage = JevUsage()
        self.http = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"}, transport=transport)

    @classmethod
    def from_environment(cls, **kwargs) -> "JevClient | None":
        key = os.environ.get(KEY_ENV)
        return cls(key, **kwargs) if key else None

    def ask(self, state, questions: dict) -> dict:
        """One request: every question judged against the same state, in parallel on Jev's side."""
        body = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(4):
            response = self.http.post(self.url, json=body)
            if response.status_code in (429, 529) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                raise JevError(f"Jev refused ({response.status_code}): {response.text[:300]}")
            payload = response.json()
            usage = payload.get("usage") or {}
            self.usage.calls += 1
            self.usage.input_tokens += int(usage.get("input_tokens") or 0)
            self.usage.output_tokens += int(usage.get("output_tokens") or 0)
            return payload.get("answers") or {}
        raise JevError("Jev stayed overloaded")

    # --- the two questions -------------------------------------------------------------

    def warning_is_real(self, *, category: str, explanation: str, source: str, translation: str) -> float:
        state = {
            "check": {"category": category, "explanation": explanation},
            "source_english": source[:STATE_LIMIT],
            "translation_chinese": translation[:STATE_LIMIT],
            "notes": ("⟦P:...⟧ markers stand for formulas, citations or markup and are copied through unchanged; "
                      "their position may differ because Chinese word order differs."),
        }
        answers = self.ask(state, {"real": {
            "type": "noul",
            "instructions": (
                "An automatic check flagged `check` on this translation. Is it a real defect that would mislead "
                "or hurt a reader of `translation_chinese` - a wrong or missing number, a lost or changed meaning, "
                "a wrong technical term, English prose left untranslated - rather than a false alarm caused by how "
                "the check compares text (different number formatting, reordered markers, a proper noun, acronym "
                "or code identifier kept in English on purpose)?"),
            "criteria": {"true": "a real defect a reader would be hurt by",
                         "false": "a false alarm or a harmless, intentional difference"},
        }})
        return float((answers.get("real") or {}).get("noul", 0.5))

    def repair_action(self, *, problem: dict, options: dict[str, str], examples: list[dict]) -> tuple[str, float]:
        state = {"problem": {k: problem.get(k) for k in ("kind", "detail", "count", "category")},
                 "examples": examples[:5]}
        text = json.dumps(state, ensure_ascii=False)
        if len(text) > STATE_LIMIT:
            state["examples"] = examples[:2]
        answers = self.ask(state, {"action": {
            "type": "choice",
            "instructions": ("A translation job's acceptance check reported `problem`, with `examples` of affected "
                             "passages. Which repair action is most likely to remove the problem without harming "
                             "the rest of the translation?"),
            "criteria": options,
        }})
        answer = answers.get("action") or {}
        return str(answer.get("choice") or next(iter(options))), float(answer.get("confidence") or 0.0)
