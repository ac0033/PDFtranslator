"""The AI OS: decision node, Jev, repair loop, optimiser, pi bridge, tools and the job runner's outcomes."""
import json
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf
import pytest

from pdf_translation import jobstate
from pdf_translation.aios import decision as decision_module
from pdf_translation.aios import optimize as optimize_module
from pdf_translation.aios import pi_agent, supervisor
from pdf_translation.aios.decision import Action, Decision, decide
from pdf_translation.aios.jev import JevClient, JevError
from pdf_translation.aios.optimize import compare
from pdf_translation.aios.supervisor import Limits, repair
from pdf_translation.formats import arxiv
from pdf_translation.jobspec import ModelChoice

AGENT = ModelChoice(provider="deepseek", model="deepseek-flash")
ENGLISH = "The model learns the weights of the network from the data in each step."
ROWS = [
    {"unit_id": "u1", "state": "accepted", "source_text": ENGLISH, "translated_text": ENGLISH, "issues": []},
    {"unit_id": "u2", "state": "exhausted", "source_text": ENGLISH, "translated_text": None, "issues": []},
    {"unit_id": "u3", "state": "accepted", "source_text": "It costs 12 dollars.", "translated_text": "它花费 13 美元。",
     "issues": [{"category": "number", "severity": "major", "explanation": "number inventory changed"}]},
]


def major(kind, count=1, **extra):
    return {"kind": kind, "severity": "major", "count": count, "detail": kind, **extra}


# --- decision node ----------------------------------------------------------------------

def test_usability_is_decided_by_the_verdict_and_each_problem_gets_one_action():
    report = {"verdict": "not_deliverable",
              "major_problems": [major("untranslated_text"), major("unit_state_exhausted"),
                                 major("minor_issue_accumulation", category="number"), major("uncaptured_prose")],
              "residual_warnings": [{"unit_id": "u3", "category": "number"}]}
    result = decide(report, ROWS, fmt="html")
    assert not result.usable
    assert [(a.name, a.unit_ids) for a in result.actions] == [
        ("retranslate_units", ["u1"]), ("retranslate_units", ["u2"]), ("review_units", ["u3"]), ("agent", [])]
    assert "Translate every sentence into Chinese" in result.actions[0].instruction
    assert decide({"verdict": "deliverable_with_known_issues", "major_problems": []}, ROWS, fmt="pdf").usable


def test_page_review_is_offered_only_for_pdfs():
    report = {"verdict": "not_deliverable",
              "major_problems": [major("minor_issue_accumulation", category="layout_boundary_unverifiable", pages=[3])]}
    assert decide(report, ROWS, fmt="pdf").actions[0].name == "review_pages"
    assert decide(report, ROWS, fmt="latex").actions[0].name == "agent"


class FakeJev:
    def __init__(self, choice=("review_units", 0.9), real=0.9, broken=False):
        self.choice, self.real, self.broken, self.asked = choice, real, broken, []

    def repair_action(self, *, problem, options, examples):
        if self.broken:
            raise JevError("down")
        self.asked.append(sorted(options))
        return self.choice

    def warning_is_real(self, **kwargs):
        if self.broken:
            raise JevError("down")
        return self.real


def test_jev_chooses_among_the_allowed_actions_and_low_confidence_goes_to_the_agent():
    report = {"verdict": "not_deliverable", "major_problems": [major("minor_issue_accumulation", category="number")],
              "residual_warnings": [{"unit_id": "u3", "category": "number"}]}
    confident = FakeJev(("retranslate_units", 0.95))
    action = decide(report, ROWS, fmt="html", jev=confident).actions[0]
    assert (action.name, action.chosen_by) == ("retranslate_units", "jev")
    assert confident.asked == [["agent", "retranslate_units", "review_units"]]
    unsure = decide(report, ROWS, fmt="html", jev=FakeJev(("review_units", 0.3))).actions[0]
    assert (unsure.name, unsure.chosen_by) == ("agent", "jev-low-confidence")
    assert decide(report, ROWS, fmt="html", jev=FakeJev(broken=True)).actions[0].name == "review_units"


