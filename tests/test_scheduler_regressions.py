import unittest
import json
import os
import tempfile

from bulletin import get_bulletin_data
from course_codes import code_in_set, normalize_course_code
from planner_data import (
    PlannerStore,
    build_major_requirement_context,
    build_prerequisite_unlock_counts,
    build_prerequisite_unlock_map,
    completed_attribute_units,
    group_course_options,
    profile_progress,
    review_match_score,
)
from requisite_parser import parse_requisite_text
from scheduler import (
    Course,
    Meeting,
    ScheduleRequest,
    _eval_tree,
    _time_preference_penalty,
    check_graduation_progress,
    split_session_timelines,
)
from app import PROFILE_COOKIE_NAME, create_app


class SchedulerRegressionTests(unittest.TestCase):
    def test_common_core_shorthand_is_canonicalized(self):
        self.assertEqual(normalize_course_code("CC 101"), "CCORE 101")
        self.assertTrue(code_in_set("GCHINA 101", {"CC 101"}))

    def test_antirequisite_is_not_treated_as_a_prerequisite(self):
        parsed = parse_requisite_text(
            "Pre-requisite: MATH 101 or MATH 105, Anti-requisite: MATH 205"
        )
        tree = parsed["normalized_eval_tree"]
        request = ScheduleRequest(completed_courses={"MATH 105"})
        ok, _, _ = _eval_tree(tree, request, None)
        self.assertTrue(ok)

        request.completed_courses.add("MATH 205")
        ok, _, _ = _eval_tree(tree, request, None)
        self.assertFalse(ok)

    def test_requested_common_core_is_not_reported_missing_on_empty_result(self):
        request = ScheduleRequest(
            year_level=2,
            must_include=[Course.parse("CCORE 201")],
        )
        warnings = check_graduation_progress([], request)
        self.assertFalse(any("CCORE 201" in warning for warning in warnings))

    def test_time_preferences_cover_morning_and_evening_windows(self):
        meetings = [
            Meeting("Mo", 10 * 60, 11 * 60 + 15),
            Meeting("Mo", 17 * 60 + 30, 19 * 60 + 30),
        ]
        neutral = ScheduleRequest(avoid_early=0, avoid_evening=0)
        preferred = ScheduleRequest(avoid_early=83, avoid_evening=81)
        self.assertEqual(_time_preference_penalty(meetings, neutral), 0)
        self.assertGreater(_time_preference_penalty(meetings, preferred), 190)

    def test_midday_meetings_have_no_time_window_penalty(self):
        meetings = [Meeting("Tu", 12 * 60, 14 * 60 + 30)]
        request = ScheduleRequest(avoid_early=100, avoid_evening=100)
        self.assertEqual(_time_preference_penalty(meetings, request), 0)

    def test_amcs_cs_progress_uses_four_bulletin_categories(self):
        progress = profile_progress({
            "major_key": "applied-mathematics-and-computational-sciences-computer-science",
            "completed_courses": ["MATH 105", "STATS 102", "MATH 201", "MATH 202"],
        })
        self.assertEqual(
            [group["category"] for group in progress["groups"]],
            [
                "Divisional Foundation Courses",
                "Interdisciplinary Courses",
                "Disciplinary Courses",
                "Electives",
            ],
        )
        self.assertEqual([(g["completed"], g["needed"]) for g in progress["groups"][:3]], [(1, 3), (3, 5), (0, 5)])

    def test_session_timelines_are_split_and_full_term_is_shared(self):
        timeline = [
            {"title": "First", "session_class": "session-s1"},
            {"title": "Second", "session_class": "session-s2"},
            {"title": "Full", "session_class": "session-full"},
            {"title": "Blocked", "session_class": "session-blocked"},
        ]
        first, second = split_session_timelines(timeline)
        self.assertEqual([item["title"] for item in first], ["First", "Full", "Blocked"])
        self.assertEqual([item["title"] for item in second], ["Second", "Full", "Blocked"])

    def test_gateway_counts_only_prerequisites(self):
        parsed = parse_requisite_text(
            "Prerequisite: COMPSCI 201; Corequisite: COMPSCI 203; Antirequisite: COMPSCI 205"
        )
        rows = [{
            "subject": "COMPSCI", "catalog_nbr": "308",
            "requisite_parsed_json": json.dumps(parsed),
        }]
        counts = build_prerequisite_unlock_counts(rows, {"COMPSCI 308"})
        self.assertEqual(counts, {"COMPSCI 201": 1})

    def test_gateway_map_names_the_courses_unlocked(self):
        parsed = parse_requisite_text("Prerequisite: COMPSCI 201")
        rows = [
            {"subject": "COMPSCI", "catalog_nbr": code, "requisite_parsed_json": json.dumps(parsed)}
            for code in ("203", "205")
        ]
        self.assertEqual(
            build_prerequisite_unlock_map(rows, {"COMPSCI 203", "COMPSCI 205"}),
            {"COMPSCI 201": ["COMPSCI 203", "COMPSCI 205"]},
        )

    def test_every_bulletin_track_has_real_requirement_categories(self):
        data = get_bulletin_data()
        self.assertTrue(data["tracks"])
        self.assertFalse(any(
            group["category"] == "Major Requirements"
            for track in data["tracks"] for group in track["requirement_groups"]
        ))

    def test_choice_preferences_raise_course_priority(self):
        profile = {
            "major_key": "applied-mathematics-and-computational-sciences-mathematics",
            "completed_courses": ["MATH 105"],
            "choice_preferences": {
                "applied-mathematics-and-computational-sciences-mathematics": {
                    "divisional-foundation-courses-2": ["PHYS 121", "INTGSCI 205"],
                }
            },
        }
        context = build_major_requirement_context(profile)
        self.assertGreater(context["priorities"]["PHYS 121"], context["priorities"]["CHEM 110"])

    def test_profile_store_persists_choice_preferences_and_likert_targets(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            store = PlannerStore(path)
            saved = store.save_profile({
                "major_key": "track", "year_level": 2, "completed_courses": [],
                "preference_weights": {dimension: 4 for dimension in (
                    "teaching_quality", "exam_difficulty", "grading", "workload",
                    "content_value", "accessibility", "pace",
                )},
                "choice_preferences": {"track": {"group": ["PHYS 121"]}},
                "distribution_preferences": {
                    "SS": ["ECON 101", "POLSCI 101"],
                    "NS": ["PHYS 121"],
                },
            })
            self.assertEqual(saved["preference_weights"]["pace"], 4)
            self.assertEqual(saved["choice_preferences"]["track"]["group"], ["PHYS 121"])
            self.assertEqual(saved["distribution_preferences"]["SS"], ["ECON 101", "POLSCI 101"])
        finally:
            os.unlink(path)

    def test_browser_profiles_are_isolated_without_login(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            app = create_app(path)
            app.testing = True
            first = app.test_client()
            second = app.test_client()
            response = first.post("/profile", data={
                "major_key": "humanities-literature",
                "year_level": "2",
                "class_of": "2029",
                "identity": "",
                "completed_courses": "LIT 214",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(second.get("/profile").status_code, 200)
            first_id = first.get_cookie(PROFILE_COOKIE_NAME).value
            second_id = second.get_cookie(PROFILE_COOKIE_NAME).value
            self.assertNotEqual(first_id, second_id)
            store = PlannerStore(path)
            self.assertEqual(store.get_profile(first_id)["major_key"], "humanities-literature")
            self.assertEqual(store.get_profile(first_id)["completed_courses"], ["LIT 214"])
            self.assertEqual(store.get_profile(second_id)["major_key"], "")
        finally:
            os.unlink(path)

    def test_likert_preferences_match_target_levels(self):
        aggregate = {"review_count": 1, **{
            "avg_" + dimension: 4 for dimension in (
                "teaching_quality", "exam_difficulty", "grading", "workload",
                "content_value", "accessibility", "pace",
            )
        }}
        exact = review_match_score(aggregate, {dimension: 4 for dimension in (
            "teaching_quality", "exam_difficulty", "grading", "workload",
            "content_value", "accessibility", "pace",
        )})
        self.assertEqual(exact, 100.0)

    def test_completed_course_with_multiple_attributes_counts_once(self):
        profile = {"completed_courses": ["TEST 101"]}
        history = {"TEST 101": {"attributes": {"AH", "SS"}, "units": 4}}
        units = completed_attribute_units(profile, history)
        self.assertEqual(units["AH"] + units["SS"], 4.0)

    def test_cross_listed_codes_are_one_bulletin_option(self):
        data = get_bulletin_data()
        track = next(track for track in data["tracks"] if track["key"] == "data-science")
        group = next(
            group for group in track["requirement_groups"]
            if any(set(option) == {"COMPSCI 309", "STATS 302"} for option in group_course_options(group))
        )
        options = group_course_options(group)
        self.assertEqual(group["min_count"], len(options))
        self.assertEqual(
            sum(set(option) == {"COMPSCI 309", "STATS 302"} for option in options), 1
        )
        progress = profile_progress({
            "major_key": "data-science",
            "completed_courses": ["STATS 302"],
        })
        subgroup = next(
            subgroup for category in progress["groups"] for subgroup in category["subgroups"]
            if any(course["display_code"] == "STATS 302 / COMPSCI 309" for course in subgroup["courses"])
        )
        crosslisted = next(
            course for course in subgroup["courses"]
            if course["display_code"] == "STATS 302 / COMPSCI 309"
        )
        self.assertTrue(crosslisted["completed"])

    def test_ppe_philosophy_disciplinary_courses_include_footnoted_rows(self):
        data = get_bulletin_data()
        track = next(
            track for track in data["tracks"]
            if track["key"] == "philosophy-politics-and-economics-philosophy"
        )
        disciplinary = [
            group for group in track["requirement_groups"]
            if group["category"] == "Disciplinary Courses"
        ]
        required = next(group for group in disciplinary if group["kind"] == "required")
        choice = next(group for group in disciplinary if group["kind"] == "choice")
        self.assertEqual(required["min_count"], 4)
        self.assertEqual(
            group_course_options(required),
            [
                ["PHIL 226", "HIST 226"],
                ["PHIL 210"],
                ["PHIL 207"],
                ["PHIL 398"],
            ],
        )
        self.assertEqual(group_course_options(choice), [["PHIL 205"], ["PHIL 305"], ["PHIL 309"]])

    def test_materials_physics_includes_the_page_182_common_requirements(self):
        data = get_bulletin_data()
        track = next(track for track in data["tracks"] if track["key"] == "materials-science-physics")
        groups = track["requirement_groups"]
        interdisciplinary = next(
            group for group in groups if group["category"] == "Interdisciplinary Courses"
        )
        self.assertEqual(
            group_course_options(interdisciplinary),
            [["CHEM 201"], ["MATSCI 201"], ["MATSCI 301"], ["MATSCI 302"], ["MATSCI 401"]],
        )
        foundation = [group for group in groups if group["category"] == "Divisional Foundation Courses"]
        self.assertEqual([(group["kind"], group["min_count"]) for group in foundation], [("choice", 1), ("required", 3)])

    def test_every_track_has_all_three_major_requirement_categories(self):
        expected = {
            "Divisional Foundation Courses",
            "Interdisciplinary Courses",
            "Disciplinary Courses",
        }
        for track in get_bulletin_data()["tracks"]:
            categories = {group["category"] for group in track["requirement_groups"]}
            self.assertTrue(expected.issubset(categories), track["label"])

    def test_qpe_public_policy_table_is_classified_as_disciplinary(self):
        track = next(
            track for track in get_bulletin_data()["tracks"]
            if track["key"] == "quantitative-political-economy-public-policy"
        )
        disciplinary = next(
            group for group in track["requirement_groups"]
            if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual(
            group_course_options(disciplinary),
            [["PUBPOL 101"], ["PUBPOL 301"], ["PUBPOL 303"], ["PUBPOL 315", "ECON 315"]],
        )

    def test_humanities_literature_counts_five_courses_across_two_lists(self):
        empty = profile_progress({
            "major_key": "humanities-literature",
            "completed_courses": [],
        })
        disciplinary = next(
            group for group in empty["groups"] if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual((disciplinary["completed"], disciplinary["needed"]), (0, 6))
        choice_groups = [group for group in disciplinary["subgroups"] if group["kind"] == "choice"]
        self.assertEqual([(group["needed"], len(group["courses"])) for group in choice_groups], [(2, 6), (2, 6)])

        complete = profile_progress({
            "major_key": "humanities-literature",
            "completed_courses": [
                "LIT 214", "LIT 311", "LIT 314", "LIT 216", "LIT 203", "LIT 210",
            ],
        })
        disciplinary = next(
            group for group in complete["groups"] if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual((disciplinary["completed"], disciplinary["needed"]), (6, 6))

    def test_humanities_combined_total_does_not_replace_each_list_minimum(self):
        progress = profile_progress({
            "major_key": "humanities-literature",
            "completed_courses": ["LIT 214", "LIT 298", "LIT 216", "LIT 223"],
        })
        disciplinary = next(
            group for group in progress["groups"] if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual((disciplinary["completed"], disciplinary["needed"]), (4, 6))

    def test_humanities_combined_total_requires_two_upper_level_courses(self):
        progress = profile_progress({
            "major_key": "humanities-literature",
            "completed_courses": [
                "LIT 214", "LIT 298", "LIT 216", "LIT 203", "LIT 210", "LIT 221",
            ],
        })
        disciplinary = next(
            group for group in progress["groups"] if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual((disciplinary["completed"], disciplinary["needed"]), (4, 6))

    def test_philosophy_and_religion_requires_both_subjects_in_each_list(self):
        progress = profile_progress({
            "major_key": "humanities-philosophy-and-religion",
            "completed_courses": [
                "HUM 201", "PHIL 210", "PHIL 226",
                "PHIL 305", "RELIG 221", "RELIG 398",
            ],
        })
        disciplinary = next(
            group for group in progress["groups"] if group["category"] == "Disciplinary Courses"
        )
        self.assertEqual((disciplinary["completed"], disciplinary["needed"]), (5, 6))

    def test_requisite_relations_use_the_correct_time_scope(self):
        prerequisite = {"type": "pre", "items": [{"type": "course", "code": "COMPSCI 201"}]}
        corequisite = {"type": "co", "items": [{"type": "course", "code": "COMPSCI 203"}]}
        request = ScheduleRequest()

        ok, _, _ = _eval_tree(prerequisite, request, {"COMPSCI 201"}, set())
        self.assertFalse(ok, "a same-session course cannot satisfy a prerequisite")
        ok, _, _ = _eval_tree(prerequisite, request, {"COMPSCI 201"}, {"COMPSCI 201"})
        self.assertTrue(ok, "a 7W1 course can satisfy a 7W2 prerequisite")
        ok, _, _ = _eval_tree(corequisite, request, {"COMPSCI 203"}, set())
        self.assertTrue(ok, "a course in the same term can satisfy a corequisite")

    def test_antirequisite_respects_cross_listed_aliases(self):
        request = ScheduleRequest(
            completed_courses={"STATS 302"},
            course_equivalencies=[{"STATS 302", "COMPSCI 309"}],
        )
        anti = {"type": "anti_check", "code": "COMPSCI 309"}
        ok, _, _ = _eval_tree(anti, request, set(), set())
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
