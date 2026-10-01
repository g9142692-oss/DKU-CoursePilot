#!/usr/bin/env python3
"""Extract major requirement course groups from the 2025-2026 DKU bulletin PDF."""

import argparse
import json
import re
from pathlib import Path

import pdfplumber


TRACKS = [
    (95, 97, "Applied Mathematics and Computational Sciences", "Computer Science"),
    (98, 102, "Applied Mathematics and Computational Sciences", "Mathematics"),
    (103, 107, "Arts and Media", "Arts"),
    (108, 113, "Arts and Media", "Media"),
    (114, 116, "Behavioral Science", "Economics"),
    (117, 118, "Behavioral Science", "Neuroscience"),
    (119, 121, "Behavioral Science", "Psychology"),
    (123, 125, "Computation and Design", "Computer Science"),
    (126, 128, "Computation and Design", "Digital Media"),
    (129, 133, "Computation and Design", "Social Policy"),
    (134, 135, "Cultures and Societies", "Cultural Anthropology"),
    (136, 138, "Cultures and Societies", "Sociology"),
    (140, 144, "Data Science", "Data Science"),
    (145, 147, "Environmental Science", "Biogeochemistry"),
    (148, 150, "Environmental Science", "Biology"),
    (151, 153, "Environmental Science", "Chemistry"),
    (154, 157, "Environmental Science", "Public Policy"),
    (158, 160, "Global China Studies", "Global China Studies"),
    (161, 164, "Global Health", "Biology"),
    (165, 170, "Global Health", "Public Policy"),
    (171, 172, "Humanities", "Creative Writing and Translation"),
    (173, 174, "Humanities", "Literature"),
    (175, 176, "Humanities", "Philosophy and Religion"),
    (177, 179, "Humanities", "World History"),
    (180, 181, "Materials Science", "Chemistry"),
    (182, 187, "Materials Science", "Physics"),
    (188, 189, "Molecular Bioscience", "Biogeochemistry"),
    (190, 191, "Molecular Bioscience", "Biophysics"),
    (192, 194, "Molecular Bioscience", "Cell and Molecular Biology"),
    (195, 196, "Molecular Bioscience", "Genetics and Genomics"),
    (197, 199, "Molecular Bioscience", "Neuroscience"),
    (200, 201, "Philosophy, Politics, and Economics", "Economic History"),
    (202, 203, "Philosophy, Politics, and Economics", "Philosophy"),
    (204, 205, "Philosophy, Politics, and Economics", "Political Science"),
    (206, 207, "Philosophy, Politics, and Economics", "Public Policy"),
    (209, 211, "Quantitative Political Economy", "Economics"),
    (212, 213, "Quantitative Political Economy", "Political Science"),
    (214, 215, "Quantitative Political Economy", "Public Policy"),
]

CATEGORIES = [
    "Divisional Foundation Courses",
    "Interdisciplinary Courses",
    "Disciplinary Courses",
    "Signature Work",
    "Electives",
]

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Bulletin course codes often have a superscript footnote number glued to the
# catalog number in extracted text (for example ``PHIL 210126`` means PHIL 210
# plus footnote 126). Capture the 2-3 digit catalog and discard that suffix.
COURSE_RE = re.compile(r"\b([A-Z]{2,})\s*([0-9]{2,3}[A-Z]?)(?:[0-9]{1,3})?(?=\b|/)")
SUBJECT_CORRECTIONS = {
    "EHTLDR": "ETHLDR", "ETHILDR": "ETHLDR",
    "POLISCI": "POLSCI", "HIS": "HIST",
}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip()


def validate_track_boundaries(pdf, specs: list[tuple]) -> None:
    """Fail early when a multi-track page range starts after its printed heading."""
    major_counts = {}
    for _, _, major, _ in specs:
        major_counts[major] = major_counts.get(major, 0) + 1
    for index, (start, end, major, track) in enumerate(specs):
        if start > end:
            raise ValueError(f"Invalid bulletin range for {major} / {track}: {start}-{end}")
        if index and specs[index - 1][2] == major and specs[index - 1][1] >= start:
            raise ValueError(f"Overlapping bulletin ranges for {major} near page {start}")
        if major_counts[major] < 2:
            continue
        label = re.sub(r"[^a-z0-9]", "", f"{major}{track}".lower())
        top_lines = (pdf.pages[start - 1].extract_text_lines() or [])[:12]
        found = any(
            label in re.sub(r"[^a-z0-9]", "", line.get("text", "").lower())
            for line in top_lines
        )
        if not found:
            raise ValueError(
                f"Configured start page {start} does not contain the heading for {major} / {track}"
            )