def test_jev_filters_warnings_it_judges_false_alarms():
    report = {"verdict": "not_deliverable", "major_problems": [major("minor_issue_accumulation", category="number")],
              "residual_warnings": [{"unit_id": "u3", "category": "number"}]}
    action = decide(report, ROWS, fmt="html", jev=FakeJev(real=0.1)).actions[0]
    assert action.unit_ids == [] and action.problem["jev_real_probability"] == {"u3": 0.1}


def test_the_jev_client_sends_typed_questions_retries_overload_and_counts_usage():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(529)
        question = next(iter(calls[-1]["questions"].values()))
        if question["type"] == "noul":
            answers = {"real": {"type": "noul", "noul": 0.83}}
        else:
            answers = {"action": {"type": "choice", "choice": "review_units", "confidence": 0.7}}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 100, "output_tokens": 5}})

    client = JevClient("k", transport=httpx.MockTransport(handler))
    client.http.headers["Authorization"] = "Bearer k"
    import pdf_translation.aios.jev as jev_module
    original_sleep = jev_module.time.sleep
    jev_module.time.sleep = lambda _: None
    try:
        assert client.warning_is_real(category="number", explanation="x", source="12", translation="13") == 0.83
        assert client.repair_action(problem=major("x"), options={"review_units": "a", "agent": "b"}, examples=[]) == ("review_units", 0.7)
    finally:
        jev_module.time.sleep = original_sleep
    assert calls[1]["model"] == "jev-latest" and calls[1]["questions"]["real"]["criteria"]["true"]
    assert set(calls[2]["questions"]["action"]["criteria"]) == {"review_units", "agent"}
    assert (client.usage.calls, client.usage.input_tokens) == (2, 200)
    refused = JevClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
    with pytest.raises(JevError, match="401"):
        refused.ask("state", {})


# --- the repair loop ----------------------------------------------------------------------

@dataclass
class FakeWorkspace:
    """Scripted acceptance reports; records what the loop asked for."""
    reports: list
    fmt: str = "html"
    budget_left: float = 1.0
    calls: list = field(default_factory=list)

    def acceptance(self):
        return self.reports[0]

    def rows(self):
        return ROWS

    def remaining(self):
        return self.budget_left

    def retranslate(self, unit_ids, instruction, editor="aios"):
        self.calls.append(("retranslate", tuple(unit_ids)))
        return {"applied": unit_ids, "refused": [], "pages": [], "cost_usd": 0.01}

    def review_units(self, unit_ids, instruction=None, editor="aios"):
        self.calls.append(("review", tuple(unit_ids)))
        return {"applied": [], "refused": [], "pages": [], "cost_usd": 0.01, "problems_found": 0}

    def render_and_accept(self, pages=None):
        self.calls.append(("render", tuple(pages or ())))
        if len(self.reports) > 1:
            self.reports.pop(0)
        return self.reports[0]


BROKEN = {"verdict": "not_deliverable", "major_problems": [major("untranslated_text")]}
FIXED = {"verdict": "deliverable", "major_problems": []}


def test_repair_stops_as_soon_as_acceptance_says_usable():
    ws = FakeWorkspace([BROKEN, FIXED])
    events = []
    outcome = repair(ws, agent_choice=AGENT, jev=None, emit=lambda kind, data: events.append(kind))
    assert (outcome.usable, outcome.rounds, outcome.stop_reason) == (True, 1, "usable")
    assert ws.calls == [("retranslate", ("u1",)), ("render", ())] and events == ["repair.round"]


