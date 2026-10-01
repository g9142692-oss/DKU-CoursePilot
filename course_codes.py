"""Shared normalization and equivalence rules for DKU course codes."""

import re
from typing import Iterable, Set


# Official cross-listings plus common student shorthand seen in saved profiles.
EQUIVALENT_COURSE_GROUPS = (
    frozenset({"CCORE 101", "GCHINA 101", "CC 101"}),
    frozenset({"CCORE 201", "GLOCHALL 201", "CC 201"}),
    frozenset({"CCORE 202", "ETHLDR 201", "CC 202"}),
)

_SHORTHAND_CANONICAL = {
    "CC 101": "CCORE 101",
    "CC 201": "CCORE 201",
    "CC 202": "CCORE 202",
}


def normalize_course_code(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    match = re.fullmatch(r"([A-Z]+)\s*([0-9][0-9A-Z]*)", text)
    if match:
        text = f"{match.group(1)} {match.group(2)}"
    return _SHORTHAND_CANONICAL.get(text, text)


def equivalent_course_codes(value: str) -> Set[str]:
    code = normalize_course_code(value)
    for group in EQUIVALENT_COURSE_GROUPS:
        normalized_group = {normalize_course_code(item) for item in group}
        if code in normalized_group:
            return normalized_group
    return {code}


def code_in_set(code: str, pool: Iterable[str]) -> bool:
    normalized_pool = {normalize_course_code(item) for item in pool}
    return bool(equivalent_course_codes(code) & normalized_pool)
