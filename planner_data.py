import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from bulletin import get_bulletin_data, get_bulletin_track
from config import BASE_DIR
from database import CourseDB
from db_files import get_db_files, get_existing_db_path
from course_codes import code_in_set, normalize_course_code
from utils import load_json_value


PLANNER_DB_PATH = os.environ.get("DKU_PLANNER_DB_PATH", os.path.join(BASE_DIR, "data", "planner.db"))
REVIEW_DIMENSIONS = (
    "teaching_quality",
    "exam_difficulty",
    "grading",
    "workload",
    "content_value",
    "accessibility",
    "pace",
)
DEFAULT_WEIGHTS = {
    "teaching_quality": 5,
    "exam_difficulty": 3,
    "grading": 4,
    "workload": 3,
    "content_value": 5,
    "accessibility": 4,
    "pace": 3,
}

CATEGORY_PRIORITY = {
    "Divisional Foundation Courses": 1.8,
    "Interdisciplinary Courses": 3.4,
    "Disciplinary Courses": 4.2,
    "Electives": 2.4,
}


def group_course_options(group: Dict[str, Any]) -> List[List[str]]:
    """Return bulletin rows as course options, preserving cross-listed aliases.

    A row such as ``COMPSCI 309 / STATS 302`` is one option with two valid
    identifiers, not two courses toward a choose-N requirement.
    """
    options: List[List[str]] = []
    for course in group.get("courses", []) or []:
        aliases = list(dict.fromkeys(
            normalize_course_code(code) for code in course.get("codes", []) if code
        ))
        if aliases and aliases not in options:
            options.append(aliases)
    if not options:
        options = [[normalize_course_code(code)] for code in group.get("course_codes", []) if code]
    return options


def option_is_completed(option: Iterable[str], completed: Set[str]) -> bool:
    return any(code_in_set(code, completed) for code in option)


def option_display(option: Iterable[str]) -> str:
    return " / ".join(dict.fromkeys(option))


def course_code_is_upper_level(code: str) -> bool:
    parts = normalize_course_code(code).split()
    if len(parts) < 2:
        return False
    match = re.match(r"(\d{3})", parts[1])
    return bool(match and int(match.group(1)) >= 300)


def option_is_upper_level(option: Iterable[str]) -> bool:
    """Return whether a course option has a 300-level-or-above alias."""
    return any(course_code_is_upper_level(code) for code in option)


def option_completed_at_upper_level(option: Iterable[str], completed: Set[str]) -> bool:
    """Count the registered alias, not a lower-level cross-listing, as upper-level."""
    return any(
        course_code_is_upper_level(code) and code_in_set(code, completed)
        for code in option
    )


def completed_option_count(group: Dict[str, Any], options: List[List[str]], completed: Set[str]) -> int:
    """Count completed options while respecting per-subject minimums."""
    done = [option for option in options if option_is_completed(option, completed)]
    subject_minimums = group.get("subject_minimums", {}) or {}
    if not subject_minimums:
        return len(done)
    required_total = int(group.get("min_count", sum(subject_minimums.values())) or 0)
    subject_deficit = 0
    for subject, minimum in subject_minimums.items():
        subject_count = sum(
            1 for option in done
            if any(code.split()[0] == subject for code in option if code)
        )
        subject_deficit += max(0, int(minimum) - subject_count)
    return max(0, min(len(done), required_total - subject_deficit))


def bulletin_equivalence_map() -> Dict[str, List[str]]:
    aliases: Dict[str, List[str]] = {}
    for track in get_bulletin_data().get("tracks", []):
        for group in track.get("requirement_groups", []):
            for option in group_course_options(group):
                if len(option) > 1:
                    for code in option:
                        aliases[code] = option
    return aliases


def unique_catalogs() -> List[Dict[str, Any]]:
    """Return one (the newest) course database for each academic term."""
    by_term: Dict[str, Dict[str, Any]] = {}
    for filename in get_db_files():
        path = get_existing_db_path(filename)
        if not path:
            continue
        metadata = CourseDB(path).get_search_options_metadata()
        term = str(metadata.get("term", "")).strip()
        if not term:
            continue
        item = {
            "file": filename,
            "path": path,
            "term": term,
            "career": metadata.get("career", "UGRD"),
            "synced_at": metadata.get("synced_at", ""),
        }
        current = by_term.get(term)
        if not current or (item["synced_at"], filename) > (current["synced_at"], current["file"]):
            by_term[term] = item
    return sorted(by_term.values(), key=lambda item: item["term"])