def test_repair_gives_up_after_two_rounds_without_progress(monkeypatch):
    monkeypatch.setattr(supervisor, "run_agent", lambda *a, **k: {"ran": False, "reason": "not installed"})
    ws = FakeWorkspace([BROKEN, BROKEN, BROKEN, BROKEN])
    outcome = repair(ws, agent_choice=AGENT, jev=None, emit=lambda *a: None, limits=Limits(max_rounds=5))
    assert (outcome.usable, outcome.stop_reason, outcome.rounds) == (False, "no_progress", 2)


def test_repair_does_not_start_without_budget():
    ws = FakeWorkspace([BROKEN], budget_left=0.0)
    outcome = repair(ws, agent_choice=AGENT, jev=None, emit=lambda *a: None)
    assert (outcome.stop_reason, ws.calls) == ("budget", [])


def test_problems_the_rules_cannot_handle_go_to_the_agent(monkeypatch):
    handed = []

    def fake_agent(ws, choice, decision, limits, *, purpose, **kwargs):
        handed.append((purpose, [a.problem["kind"] for a in decision.for_agent()]))
        return {"ran": True, "changed": True, "pages": [2]}

    monkeypatch.setattr(supervisor, "run_agent", fake_agent)
    unknown = {"verdict": "not_deliverable", "major_problems": [major("latex_compile_failed")]}
    ws = FakeWorkspace([unknown, FIXED])
    outcome = repair(ws, agent_choice=AGENT, jev=None, emit=lambda *a: None)
    assert outcome.usable and handed == [("repair", ["latex_compile_failed"])] and ("render", (2,)) in ws.calls


def test_when_nothing_can_be_done_the_loop_ends_at_once(monkeypatch):
    monkeypatch.setattr(supervisor, "run_agent", lambda *a, **k: {"ran": False, "reason": "not installed"})
    ws = FakeWorkspace([{"verdict": "not_deliverable", "major_problems": [major("uncaptured_prose")]}])
    outcome = repair(ws, agent_choice=AGENT, jev=None, emit=lambda *a: None)
    assert (outcome.stop_reason, outcome.rounds) == ("nothing_to_do", 1)


# --- the optimiser ------------------------------------------------------------------------

def report(verdict="deliverable_with_known_issues", majors=0, residual=3):
    return {"verdict": verdict, "major_problems": [major("x")] * majors,
            "minor_issues": [{"category": "number", "needs_human": residual}]}


@pytest.mark.parametrize("after,keep", [
    (report(residual=1), True), (report(residual=3), True), (report(residual=4), False),
    (report("not_deliverable", majors=1), False), (report("deliverable", residual=0), True),
])
def test_an_optimised_version_is_kept_only_when_it_is_not_worse(after, keep):
    assert compare(report(), after).keep is keep


def test_a_worse_optimised_version_is_discarded_and_its_cost_stays_booked(tmp_path, monkeypatch):
    from pdf_translation.storage import StateStore

    store_path = tmp_path / "runs" / "job" / "state.sqlite"
    store = StateStore(store_path)
    store.begin_run("job", "i", "c")

    class Ws(FakeWorkspace):
        runs_root = tmp_path / "runs"
        job_dir = tmp_path
        aios_dir = tmp_path

        class config:
            run_id = "job"

        def spent(self):
            return round(StateStore(store_path).spent("job"), 6)

        def store(self):
            return StateStore(store_path)

        def rows(self):
            return ROWS

        def review_units(self, unit_ids, instruction=None, editor="aios"):
            from pdf_translation.schemas import Usage
            StateStore(store_path).record_usage("job", "review", Usage(cost_usd=0.02, usage_source="provider"), "agent_assist")
            with StateStore(store_path).connect() as con:
                con.execute("INSERT INTO events(run_id,unit_id,event,detail) VALUES('job','u3','changed','x')")
            return {"applied": ["u3"], "refused": [], "pages": [], "cost_usd": 0.02, "problems_found": 1}

    before = {**report(), "residual_warnings": [{"unit_id": "u3", "category": "number"}]}
    ws = Ws([before, report(residual=9), report()])
    monkeypatch.setattr(optimize_module, "run_agent", lambda *a, **k: {"ran": False})
    result = optimize_module.optimize(ws, agent_choice=AGENT, jev=None)
    assert result["keep"] is False and "more unresolved warnings" in result["reason"]
    with StateStore(store_path).connect() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE event='changed'").fetchone()[0] == 0  # store restored
    assert StateStore(store_path).spent("job") == pytest.approx(0.02)  # the money spent is still on the ledger


