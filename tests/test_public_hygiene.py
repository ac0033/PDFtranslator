"""What the repository may carry: the product, not the machine it was built on.

Words only this machine knows (private project names, directory names) live in
.notes/hygiene_private_words.txt, one per line; the file is ignored by Git, so
the words themselves never enter the repository.
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".jsonl", ".txt", ".bat", ".yml", ".yaml", ".cfg", ".ini", ".example", ""}

#: Allowed on purpose: a Windows system font used by rendering tests.
ALLOWED = [re.compile(r"C:/Windows/Fonts/[A-Za-z0-9_.-]+")]
FORBIDDEN = {
    "absolute user or drive path": re.compile(r"(?<![A-Za-z])(?:[A-Z]:[\\/](?!Windows/Fonts/)[A-Za-z0-9_\u4e00-\u9fff]|/Users/[a-z]|/home/[a-z]|C:\\\\Users)"),
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|anthropic\.com)[A-Za-z0-9-]+\.[A-Za-z]{2,}"),
    "API key shape": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
}


def tracked_text_files() -> list[Path]:
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout.split("\n")
    return [ROOT / n for n in names if n and Path(n).suffix.lower() in TEXT_SUFFIXES and (ROOT / n).is_file()]


def private_words() -> list[str]:
    path = ROOT / ".notes" / "hygiene_private_words.txt"
    if not path.is_file():
        return []
    return [w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]


def test_tracked_files_carry_no_machine_or_personal_details():
    problems = []
    words = private_words()
    for path in tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in ALLOWED:
            text = pattern.sub("", text)
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                problems.append(f"{path.relative_to(ROOT)}: {label}: {match.group(0)[:40]}")
        problems += [f"{path.relative_to(ROOT)}: private word" for w in words if w.lower() in text.lower()]
    assert not problems, "\n".join(problems[:30])


def test_private_and_assistant_directories_are_ignored_and_untracked():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.notes/" in ignore and "/.claude/" in ignore
    tracked = subprocess.run(["git", "ls-files", ".notes", ".claude"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert not tracked.strip()


def test_version_is_the_same_everywhere_and_has_a_changelog_entry():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "src/pdf_translation/__init__.py").read_text(encoding="utf-8")).group(1)
    assert project == package
    changelog = (ROOT / "docs/CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(project)}\b", changelog, re.M), f"docs/CHANGELOG.md has no entry for {project}"


@pytest.mark.parametrize("pattern,sample", [
    ("absolute user or drive path", "D:\\work\\private"),
    ("email address", "someone" + "@" + "corp.cn"),  # assembled so this file does not trip itself
    ("API key shape", "sk-" + "a" * 24),
])
def test_the_rules_catch_what_they_claim_to(pattern, sample):
    assert FORBIDDEN[pattern].search(sample)
