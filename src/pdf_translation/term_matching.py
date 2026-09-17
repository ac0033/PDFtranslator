"""Shared source-term identity and whitespace-tolerant occurrence matching."""
import re


def normalized_term(term: str) -> str:
    normalized=" ".join(term.split()).casefold()
    if not normalized:
        raise ValueError("source term must not be blank")
    return normalized


def term_pattern(term: str) -> re.Pattern:
    words=normalized_term(term).split(" ")
    return re.compile(r"(?<![A-Za-z0-9_])"+r"\s+".join(re.escape(word) for word in words)+r"(?![A-Za-z0-9_])",re.IGNORECASE)



def normalize_target_spacing(text: str) -> str:
    """Ignore layout whitespace only at Latin/CJK script boundaries."""
    return re.sub(r"(?<=[A-Za-z])\s+(?=[\u3400-\u9fff])|(?<=[\u3400-\u9fff])\s+(?=[A-Za-z])", "", text)