def test_the_optimiser_spends_nothing_when_there_is_nothing_to_improve():
    ws = FakeWorkspace([{"verdict": "deliverable", "major_problems": [], "minor_issues": []}])
    assert optimize_module.optimize(ws, agent_choice=AGENT, jev=None)["reason"] == "nothing left to improve"
    assert ws.calls == []


# --- the pi bridge -------------------------------------------------------------------------

def test_pi_runs_isolated_with_only_the_services_tools(tmp_path, monkeypatch):
    cli = tmp_path / "cli.js"
    cli.write_text("")
    monkeypatch.setattr(pi_agent, "pi_cli", lambda: cli)
    monkeypatch.setenv("MY_DEEPSEEK_KEY", "sk-x")
    choice = ModelChoice(provider="deepseek", model="deepseek-flash", api_key_env="MY_DEEPSEEK_KEY")
    command, env = pi_agent.PiSession(tmp_path, choice, budget_left=0.1)._command()
    for flag in ("--no-extensions", "--no-skills", "--no-context-files", "--no-session", "--no-approve", "--offline"):
        assert flag in command
    assert command[command.index("--tools") + 1].split(",") == list(pi_agent.TOOLS)
    assert command[command.index("--provider") + 1] == "deepseek" and "--api-key" not in command
    assert env["DEEPSEEK_API_KEY"] == "sk-x"  # the key reaches pi under the name pi expects, never on the command line
    assert env["PI_CODING_AGENT_DIR"].startswith(str(tmp_path)) and env["PDFTRANS_JOB_DIR"] == str(tmp_path)


def test_a_compatible_endpoint_is_described_to_pi_in_its_own_models_file(tmp_path, monkeypatch):
    cli = tmp_path / "cli.js"
    cli.write_text("")
    monkeypatch.setattr(pi_agent, "pi_cli", lambda: cli)
    choice = ModelChoice(provider="acme", model="acme-large", base_url="https://llm.acme.example/v1", api_key_env="ACME_KEY")
    command, _ = pi_agent.PiSession(tmp_path, choice, budget_left=0.1)._command()
    models = json.loads((tmp_path / "aios" / "pi-home" / "models.json").read_text(encoding="utf-8"))
    assert models["providers"]["acme"] == {"baseUrl": "https://llm.acme.example/v1", "api": "openai-completions",
                                           "apiKey": "$ACME_KEY", "models": [{"id": "acme-large"}]}
    assert command[command.index("--model") + 1] == "acme-large"


FAKE_PI = textwrap.dedent('''
    import json, sys

    def send(record):
        print(json.dumps(record), flush=True)

    cost = 0.0
    prompt = json.loads(sys.stdin.readline())
    send({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
    aborted = False
    for turn in range(int(sys.argv[1])):
        cost += 0.05
        send({"type": "tool_execution_end", "toolName": "units", "isError": False})
        send({"type": "turn_end"})
        while True:  # like pi, answer the client's cost poll before the next turn
            msg = json.loads(sys.stdin.readline())
            if msg["type"] == "abort":
                aborted = True
                break
            if msg["type"] == "get_session_stats":
                send({"id": msg["id"], "type": "response", "command": "get_session_stats", "success": True,
                      "data": {"tokens": {"input": 1000, "output": 100}, "cost": cost}})
                break
        if aborted:
            break
    if sys.argv[2] == "finish" and not aborted:
        send({"type": "tool_execution_end", "toolName": "finish", "isError": False})
    send({"type": "agent_settled"})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "get_session_stats":
            send({"id": msg["id"], "type": "response", "command": "get_session_stats", "success": True,
                  "data": {"tokens": {"input": 1000, "output": 100}, "cost": cost}})
''')