def current_catalog() -> Optional[Dict[str, Any]]:
    catalogs = unique_catalogs()
    return catalogs[-1] if catalogs else None


def build_major_requirement_lines(profile: Dict[str, Any], track: Optional[Dict[str, Any]] = None) -> str:
    """Serialize the selected bulletin track for the scheduler.

    Choice groups retain their minimum-course requirement instead of being
    flattened into an undifferentiated list. Completed courses reduce the
    outstanding count, while recommended electives remain available as major
    candidates for schedule ranking.
    """
    track = track or get_bulletin_track(profile.get("major_key", ""))
    if not track:
        return ""
    completed = {normalize_course_code(code) for code in profile.get("completed_courses", [])}
    lines: List[str] = []
    handled = set()
    for group in track.get("requirement_groups", []):
        if group.get("kind") == "recommended":
            continue
        options = group_course_options(group)
        codes = [code for option in options for code in option]
        handled.update(codes)
        remaining_options = [option for option in options if not option_is_completed(option, completed)]
        if group.get("kind") == "choice":
            completed_count = sum(1 for option in options if option_is_completed(option, completed))
            needed = max(0, int(group.get("min_count", 1) or 1) - completed_count)
            if needed and remaining_options:
                canonical = [option[0] for option in remaining_options]
                quoted = ",".join(json.dumps(code) for code in canonical)
                lines.append(f"n_in_m({len(canonical)},{min(needed, len(canonical))},{quoted})")
        else:
            lines.extend(option[0] for option in remaining_options)
    # Keep track electives in the major candidate pool without treating them
    # as outstanding requirements.
    for raw_code in track.get("course_pool", []):
        code = normalize_course_code(raw_code)
        if code and code not in handled and not code_in_set(code, completed):
            lines.append(code)
    return "\n".join(dict.fromkeys(lines))