def course_codes(value: str) -> list[str]:
    value = clean(value).upper().replace("CULA NTH", "CULANTH").replace("GCHIN A", "GCHINA")
    # Correct recurring subject-code OCR/typography errors in the bulletin.
    value = re.sub(r"\b(?:EHTLDR|ETHILDR)\b", "ETHLDR", value)
    value = re.sub(r"\bPOLISCI\b", "POLSCI", value)
    value = re.sub(r"\bHIS(?=\s*\d)", "HIST", value)
    codes = []
    for subject, catalog in COURSE_RE.findall(value):
        subject = SUBJECT_CORRECTIONS.get(subject, subject)
        code = f"{subject} {catalog}"
        if code not in codes:
            codes.append(code)
    return codes


def instruction_count(value: str) -> int | None:
    text = clean(value).lower()
    if "choose" not in text:
        return None
    compact = re.sub(r"[^a-z0-9]", "", text)
    credit_minimum = re.search(r"choose(?:atleast)?(\d+)credits?", compact)
    if credit_minimum:
        return max(1, (int(credit_minimum.group(1)) + 3) // 4)
    digit = re.search(r"choose(?:atleast)?(\d+)(?:courses?)?", compact)
    if digit:
        return int(digit.group(1))
    if re.search(r"choose(?:atleast)?one[a-z]*andone[a-z]*", compact):
        return 2
    word = re.search(r"choose(?:atleast)?(one|two|three|four|five|six|seven|eight|nine|ten)", compact)
    return NUMBER_WORDS.get(word.group(1)) if word else 1


def detect_category(text: str, current: str) -> str:
    normalized = clean(text).lower()
    found = current
    last_pos = -1
    for category in CATEGORIES:
        labels = [category]
        if category.endswith("s"):
            labels.append(category[:-1])
        for label in labels:
            words = [re.escape(part) for part in label.lower().split()]
            pattern = r"(?<![a-z])" + r"\s*".join(words) + r"(?![a-z])"
            matches = list(re.finditer(pattern, normalized))
            if matches and matches[-1].start() > last_pos:
                found = category
                last_pos = matches[-1].start()
    return found


def new_group(category: str, instruction: str = "", kind: str = "required", min_count: int = 0) -> dict:
    return {
        "key": "",
        "category": category or "Major Requirements",
        "instruction": clean(instruction),
        "kind": kind,
        "min_count": min_count,
        "course_codes": [],
        "summary": "",
        "courses": [],
    }


def finalize(groups: list[dict]) -> list[dict]:
    result = []
    for index, group in enumerate(groups):
        deduped_courses = []
        seen_options = set()
        for course in group["courses"]:
            option = tuple(course.get("codes", []))
            if option and option not in seen_options:
                seen_options.add(option)
                deduped_courses.append(course)
        group["courses"] = deduped_courses
        codes = []
        for course in group["courses"]:
            for code in course["codes"]:
                if code not in codes:
                    codes.append(code)
        if not codes:
            continue
        group["course_codes"] = codes
        option_count = len(group["courses"])
        if group["kind"] == "required":
            group["min_count"] = option_count
        group["key"] = f"{slug(group['category'])}-{index + 1}"
        group["summary"] = (
            f"Choose {group['min_count']} of {option_count}" if group["kind"] == "choice"
            else ("Recommended electives" if group["kind"] == "recommended" else "Required courses")
        )
        result.append(group)
    return result


def repair_amcs_computer_science_groups(groups: list[dict]) -> list[dict]:
    """Preserve headings that pdfplumber merges into adjacent AMCS tables."""
    course_lookup = {}
    for group in groups:
        for course in group.get("courses", []):
            for code in course.get("codes", []):
                course_lookup.setdefault(code, course)
    specs = [
        ("Divisional Foundation Courses", "choice", 1, ["MATH 101", "MATH 105"], "Choose one of the following two Math courses"),
        ("Divisional Foundation Courses", "choice", 2, ["BIOL 110", "CHEM 110", "PHYS 121", "INTGSCI 205"], "Choose two of the following courses"),
        ("Interdisciplinary Courses", "choice", 1, ["COMPSCI 101", "STATS 102"], "Choose one course from the following two courses"),
        ("Interdisciplinary Courses", "required", 4, ["MATH 201", "MATH 202", "MATH 206", "MATH 302"], "Complete the following courses"),
        ("Disciplinary Courses", "required", 4, ["COMPSCI 201", "COMPSCI 203", "COMPSCI 205", "COMPSCI 308"], "Complete the following courses"),
        ("Disciplinary Courses", "choice", 1, ["COMPSCI 306", "COMPSCI 310", "COMPSCI 311"], "Choose one of the following three courses"),
    ]
    repaired = []
    for category, kind, minimum, codes, instruction in specs:
        group = new_group(category, instruction, kind, minimum)
        group["courses"] = [course_lookup[code] for code in codes if code in course_lookup]
        repaired.append(group)
    elective_courses = []
    for group in groups:
        if group.get("category") == "Electives" or group.get("kind") == "recommended":
            elective_courses.extend(group.get("courses", []))
    if elective_courses:
        group = new_group("Electives", kind="recommended")
        group["courses"] = elective_courses
        repaired.append(group)
    return finalize(repaired)


def repair_qpe_public_policy_groups(groups: list[dict]) -> list[dict]:
    """Repair a source-layout error where the disciplinary heading follows its table."""
    course_lookup = {}
    for group in groups:
        for course in group.get("courses", []):
            for code in course.get("codes", []):
                course_lookup.setdefault(code, course)
    specs = [
        ("Divisional Foundation Courses", "required", 2, ["SOSC 102", "STATS 101"], "Complete the following courses"),
        ("Divisional Foundation Courses", "choice", 1, ["MATH 101", "MATH 105"], "Choose one of the following two courses"),
        ("Interdisciplinary Courses", "required", 4, ["ECON 101", "PPE 202", "POLECON 201", "SOSC 205"], "Complete the following courses"),
        ("Interdisciplinary Courses", "choice", 1, ["SOSC 302", "SOSC 314", "SOSC 320"], "Choose one of the following three courses"),
        ("Interdisciplinary Courses", "choice", 1, ["POLECON 301", "POLECON 302"], "Choose one of the following two courses"),
        ("Interdisciplinary Courses", "required", 1, ["POLECON 490"], "Complete the following course"),
        ("Disciplinary Courses", "required", 4, ["PUBPOL 101", "PUBPOL 301", "PUBPOL 303", "PUBPOL 315"], "Complete the following courses"),
    ]
    repaired = []
    for category, kind, minimum, codes, instruction in specs:
        group = new_group(category, instruction, kind, minimum)
        group["courses"] = [course_lookup[code] for code in codes if code in course_lookup]
        repaired.append(group)
    elective_courses = []
    for group in groups:
        if group.get("category") == "Electives" or group.get("kind") == "recommended":
            elective_courses.extend(group.get("courses", []))
    if elective_courses:
        group = new_group("Electives", kind="recommended")
        group["courses"] = elective_courses
        repaired.append(group)
    return finalize(repaired)


def annotate_humanities_constraints(groups: list[dict], track: str) -> tuple[list[dict], list[dict]]:
    """Model the Humanities two-list and upper-level disciplinary rules."""
    choice_groups = [
        group for group in groups
        if group.get("category") == "Disciplinary Courses" and group.get("kind") == "choice"
    ]
    for group in choice_groups:
        group["min_count"] = 2
        group["summary"] = f"Choose at least 2 of {len(group.get('courses', []))}"
        if track == "Philosophy and Religion":
            group["subject_minimums"] = {"PHIL": 1, "RELIG": 1}
            group["summary"] = "Choose at least 1 PHIL and 1 RELIG course"
    rules = []
    if len(choice_groups) == 2:
        rules.append({
            "category": "Disciplinary Courses",
            "choice_group_keys": [group["key"] for group in choice_groups],
            "choice_total_min_count": 5,
            "upper_level_min_count": 2,
            "summary": (
                "Complete 5 courses across both lists, with at least 2 from each list "
                "and at least 2 at 300-level or above"
            ),
        })
    return groups, rules


def extract_track(pdf, start: int, end: int, major: str, track: str) -> dict:
    groups: list[dict] = []
    current_category = "Major Requirements"
    current_group: dict | None = None

    def close_group() -> None:
        nonlocal current_group
        if current_group and current_group["courses"]:
            groups.append(current_group)
        current_group = None

    for page_number in range(start, end + 1):
        page = pdf.pages[page_number - 1]
        tables = page.find_tables()
        previous_bottom = 0.0
        if page_number == start:
            label_compact = re.sub(r"[^a-z0-9]", "", (major if major == track else f"{major}{track}").lower())
            for line in page.extract_text_lines() or []:
                line_compact = re.sub(r"[^a-z0-9]", "", line.get("text", "").lower())
                if label_compact and label_compact in line_compact:
                    previous_bottom = float(line.get("top", 0.0))
                    break
        for table in tables:
            if table.bbox[3] <= previous_bottom:
                continue
            # Category headings sit directly on the table border in the source
            # PDF. Include a small slice below the detected table top so words
            # such as "Interdisciplinary Courses" are not clipped away.
            heading_bottom = min(page.height, table.bbox[1] + 18)
            heading = page.crop((0, previous_bottom, page.width, heading_bottom)).extract_text() or ""
            current_category = detect_category(heading, current_category)
            if current_group and current_group["category"] != current_category:
                close_group()
            rows = table.extract() or []
            for row in rows:
                cells = [clean(cell) for cell in (row or [])]
                row_text = clean(" ".join(cells))
                if not row_text or ("course" in row_text.lower() and "credit" in row_text.lower() and "code" in row_text.lower()):
                    continue
                count = instruction_count(row_text)
                if count is not None:
                    close_group()
                    kind = "recommended" if current_category == "Electives" else "choice"
                    current_group = new_group(current_category, row_text, kind, count)
                    continue
                codes = course_codes(" ".join(cells[:2]))
                if not codes:
                    compact_row = re.sub(r"[^a-z0-9]", "", row_text.lower())
                    if "completethefollowing" in compact_row:
                        close_group()
                        kind = "recommended" if current_category == "Electives" else "required"
                        current_group = new_group(current_category, row_text, kind, 0)
                    continue
                title = cells[1] if len(cells) > 1 else ""
                credit = cells[-1] if cells else ""
                # Narrative requirement rows sometimes contain numbers such as
                # "300-level" and get merged into tables. They are not courses.
                if not title and not re.search(r"\d", credit):
                    continue
                if current_group is None:
                    kind = "recommended" if current_category == "Electives" else "required"
                    current_group = new_group(current_category, kind=kind)
                current_group["courses"].append({"codes": codes, "title": title, "credits": credit})
            previous_bottom = table.bbox[3]
        tail = page.crop((0, previous_bottom, page.width, page.height)).extract_text() or ""
        next_category = detect_category(tail, current_category)
        if next_category != current_category:
            close_group()
            current_category = next_category
    close_group()

    requirement_groups = finalize(groups)
    category_rules = []
    if major == "Applied Mathematics and Computational Sciences" and track == "Computer Science":
        requirement_groups = repair_amcs_computer_science_groups(requirement_groups)
    if major == "Quantitative Political Economy" and track == "Public Policy":
        requirement_groups = repair_qpe_public_policy_groups(requirement_groups)
    if major == "Humanities":
        requirement_groups, category_rules = annotate_humanities_constraints(requirement_groups, track)
    pool = []
    for group in requirement_groups:
        for code in group["course_codes"]:
            if code not in pool:
                pool.append(code)
    label = major if major == track else f"{major} / {track}"
    return {
        "key": slug(label),
        "label": label,
        "major": major,
        "track": track,
        "requirement_groups": requirement_groups,
        "category_rules": category_rules,
        "course_pool": pool,
        "course_count": len(pool),
        "bulletin_pages": [start, end],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("output")
    args = parser.parse_args()
    with pdfplumber.open(args.pdf) as pdf:
        validate_track_boundaries(pdf, TRACKS)
        tracks = [extract_track(pdf, *spec) for spec in TRACKS]
    payload = {
        "source": "DKU Undergraduate Bulletin 2025-2026",
        "source_file": Path(args.pdf).name,
        "tracks": tracks,
        "rules": [],
        "error": "",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(tracks)} tracks and {sum(t['course_count'] for t in tracks)} track-course entries to {output}")


if __name__ == "__main__":
    main()