def fake_session(tmp_path, monkeypatch, turns: int, ending: str, budget: float):
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI, encoding="utf-8")
    (tmp_path / "aios").mkdir(exist_ok=True)
    session = pi_agent.PiSession(tmp_path, AGENT, budget_left=budget)
    monkeypatch.setattr(session, "_command", lambda: ([sys.executable, str(script), str(turns), ending], dict(__import__("os").environ)))
    return session


def test_a_pi_session_reports_finish_turns_and_cost(tmp_path, monkeypatch):
    result = fake_session(tmp_path, monkeypatch, 2, "finish", budget=1.0).run("fix it")
    assert result.finished and result.stop_reason == "finished" and result.turns == 2
    assert result.cost_usd == pytest.approx(0.10) and result.input_tokens == 1000


def test_a_pi_session_is_aborted_when_it_spends_past_the_budget(tmp_path, monkeypatch):
    result = fake_session(tmp_path, monkeypatch, 5, "settle", budget=0.07).run("fix it")
    assert result.stop_reason == "budget" and not result.finished


def test_the_ai_os_reports_what_to_install_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(pi_agent, "node_version", lambda: None)
    assert pi_agent.availability() == (False, pi_agent.availability()[1]) and "Node.js" in pi_agent.availability()[1]
    monkeypatch.setattr(pi_agent, "node_version", lambda: (20, 1, 0))
    assert "too old" in pi_agent.availability()[1]
    monkeypatch.setattr(pi_agent, "node_version", lambda: (24, 0, 0))
    monkeypatch.setattr(pi_agent, "pi_cli", lambda: None)
    assert "pdftrans setup" in pi_agent.availability()[1]


# --- arXiv -----------------------------------------------------------------------------------

def make_pdf(path: Path, text: str) -> Path:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    return path


def test_only_a_versioned_arxiv_identifier_on_the_pdf_counts(tmp_path):
    assert arxiv.find_identifier(make_pdf(tmp_path / "a.pdf", "arXiv:2401.01234v2 [cs.LG] 3 Jan 2024")) == "2401.01234v2"
    assert arxiv.find_identifier(make_pdf(tmp_path / "b.pdf", "see arXiv:2401.01234 for details")) is None
    assert arxiv.find_identifier(make_pdf(tmp_path / "c.pdf", "arXiv:hep-th/9901001v1")) == "hep-th/9901001v1"