def build_major_requirement_context(profile: Dict[str, Any], track: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the outstanding bulletin groups and category-aware course weights."""
    track = track or get_bulletin_track(profile.get("major_key", ""))
    if not track:
        return {"categories": {}, "priorities": {}, "groups": []}
    completed = {normalize_course_code(code) for code in profile.get("completed_courses", [])}
    track_preferences = profile.get("choice_preferences", {}).get(track.get("key", ""), {})
    if not isinstance(track_preferences, dict):
        track_preferences = {}
    categories: Dict[str, str] = {}
    priorities: Dict[str, float] = {}
    groups: List[Dict[str, Any]] = []
    for group in track.get("requirement_groups", []):
        category = group.get("category", "Major requirement")
        kind = group.get("kind", "required")
        group_key = group.get("key", "")
        options = group_course_options(group)
        codes = [code for option in options for code in option]
        for code in codes:
            categories[code] = category
            priorities[code] = CATEGORY_PRIORITY.get(category, 2.0) + (0.8 if kind != "recommended" else 0.0)
        preferred_codes = {
            normalize_course_code(code) for code in track_preferences.get(group_key, [])
        }
        preferred_options = [
            option for option in options if any(code in preferred_codes for code in option)
        ]
        expanded_preferred = {code for option in preferred_options for code in option}
        for code in expanded_preferred:
            priorities[code] += 4.5
        remaining_options = [option for option in options if not option_is_completed(option, completed)]
        remaining = [code for option in remaining_options for code in option]
        completed_count = completed_option_count(group, options, completed)
        if kind == "recommended":
            groups.append({
                "key": group_key, "category": category, "kind": kind, "needed": 0,
                "codes": remaining, "options": remaining_options,
                "summary": group.get("summary", "Recommended electives"),
                "preferred_codes": sorted(expanded_preferred & set(remaining)),
            })
            continue
        needed = (
            max(0, int(group.get("min_count", 1) or 1) - completed_count)
            if kind == "choice" else len(remaining_options)
        )
        if needed:
            groups.append({
                "key": group_key, "category": category, "kind": kind, "needed": needed,
                "codes": remaining, "options": remaining_options,
                "summary": group.get("summary", ""),
                "preferred_codes": sorted(expanded_preferred & set(remaining)),
            })
    groups_by_key = {group.get("key"): group for group in track.get("requirement_groups", [])}
    for rule in track.get("category_rules", []) or []:
        choice_groups = [groups_by_key.get(key) for key in rule.get("choice_group_keys", [])]
        choice_groups = [group for group in choice_groups if group]
        options = []
        for source_group in choice_groups:
            for option in group_course_options(source_group):
                if option not in options:
                    options.append(option)
        completed_count = sum(1 for option in options if option_is_completed(option, completed))
        needed = max(0, int(rule.get("choice_total_min_count", 0)) - completed_count)
        remaining_options = [option for option in options if not option_is_completed(option, completed)]
        if needed and remaining_options:
            groups.append({
                "key": f"{rule.get('category', 'category')}-combined-total",
                "category": rule.get("category", "Major requirement"),
                "kind": "choice_total",
                "needed": needed,
                "codes": [code for option in remaining_options for code in option],
                "options": remaining_options,
                "summary": rule.get("summary", "Combined choice total"),
                "preferred_codes": [],
            })
        upper_level_minimum = int(rule.get("upper_level_min_count", 0) or 0)
        if upper_level_minimum:
            upper_options = [
                [code for code in option if course_code_is_upper_level(code)]
                for option in options
            ]
            upper_options = [option for option in upper_options if option]
            completed_upper = sum(
                1 for option in upper_options if option_is_completed(option, completed)
            )
            upper_needed = max(0, upper_level_minimum - completed_upper)
            remaining_upper = [
                option for option in upper_options if not option_is_completed(option, completed)
            ]
            if upper_needed and remaining_upper:
                groups.append({
                    "key": f"{rule.get('category', 'category')}-upper-level",
                    "category": rule.get("category", "Major requirement"),
                    "kind": "choice_upper_level",
                    "needed": upper_needed,
                    "codes": [code for option in remaining_upper for code in option],
                    "options": remaining_upper,
                    "summary": f"Choose {upper_level_minimum} courses at 300-level or above",
                    "preferred_codes": [],
                })
    return {"categories": categories, "priorities": priorities, "groups": groups}


def _relation_course_codes(node: Any, active_relation: str = "") -> set[str]:
    """Collect prerequisite course leaves without treating co/anti-requisites as gateways."""
    if not isinstance(node, dict):
        return set()
    node_type = str(node.get("type", ""))
    relation = node_type if node_type in {"pre", "pre_or_co"} else active_relation
    if node_type in {"co", "anti"}:
        relation = node_type
    result: set[str] = set()
    if node_type == "course" and relation in {"pre", "pre_or_co"}:
        code = normalize_course_code(node.get("code", ""))
        if code:
            result.add(code)
    for item in node.get("items", []) or []:
        result.update(_relation_course_codes(item, relation))
    return result


def build_prerequisite_unlock_map(rows: Iterable[Any], target_codes: Iterable[str]) -> Dict[str, List[str]]:
    """Map each prerequisite to the distinct major courses it unlocks."""
    targets = {normalize_course_code(code) for code in target_codes}
    equivalencies = bulletin_equivalence_map()
    prerequisites_by_target: Dict[str, set[str]] = {}
    for row in rows:
        try:
            code = normalize_course_code(f"{row['subject']} {row['catalog_nbr']}")
            raw = row["requisite_parsed_json"]
        except (KeyError, TypeError, IndexError):
            continue
        if code not in targets:
            continue
        parsed = load_json_value(raw, {})
        tree = parsed.get("normalized_eval_tree") or parsed.get("eval_tree") or {}
        target = option_display(equivalencies.get(code, [code]))
        prerequisites_by_target.setdefault(target, set()).update(_relation_course_codes(tree))
    unlocked: Dict[str, List[str]] = {}
    for target, prerequisites in prerequisites_by_target.items():
        for code in prerequisites:
            unlocked.setdefault(code, []).append(target)
    return {code: sorted(set(targets)) for code, targets in unlocked.items()}


def build_prerequisite_unlock_counts(rows: Iterable[Any], target_codes: Iterable[str]) -> Dict[str, int]:
    return {code: len(targets) for code, targets in build_prerequisite_unlock_map(rows, target_codes).items()}


def review_match_score(aggregate: Dict[str, Any], preferences: Dict[str, int], preferred_pace: int = 3) -> Optional[float]:
    if not aggregate or not aggregate.get("review_count"):
        return None
    matches = []
    for dimension in REVIEW_DIMENSIONS:
        raw = aggregate.get("avg_" + dimension)
        if raw is None:
            continue
        rating = float(raw)
        fallback = preferred_pace if dimension == "pace" else DEFAULT_WEIGHTS[dimension]
        target = max(1, min(5, int(preferences.get(dimension, fallback))))
        matches.append(max(0.0, 1.0 - abs(rating - target) / 4.0))
    return round(sum(matches) / len(matches) * 100.0, 1) if matches else None


class PlannerStore:
    def __init__(self, path: str = PLANNER_DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.init()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    major_key TEXT NOT NULL DEFAULT '',
                    year_level INTEGER NOT NULL DEFAULT 0,
                    class_of INTEGER,
                    identity TEXT NOT NULL DEFAULT '',
                    completed_courses_json TEXT NOT NULL DEFAULT '[]',
                    preference_weights_json TEXT NOT NULL DEFAULT '{}',
                    choice_preferences_json TEXT NOT NULL DEFAULT '{}',
                    distribution_preferences_json TEXT NOT NULL DEFAULT '{}',
                    preferred_pace INTEGER NOT NULL DEFAULT 3,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_key TEXT PRIMARY KEY,
                    major_key TEXT NOT NULL DEFAULT '',
                    year_level INTEGER NOT NULL DEFAULT 0,
                    class_of INTEGER,
                    identity TEXT NOT NULL DEFAULT '',
                    completed_courses_json TEXT NOT NULL DEFAULT '[]',
                    preference_weights_json TEXT NOT NULL DEFAULT '{}',
                    choice_preferences_json TEXT NOT NULL DEFAULT '{}',
                    distribution_preferences_json TEXT NOT NULL DEFAULT '{}',
                    preferred_pace INTEGER NOT NULL DEFAULT 3,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_code TEXT NOT NULL,
                    instructor_name TEXT NOT NULL,
                    teaching_quality INTEGER NOT NULL,
                    exam_difficulty INTEGER NOT NULL,
                    grading INTEGER NOT NULL,
                    workload INTEGER NOT NULL,
                    content_value INTEGER NOT NULL,
                    accessibility INTEGER NOT NULL,
                    pace INTEGER NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reviews_course_idx ON reviews(course_code);
                CREATE INDEX IF NOT EXISTS reviews_course_instructor_idx ON reviews(course_code, instructor_name);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(profile)").fetchall()}
            if "choice_preferences_json" not in columns:
                conn.execute("ALTER TABLE profile ADD COLUMN choice_preferences_json TEXT NOT NULL DEFAULT '{}'")
            if "distribution_preferences_json" not in columns:
                conn.execute("ALTER TABLE profile ADD COLUMN distribution_preferences_json TEXT NOT NULL DEFAULT '{}'")
            conn.commit()

    def get_profile(self, profile_key: str = "local") -> Dict[str, Any]:
        profile_key = str(profile_key or "local")[:128]
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM profiles WHERE profile_key = ?", (profile_key,)
            ).fetchone()
            # Preserve an existing local-only profile once when upgrading from
            # the earlier singleton schema. Packaged/public deployments do not
            # include planner.db, so they begin without legacy personal data.
            if not row:
                profile_count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
                legacy = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
                if profile_count == 0 and legacy:
                    conn.execute(
                        """INSERT INTO profiles
                           (profile_key, major_key, year_level, class_of, identity,
                            completed_courses_json, preference_weights_json,
                            choice_preferences_json, distribution_preferences_json,
                            preferred_pace, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            profile_key, legacy["major_key"], legacy["year_level"],
                            legacy["class_of"], legacy["identity"],
                            legacy["completed_courses_json"], legacy["preference_weights_json"],
                            legacy["choice_preferences_json"],
                            legacy["distribution_preferences_json"],
                            legacy["preferred_pace"], legacy["updated_at"],
                        ),
                    )
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM profiles WHERE profile_key = ?", (profile_key,)
                    ).fetchone()
        if not row:
            return {
                "major_key": "", "year_level": 0, "class_of": "", "identity": "",
                "completed_courses": [], "preference_weights": dict(DEFAULT_WEIGHTS),
                "preferred_pace": 3, "choice_preferences": {}, "distribution_preferences": {},
            }
        try:
            completed = json.loads(row["completed_courses_json"] or "[]")
        except json.JSONDecodeError:
            completed = []
        try:
            weights = json.loads(row["preference_weights_json"] or "{}")
        except json.JSONDecodeError:
            weights = {}
        try:
            choice_preferences = json.loads(row["choice_preferences_json"] or "{}")
        except (json.JSONDecodeError, KeyError, IndexError):
            choice_preferences = {}
        try:
            distribution_preferences = json.loads(row["distribution_preferences_json"] or "{}")
        except (json.JSONDecodeError, KeyError, IndexError):
            distribution_preferences = {}
        # Values from the earlier importance-weight UI were 0-100. Start those
        # profiles from clear 1-5 target defaults instead of misreading 90 as 5.
        if any(float(value) > 5 for value in weights.values() if str(value).replace(".", "", 1).isdigit()):
            weights = {}
        return {
            "major_key": row["major_key"],
            "year_level": row["year_level"],
            "class_of": row["class_of"] or "",
            "identity": row["identity"],
            "completed_courses": [normalize_course_code(code) for code in completed],
            "preference_weights": {**DEFAULT_WEIGHTS, **weights},
            "preferred_pace": row["preferred_pace"],
            "choice_preferences": choice_preferences if isinstance(choice_preferences, dict) else {},
            "distribution_preferences": distribution_preferences if isinstance(distribution_preferences, dict) else {},
        }

    def save_profile(self, data: Dict[str, Any], profile_key: str = "local") -> Dict[str, Any]:
        profile_key = str(profile_key or "local")[:128]
        completed = []
        for raw in data.get("completed_courses", []):
            code = normalize_course_code(raw)
            if code and code not in completed:
                completed.append(code)
        weights = {
            key: max(1, min(5, int(data.get("preference_weights", {}).get(key, DEFAULT_WEIGHTS[key]))))
            for key in REVIEW_DIMENSIONS
        }
        choice_preferences = data.get("choice_preferences", {})
        if not isinstance(choice_preferences, dict):
            choice_preferences = {}
        distribution_preferences = {}
        for attr, raw_codes in (data.get("distribution_preferences", {}) or {}).items():
            if attr not in {"AH", "NS", "SS"}:
                continue
            values = []
            for raw_code in raw_codes if isinstance(raw_codes, list) else []:
                code = normalize_course_code(raw_code)
                if code and code not in values:
                    values.append(code)
            distribution_preferences[attr] = values
        now = datetime.now(timezone.utc).isoformat()
        values = (
            str(data.get("major_key", "")).strip(),
            max(0, min(4, int(data.get("year_level") or 0))),
            int(data["class_of"]) if str(data.get("class_of", "")).isdigit() else None,
            str(data.get("identity", "")).strip(),
            json.dumps(completed),
            json.dumps(weights),
            json.dumps(choice_preferences),
            json.dumps(distribution_preferences),
            weights["pace"],
            now,
        )
        with closing(self.connect()) as conn:
            conn.execute(
                """INSERT INTO profiles
                   (profile_key, major_key, year_level, class_of, identity, completed_courses_json,
                    preference_weights_json, choice_preferences_json, distribution_preferences_json,
                    preferred_pace, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile_key) DO UPDATE SET
                    major_key=excluded.major_key, year_level=excluded.year_level,
                    class_of=excluded.class_of, identity=excluded.identity,
                    completed_courses_json=excluded.completed_courses_json,
                    preference_weights_json=excluded.preference_weights_json,
                    choice_preferences_json=excluded.choice_preferences_json,
                    distribution_preferences_json=excluded.distribution_preferences_json,
                    preferred_pace=excluded.preferred_pace, updated_at=excluded.updated_at""",
                (profile_key, *values),
            )
            conn.commit()
        return self.get_profile(profile_key)

    def add_review(self, data: Dict[str, Any]) -> None:
        course_code = normalize_course_code(data.get("course_code", ""))
        instructor = " ".join(str(data.get("instructor_name", "")).strip().split())
        if not course_code or not instructor:
            raise ValueError("Course and instructor are required.")
        ratings = []
        for dimension in REVIEW_DIMENSIONS:
            value = int(data.get(dimension) or 0)
            if value < 1 or value > 5:
                raise ValueError("Every rating must be between 1 and 5.")
            ratings.append(value)
        with closing(self.connect()) as conn:
            conn.execute(
                f"""INSERT INTO reviews
                    (course_code, instructor_name, {', '.join(REVIEW_DIMENSIONS)}, comment, created_at)
                    VALUES (?, ?, {', '.join('?' for _ in REVIEW_DIMENSIONS)}, ?, ?)""",
                (course_code, instructor, *ratings, str(data.get("comment", "")).strip()[:1000],
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def aggregate_reviews(self, course_code: str = "") -> List[Dict[str, Any]]:
        where = "WHERE course_code = ?" if course_code else ""
        params = (normalize_course_code(course_code),) if course_code else ()
        averages = ", ".join(f"ROUND(AVG({d}), 2) AS avg_{d}" for d in REVIEW_DIMENSIONS)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""SELECT course_code, instructor_name, COUNT(*) AS review_count, {averages}
                    FROM reviews {where}
                    GROUP BY course_code, instructor_name
                    ORDER BY review_count DESC, course_code, instructor_name""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def aggregate_map(self) -> Dict[str, Dict[str, Any]]:
        result = {}
        for row in self.aggregate_reviews():
            result[f"{row['course_code']}|{row['instructor_name'].lower()}"] = row
        for row in self.course_aggregates():
            result[f"{row['course_code']}|"] = row
        return result

    def course_aggregates(self) -> List[Dict[str, Any]]:
        averages = ", ".join(f"ROUND(AVG({d}), 2) AS avg_{d}" for d in REVIEW_DIMENSIONS)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"""SELECT course_code, '' AS instructor_name, COUNT(*) AS review_count, {averages}
                    FROM reviews GROUP BY course_code ORDER BY review_count DESC, course_code"""
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_reviews(self, limit: int = 20) -> List[Dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def build_course_history() -> Dict[str, Dict[str, Any]]:
    history: Dict[str, Dict[str, Any]] = {}
    active = current_catalog()
    active_term = active["term"] if active else ""
    for catalog in unique_catalogs():
        db = CourseDB(catalog["path"])
        for row in db.search_courses(limit=20000):
            code = normalize_course_code(f"{row['subject']} {row['catalog_nbr']}")
            item = history.setdefault(code, {
                "course_code": code, "title": row["course_name"] or "",
                "terms": set(), "instructors": set(), "current_rows": [],
                "prerequisite_codes": set(), "attributes": set(), "units": 0.0,
            })
            item["terms"].add(catalog["term"])
            for instructor in str(row["instructor_names"] or "").split(","):
                if instructor.strip():
                    item["instructors"].add(instructor.strip())
            if catalog["term"] == active_term:
                item["current_rows"].append(row)
            parsed = load_json_value(row["requisite_parsed_json"], {})
            tree = parsed.get("normalized_eval_tree") or parsed.get("eval_tree") or {}
            item["prerequisite_codes"].update(_relation_course_codes(tree))
            attr_values = str(row["course_attribute_values"] or "").upper()
            for token, marker in (
                ("AH", "AH"), ("SS", "SS"), ("NS", "NS"), ("QR", "QR"),
                ("DVSN-ARTHUM", "AH"), ("DVSN-SOCSCI", "SS"), ("DVSN-NATSCI", "NS"),
            ):
                if token in attr_values:
                    item["attributes"].add(marker)
            try:
                item["units"] = max(item["units"], float(row["units"] or 0))
            except (TypeError, ValueError):
                pass
    return history


def completed_attribute_units(profile: Dict[str, Any], history: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """Allocate completed courses across AH/SS/NS/QR without double counting."""
    history = history or build_course_history()
    dimensions = ("AH", "SS", "NS", "QR")
    states = {(0, 0, 0, 0)}
    for raw_code in profile.get("completed_courses", []):
        item = history.get(normalize_course_code(raw_code), {})
        options = [dimension for dimension in dimensions if dimension in item.get("attributes", set())]
        units_half = max(0, int(round(float(item.get("units", 0) or 0) * 2)))
        if not options or not units_half:
            continue
        next_states = set(states)
        for state in states:
            for option in options:
                values = list(state)
                index = dimensions.index(option)
                values[index] = min(8, values[index] + units_half)
                next_states.add(tuple(values))
        states = next_states
    best = max(states, key=lambda state: (sum(value >= 8 for value in state), sum(state)))
    return {dimension: best[index] / 2.0 for index, dimension in enumerate(dimensions)}


def build_recommendations(profile: Dict[str, Any], store: PlannerStore, limit: int = 12) -> List[Dict[str, Any]]:
    track = get_bulletin_track(profile.get("major_key", ""))
    if not track:
        return []
    completed = {normalize_course_code(code) for code in profile.get("completed_courses", [])}
    history = build_course_history()
    reviews = {row["course_code"]: row for row in store.course_aggregates()}
    context = build_major_requirement_context(profile, track)
    categories = context["categories"]
    required_codes = {
        code for group in context["groups"] if group["kind"] != "recommended"
        for code in group["codes"]
    }
    eligible_codes = required_codes | {
        code for group in context["groups"] if group["kind"] == "recommended"
        for code in group["codes"]
    }
    unlock_map: Dict[str, List[str]] = {}
    equivalencies = bulletin_equivalence_map()
    target_codes = {normalize_course_code(code) for code in track.get("course_pool", [])}
    for target_code in target_codes:
        for prerequisite in history.get(target_code, {}).get("prerequisite_codes", set()):
            target = option_display(equivalencies.get(target_code, [target_code]))
            prerequisite_aliases = equivalencies.get(prerequisite, [prerequisite])
            for prerequisite_alias in prerequisite_aliases:
                unlock_map.setdefault(prerequisite_alias, []).append(target)
    unlock_map = {code: sorted(set(targets)) for code, targets in unlock_map.items()}
    preferred_codes = {
        code for group in context["groups"] for code in group.get("preferred_codes", [])
    }
    recommendation_options: List[List[str]] = []
    for group in context["groups"]:
        for option in group.get("options", []) or [[code] for code in group.get("codes", [])]:
            normalized = list(dict.fromkeys(normalize_course_code(code) for code in option if code))
            if normalized and normalized not in recommendation_options:
                recommendation_options.append(normalized)

    recommendations = []
    for option in recommendation_options:
        if not set(option) & eligible_codes or option_is_completed(option, completed):
            continue
        histories = [history.get(code, {}) for code in option]
        current_aliases = [code for code in option if history.get(code, {}).get("current_rows")]
        code = current_aliases[0] if current_aliases else option[0]
        course = {
            "title": next((item.get("title", "") for item in histories if item.get("title")), ""),
            "terms": set().union(*(item.get("terms", set()) for item in histories)),
            "instructors": set().union(*(item.get("instructors", set()) for item in histories)),
            "current_rows": [row for item in histories for row in item.get("current_rows", [])],
        }
        aggregate = max(
            (reviews.get(alias, {}) for alias in option),
            key=lambda item: item.get("review_count", 0), default={},
        )
        match = review_match_score(aggregate, profile["preference_weights"], profile["preferred_pace"])
        offered_now = bool(course["current_rows"])
        category = next((categories.get(alias) for alias in option if categories.get(alias)), "Electives")
        category_score = CATEGORY_PRIORITY.get(category, 2.0) * 20
        is_required = bool(set(option) & required_codes)
        unlocked = sorted(set().union(*(set(unlock_map.get(alias, [])) for alias in option)))
        is_preferred = bool(set(option) & preferred_codes)
        score = category_score + (20 if is_required else 0) + (25 if offered_now else 0)
        score += len(course["terms"]) * 2 + min(len(unlocked), 8) * 12
        if is_preferred:
            score += 45
        if match is not None:
            score += match / 10.0
        recommendations.append({
            "course_code": option_display(option),
            "search_code": code,
            "title": course["title"],
            "category": category,
            "required": is_required,
            "offered_now": offered_now,
            "term_count": len(course["terms"]),
            "instructors": sorted(course["instructors"]),
            "review_count": aggregate.get("review_count", 0),
            "match_score": match,
            "unlock_count": len(unlocked),
            "unlock_courses": unlocked,
            "preferred": is_preferred,
            "score": score,
        })
    recommendations.sort(key=lambda item: (-item["score"], item["course_code"]))
    return recommendations[:limit]


def profile_progress(profile: Dict[str, Any]) -> Dict[str, Any]:
    track = get_bulletin_track(profile.get("major_key", ""))
    if not track:
        return {"track": None, "completed": 0, "total": 0, "percent": 0, "groups": []}
    completed = {normalize_course_code(code) for code in profile.get("completed_courses", [])}
    track_preferences = profile.get("choice_preferences", {}).get(track.get("key", ""), {})
    if not isinstance(track_preferences, dict):
        track_preferences = {}
    category_groups: Dict[str, Dict[str, Any]] = {}
    for group in track.get("requirement_groups", []):
        category = group.get("category", "Major requirement")
        category_group = category_groups.setdefault(category, {
            "category": category, "completed": 0, "needed": 0,
            "course_codes": [], "completed_codes": [], "summaries": [],
            "is_recommended": True, "subgroups": [],
        })
        options = group_course_options(group)
        codes = [code for option in options for code in option]
        done_options = [option for option in options if option_is_completed(option, completed)]
        needed = group.get("min_count", len(options)) if group.get("kind") != "recommended" else 0
        effective_completed = completed_option_count(group, options, completed)
        preferred_codes = {
            normalize_course_code(code) for code in track_preferences.get(group.get("key", ""), [])
        }
        titles = {}
        for course in group.get("courses", []):
            for code in course.get("codes", []):
                titles[normalize_course_code(code)] = course.get("title", "")
        category_group["subgroups"].append({
            "key": group.get("key", ""),
            "kind": group.get("kind", "required"),
            "summary": group.get("summary", ""),
            "instruction": group.get("instruction", ""),
            "needed": needed,
            "remaining_needed": max(0, needed - effective_completed),
            "effective_completed": effective_completed,
            "subject_minimums": group.get("subject_minimums", {}),
            "completed_option_keys": [tuple(option) for option in done_options],
            "courses": [{
                "code": option[0],
                "display_code": option_display(option),
                "aliases": option,
                "title": next((titles.get(code, "") for code in option if titles.get(code)), ""),
                "completed": option_is_completed(option, completed),
                "preferred": any(code in preferred_codes for code in option),
            } for option in options],
        })
        category_group["course_codes"].extend(code for code in codes if code not in category_group["course_codes"])
        category_group["completed_codes"].extend(
            option_display(option) for option in done_options
            if option_display(option) not in category_group["completed_codes"]
        )
        summary = group.get("summary", "")
        if summary and summary not in category_group["summaries"]:
            category_group["summaries"].append(summary)
        if group.get("kind") == "recommended":
            continue
        category_group["is_recommended"] = False
        category_group["completed"] += min(effective_completed, needed)
        category_group["needed"] += needed

    for rule in track.get("category_rules", []) or []:
        category_group = category_groups.get(rule.get("category", ""))
        if not category_group:
            continue
        choice_keys = set(rule.get("choice_group_keys", []))
        choice_subgroups = [
            subgroup for subgroup in category_group["subgroups"]
            if subgroup.get("key") in choice_keys
        ]
        other_subgroups = [
            subgroup for subgroup in category_group["subgroups"]
            if subgroup.get("key") not in choice_keys and subgroup.get("kind") != "recommended"
        ]
        completed_choice_options = {
            option for subgroup in choice_subgroups
            for option in subgroup.get("completed_option_keys", [])
        }
        choice_total = int(rule.get("choice_total_min_count", 0) or 0)
        minimum_deficit = sum(
            max(0, int(subgroup.get("needed", 0)) - int(subgroup.get("effective_completed", 0)))
            for subgroup in choice_subgroups
        )
        credited_choices = min(
            len(completed_choice_options),
            max(0, choice_total - minimum_deficit),
        )
        upper_level_minimum = int(rule.get("upper_level_min_count", 0) or 0)
        completed_upper_level = sum(
            1 for option in completed_choice_options
            if option_completed_at_upper_level(option, completed)
        )
        upper_level_deficit = max(0, upper_level_minimum - completed_upper_level)
        credited_choices = min(
            credited_choices,
            max(0, choice_total - upper_level_deficit),
        )
        required_needed = sum(int(subgroup.get("needed", 0)) for subgroup in other_subgroups)
        required_completed = sum(
            min(int(subgroup.get("effective_completed", 0)), int(subgroup.get("needed", 0)))
            for subgroup in other_subgroups
        )
        category_group["needed"] = required_needed + choice_total
        category_group["completed"] = required_completed + credited_choices
        required_label = f"{required_needed} required course" + ("s" if required_needed != 1 else "")
        category_group["summaries"] = [required_label, rule.get("summary", "")]
        category_remaining = max(0, choice_total - credited_choices)
        for subgroup in choice_subgroups:
            other_minimum = sum(
                int(other.get("needed", 0)) for other in choice_subgroups if other is not subgroup
            )
            subgroup["preference_max"] = min(
                len([course for course in subgroup.get("courses", []) if not course.get("completed")]),
                max(0, choice_total - other_minimum),
            )
            subgroup["choice_category_key"] = rule.get("category", "")
            subgroup["choice_category_max"] = category_remaining
            subgroup["preference_instruction"] = (
                f"Select at least {subgroup['remaining_needed']} here; "
                f"up to {subgroup['preference_max']} preferences"
            )
    groups = []
    for category in (
        "Divisional Foundation Courses", "Interdisciplinary Courses",
        "Disciplinary Courses", "Electives", "Signature Work",
    ):
        item = category_groups.pop(category, None)
        if item:
            for subgroup in item["subgroups"]:
                subgroup.pop("completed_option_keys", None)
                subgroup.pop("effective_completed", None)
            item["summary"] = " · ".join(item.pop("summaries"))
            groups.append(item)
    for item in category_groups.values():
        for subgroup in item["subgroups"]:
            subgroup.pop("completed_option_keys", None)
            subgroup.pop("effective_completed", None)
        item["summary"] = " · ".join(item.pop("summaries"))
        groups.append(item)
    total = sum(group["needed"] for group in groups)
    done = sum(group["completed"] for group in groups)
    return {
        "track": track, "completed": done, "total": total,
        "percent": round(done / total * 100) if total else 0,
        "groups": groups,
    }