def test_the_arxiv_route_needs_network_permission_and_a_real_source(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", "arXiv:2401.01234v2")
    assert "allow_network" in arxiv.try_source(pdf, tmp_path, allow_network=False).reason
    seen = []

    def pdf_only(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.5 only")

    decision = arxiv.try_source(pdf, tmp_path, allow_network=True, transport=httpx.MockTransport(pdf_only))
    assert not decision.use_source and "PDF only" in decision.reason
    assert str(seen[0].url) == "https://arxiv.org/e-print/2401.01234v2"
    assert "pdftrans/" in seen[0].headers["User-Agent"] and "@" not in seen[0].headers["User-Agent"]
    missing = arxiv.try_source(pdf, tmp_path, allow_network=True,
                               transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert not missing.use_source and "404" in missing.reason


# --- the job runner's outcomes ---------------------------------------------------------------

def runner_job(tmp_path, monkeypatch, *, reports, optimize_result=None, optimize_error=None, repaired=None):
    from scripts import run_job

    job_dir = tmp_path / "job-20260927-000000-abcdef"
    (job_dir / "work").mkdir(parents=True)
    (job_dir / "work" / "out.pdf").write_bytes(b"%PDF")
    (job_dir / "job.json").write_text(json.dumps({"format": "html", "request": {"deliver": {"optimized": True}},
                                                  "agent": AGENT.model_dump(mode="json")}), encoding="utf-8")

    class Ws:
        def __init__(self):
            self.current = reports[0]

        def acceptance(self):
            return self.current

        def outputs(self):
            return {"mono": job_dir / "work" / "out.pdf"}

        def spent(self):
            return 0.01

        def budget(self):
            return 1.0

        def store(self):
            raise RuntimeError("no store in this test")

        def render_and_accept(self, pages=None):
            return self.current

    ws = Ws()

    def fake_repair(workspace, **kwargs):
        workspace.current = repaired or reports[0]
        return supervisor.RepairOutcome(workspace.current["verdict"] != "not_deliverable", 1, "done")

    def fake_optimize(workspace, **kwargs):
        if optimize_error:
            raise optimize_error
        return optimize_result or {"keep": True, "reason": "not worse than the usable version"}

    monkeypatch.setattr(run_job, "repair", fake_repair)
    monkeypatch.setattr(run_job, "optimize", fake_optimize)
    monkeypatch.setattr(run_job.JevClient, "from_environment", classmethod(lambda cls: None))
    job = run_job.Job(job_dir)
    job.decide_and_deliver(ws)
    return job_dir, job


def test_a_usable_version_is_published_before_the_optimised_one(tmp_path, monkeypatch):
    job_dir, job = runner_job(tmp_path, monkeypatch, reports=[FIXED])
    kinds = [e["type"] for e in jobstate.read_events(job_dir)]
    assert kinds.index("delivery.usable") < kinds.index("delivery.optimized") and kinds[-1] == "job.finished"
    assert job.state["status"] == "succeeded" and job.state["recommended"] == "optimized"


def test_an_unrepairable_version_is_published_as_degraded_with_its_verdict(tmp_path, monkeypatch):
    job_dir, job = runner_job(tmp_path, monkeypatch, reports=[BROKEN])
    assert job.state["delivery"] == "degraded" and job.state["status"] == "succeeded"
    manifest = json.loads((job_dir / "deliveries" / "degraded" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == "not_deliverable" and "mono" in manifest["files"]


def test_a_failed_optimisation_keeps_the_usable_version(tmp_path, monkeypatch):
    job_dir, job = runner_job(tmp_path, monkeypatch, reports=[FIXED], optimize_error=RuntimeError("model down"))
    assert job.state["status"] == "succeeded" and job.state["delivery"] == "usable"
    assert "optimisation failed" in job.state["aios"]["optimize"]["reason"]


def test_a_worse_optimised_version_is_not_published(tmp_path, monkeypatch):
    job_dir, job = runner_job(tmp_path, monkeypatch, reports=[FIXED],
                              optimize_result={"keep": False, "reason": "the optimised version grades lower"})
    assert job.state["recommended"] == "usable" and not (job_dir / "deliveries" / "optimized").exists()


def test_the_jev_evaluation_fails_when_a_real_defect_would_be_dismissed(monkeypatch):
    from scripts import evaluate_jev

    class AlwaysFalseAlarm:
        usage = type("U", (), {"calls": 0, "input_tokens": 0, "output_tokens": 0})()

        def warning_is_real(self, **kwargs):
            return 0.1

    class Perfect(AlwaysFalseAlarm):
        def __init__(self):
            self.truth = {(c["category"], c["unit_id"], c["source"]): c["real"] for c in evaluate_jev.cases()}

        def warning_is_real(self, *, category, explanation, source, translation):
            return next(0.9 if real else 0.1 for (cat, _, src), real in self.truth.items() if cat == category and src == source)

    blind = evaluate_jev.evaluate(AlwaysFalseAlarm(), 0.5)
    assert not blind["passed"] and len(blind["real_defects_dismissed"]) == blind["real_defects"] == 19
    assert evaluate_jev.evaluate(Perfect(), 0.5)["passed"]
