"""
Tests for the foundation layer.

Standard library `unittest` only — no pytest, no plugins. This project has to
still run in five years, and `python -m unittest` will.

    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import doctest
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402


class DisplayNameTests(unittest.TestCase):
    """Constraint 3: the computed name is the whole naming system."""

    def test_inserts_fathers_given_name(self):
        self.assertEqual(
            db.display_name("Steven", "Sukkar", "Khalil"), "Steven Khalil Sukkar"
        )

    def test_falls_back_when_father_unknown(self):
        self.assertEqual(db.display_name("Mariam", "Sukkar"), "Mariam Sukkar")
        self.assertEqual(db.display_name("Mariam", "Sukkar", None), "Mariam Sukkar")

    def test_treats_blank_father_as_unknown(self):
        self.assertEqual(db.display_name("Mariam", "Sukkar", ""), "Mariam Sukkar")
        self.assertEqual(db.display_name("Mariam", "Sukkar", "   "), "Mariam Sukkar")

    def test_strips_stray_whitespace(self):
        self.assertEqual(
            db.display_name("  Steven ", " Sukkar ", " Khalil "),
            "Steven Khalil Sukkar",
        )

    def test_two_men_named_khalil_are_distinguishable(self):
        self.assertNotEqual(
            db.display_name("Khalil", "Sukkar", "Youssef"),
            db.display_name("Khalil", "Sukkar", "Joseph"),
        )

    def test_keeps_a_married_in_womans_own_family_name(self):
        self.assertEqual(db.display_name("Nada", "Karam"), "Nada Karam")

    def test_handles_arabic_script(self):
        self.assertEqual(db.display_name("خليل", "سكر", "يوسف"), "خليل يوسف سكر")


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_init_db_is_repeatable(self):
        db.init_db(self.conn)  # must not raise
        db.init_db(self.conn)

    def test_foreign_keys_are_enforced(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO people (given_name, family_name, father_id)"
                " VALUES ('Ghost', 'Sukkar', 9999)"
            )

    def test_person_cannot_be_their_own_father(self):
        import sqlite3

        person_id = db.create_person(self.conn, "Elias")
        with self.assertRaises(sqlite3.IntegrityError):
            db.update_person(self.conn, person_id, father_id=person_id)

    def test_blank_given_name_is_rejected(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_person(self.conn, "   ")

    def test_sex_is_restricted(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_person(self.conn, "Sami", sex="male")

    def test_no_date_columns_exist_anywhere(self):
        """Constraint 2, enforced rather than trusted.

        `created_at` and `reviewed_at` describe rows, not relatives, and are
        allowed. Anything else date-shaped is a regression.
        """
        allowed = {"created_at", "reviewed_at"}
        forbidden = ("birth", "death", "born", "died", "dob", "year", "age", "date")

        tables = [
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            for column in self.conn.execute(f"PRAGMA table_info({table})"):
                name = column["name"]
                if name in allowed:
                    continue
                for needle in forbidden:
                    self.assertNotIn(
                        needle,
                        name.lower(),
                        f"{table}.{name} looks like a date field",
                    )


class UnionTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.a = db.create_person(self.conn, "Khalil", sex="M")
        self.b = db.create_person(self.conn, "Mariam", sex="F")

    def tearDown(self):
        self.conn.close()

    def test_union_is_undirected(self):
        first = db.create_union(self.conn, self.a, self.b)
        second = db.create_union(self.conn, self.b, self.a)
        self.assertEqual(first, second)
        self.assertEqual(len(db.get_unions(self.conn)), 1)

    def test_reversed_duplicate_is_rejected_at_the_database_level(self):
        import sqlite3

        db.create_union(self.conn, self.a, self.b)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO unions (partner_a_id, partner_b_id) VALUES (?, ?)",
                (self.b, self.a),
            )

    def test_self_union_is_rejected(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO unions (partner_a_id, partner_b_id) VALUES (?, ?)",
                (self.a, self.a),
            )

    def test_partners_are_found_from_either_side(self):
        db.create_union(self.conn, self.a, self.b)
        self.assertEqual([r["id"] for r in db.get_partners(self.conn, self.a)], [self.b])
        self.assertEqual([r["id"] for r in db.get_partners(self.conn, self.b)], [self.a])


class GraphTests(unittest.TestCase):
    """Constraint 1: cousin intermarriage must not break anything."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_shared_ancestor_reached_by_two_paths(self):
        joseph = self.ids["joseph"]
        ancestors = db.get_ancestors(self.conn, joseph)
        # Elias is Joseph's great-great-grandfather down BOTH sides.
        self.assertIn(self.ids["elias"], ancestors)
        self.assertIn(self.ids["youssef"], ancestors)
        self.assertIn(self.ids["boutros"], ancestors)

    def test_ancestor_walk_terminates_on_a_diamond(self):
        # A set-based walk over a graph with two paths to Elias must finish,
        # and must list him once.
        ancestors = db.get_ancestors(self.conn, self.ids["joseph"])
        self.assertEqual(len([a for a in ancestors if a == self.ids["elias"]]), 1)

    def test_children_are_found_through_either_parent(self):
        via_father = {r["id"] for r in db.get_children(self.conn, self.ids["khalil_y"])}
        via_mother = {r["id"] for r in db.get_children(self.conn, self.ids["therese"])}
        self.assertIn(self.ids["mariam"], via_father)
        self.assertIn(self.ids["mariam"], via_mother)

    def test_siblings(self):
        siblings = {r["id"] for r in db.get_siblings(self.conn, self.ids["khalil_y"])}
        self.assertEqual(siblings, {self.ids["georges"]})

    def test_display_names_come_out_of_the_database_computed(self):
        names = {
            db.row_display_name(db.get_person(self.conn, self.ids[key]))
            for key in ("khalil_y", "khalil_a")
        }
        self.assertEqual(names, {"Khalil Youssef Sukkar", "Khalil Antoun Sukkar"})

    def test_correcting_a_father_link_corrects_the_display_name(self):
        """The reason the name is never stored."""
        mariam = self.ids["mariam"]
        self.assertEqual(
            db.row_display_name(db.get_person(self.conn, mariam)),
            "Mariam Khalil Sukkar",
        )
        db.update_person(self.conn, mariam, father_id=self.ids["georges"])
        self.assertEqual(
            db.row_display_name(db.get_person(self.conn, mariam)),
            "Mariam Georges Sukkar",
        )


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_branches_come_from_config(self):
        keys = {row["key"] for row in db.get_branches(self.conn)}
        self.assertEqual(keys, {entry["key"] for entry in config.FOUNDING_ANCESTORS})

    def test_sync_branches_updates_rather_than_duplicates(self):
        before = len(db.get_branches(self.conn))
        db.sync_branches(self.conn)
        self.assertEqual(len(db.get_branches(self.conn)), before)

    def test_branch_follows_the_patriline(self):
        youssef_branch = db.get_branch_by_key(self.conn, "youssef")["id"]
        for key in ("khalil_y", "georges", "mariam"):
            person = db.get_person(self.conn, self.ids[key])
            self.assertEqual(person["branch_id"], youssef_branch, key)

    def test_cousin_marriage_child_takes_the_fathers_branch(self):
        boutros_branch = db.get_branch_by_key(self.conn, "boutros")["id"]
        joseph = db.get_person(self.conn, self.ids["joseph"])
        self.assertEqual(joseph["branch_id"], boutros_branch)

    def test_married_in_wife_inherits_her_husbands_branch(self):
        youssef_branch = db.get_branch_by_key(self.conn, "youssef")["id"]
        nada = db.get_person(self.conn, self.ids["nada"])
        self.assertEqual(nada["branch_id"], youssef_branch)

    def test_ancestor_above_the_founders_has_no_branch(self):
        # Elias predates the split into branches. Guessing one would be wrong.
        elias = db.get_person(self.conn, self.ids["elias"])
        self.assertIsNone(elias["branch_id"])

    def test_assign_branches_is_idempotent(self):
        self.assertEqual(db.assign_branches(self.conn), 0)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_seed_data_is_clean(self):
        seed.load(self.conn)
        issues = db.check_integrity(self.conn)
        self.assertEqual(issues["foreign_key_violations"], [])
        self.assertEqual(issues["ancestry_cycles"], [])
        self.assertEqual(issues["name_collisions"], [])
        self.assertEqual(issues["branches_without_founder"], [])

    def test_name_collision_is_detected(self):
        father = db.create_person(self.conn, "Youssef", sex="M")
        db.create_person(self.conn, "Khalil", sex="M", father_id=father)
        db.create_person(self.conn, "Khalil", sex="M", father_id=father)
        collisions = db.find_name_collisions(self.conn)
        self.assertEqual(len(collisions), 1)
        name, rows = collisions[0]
        self.assertEqual(name, "Khalil Youssef Sukkar")
        self.assertEqual(len(rows), 2)

    def test_distinct_fathers_are_not_a_collision(self):
        a = db.create_person(self.conn, "Youssef", sex="M")
        b = db.create_person(self.conn, "Antoun", sex="M")
        db.create_person(self.conn, "Khalil", sex="M", father_id=a)
        db.create_person(self.conn, "Khalil", sex="M", father_id=b)
        self.assertEqual(db.find_name_collisions(self.conn), [])

    def test_ancestry_cycle_is_detected(self):
        a = db.create_person(self.conn, "Elias")
        b = db.create_person(self.conn, "Youssef", father_id=a)
        c = db.create_person(self.conn, "Khalil", father_id=b)
        # Close the loop: Elias's father is his own great-grandson.
        db.update_person(self.conn, a, father_id=c)
        self.assertTrue(db.find_ancestry_cycles(self.conn))

    def test_no_cycle_reported_on_a_diamond(self):
        """Two paths to one ancestor is normal here, not a cycle."""
        seed.load(self.conn)
        self.assertEqual(db.find_ancestry_cycles(self.conn), [])

    def test_ancestor_walk_survives_a_cycle(self):
        a = db.create_person(self.conn, "Elias")
        b = db.create_person(self.conn, "Youssef", father_id=a)
        db.update_person(self.conn, a, father_id=b)
        self.assertEqual(db.get_ancestors(self.conn, b), {a, b})


class SubmissionTests(unittest.TestCase):
    """Constraint 4: the queue is the only way in."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_submission_round_trips_its_payload(self):
        payload = {"kind": "add_child", "given_name": "Rita", "father_key": 3}
        submission_id = db.add_submission(self.conn, 12345, payload)
        row = db.get_submission(self.conn, submission_id)
        self.assertEqual(db.submission_payload(row), payload)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["reviewed_at"])

    def test_payload_preserves_arabic(self):
        submission_id = db.add_submission(self.conn, 1, {"given_name_ar": "خليل"})
        row = db.get_submission(self.conn, submission_id)
        self.assertEqual(db.submission_payload(row)["given_name_ar"], "خليل")
        self.assertIn("خليل", row["payload_json"])  # stored readable, not escaped

    def test_queue_lists_pending_oldest_first(self):
        first = db.add_submission(self.conn, 1, {"n": 1})
        second = db.add_submission(self.conn, 1, {"n": 2})
        self.assertEqual([r["id"] for r in db.list_submissions(self.conn)], [first, second])

    def test_resolving_removes_it_from_the_pending_queue(self):
        submission_id = db.add_submission(self.conn, 1, {"n": 1})
        db.resolve_submission(self.conn, submission_id, "approved", reviewed_by=99)
        self.assertEqual(db.list_submissions(self.conn, "pending"), [])
        row = db.get_submission(self.conn, submission_id)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["reviewed_by"], 99)
        self.assertIsNotNone(row["reviewed_at"])

    def test_only_real_decisions_are_accepted(self):
        submission_id = db.add_submission(self.conn, 1, {"n": 1})
        with self.assertRaises(ValueError):
            db.resolve_submission(self.conn, submission_id, "pending", reviewed_by=1)
        with self.assertRaises(ValueError):
            db.resolve_submission(self.conn, submission_id, "auto_merged", reviewed_by=1)

    def test_invalid_status_is_rejected_by_the_database_too(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO submissions (telegram_user_id, payload_json, status)"
                " VALUES (1, '{}', 'auto_approved')"
            )

    def test_queue_scopes_to_a_branch_via_the_submitter(self):
        youssef = db.get_branch_by_key(self.conn, "youssef")["id"]
        boutros = db.get_branch_by_key(self.conn, "boutros")["id"]
        db.upsert_contributor(self.conn, 111, branch_id=youssef)
        db.upsert_contributor(self.conn, 222, branch_id=boutros)
        mine = db.add_submission(self.conn, 111, {"n": 1})
        db.add_submission(self.conn, 222, {"n": 2})

        scoped = db.list_submissions(self.conn, "pending", branch_id=youssef)
        self.assertEqual([r["id"] for r in scoped], [mine])


class ContributorTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_upsert_creates_then_updates(self):
        db.upsert_contributor(self.conn, 42, display_label="Steven")
        db.upsert_contributor(self.conn, 42, linked_person_id=self.ids["joseph"])
        row = db.get_contributor(self.conn, 42)
        self.assertEqual(row["display_label"], "Steven")  # not clobbered
        self.assertEqual(row["linked_person_id"], self.ids["joseph"])

    def test_super_admin_from_config_needs_no_database_row(self):
        original = config.SUPER_ADMIN_TELEGRAM_IDS
        config.SUPER_ADMIN_TELEGRAM_IDS = [777]
        try:
            self.assertTrue(db.is_super_admin(777, self.conn))
            self.assertFalse(db.is_super_admin(778, self.conn))
        finally:
            config.SUPER_ADMIN_TELEGRAM_IDS = original

    def test_branch_admin_sees_only_their_branch(self):
        youssef = db.get_branch_by_key(self.conn, "youssef")["id"]
        self.conn.execute(
            "UPDATE branches SET admin_telegram_id = 555 WHERE id = ?", (youssef,)
        )
        self.assertEqual(db.admin_branch_ids(self.conn, 555), [youssef])

    def test_super_admin_sees_everything(self):
        original = config.SUPER_ADMIN_TELEGRAM_IDS
        config.SUPER_ADMIN_TELEGRAM_IDS = [777]
        try:
            self.assertIsNone(db.admin_branch_ids(self.conn, 777))
        finally:
            config.SUPER_ADMIN_TELEGRAM_IDS = original


class FuzzyMatchTests(unittest.TestCase):
    """Duplicate handling: flag for a human, never decide."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_exact_name_and_father_matches(self):
        matches = db.find_probable_matches(self.conn, "Khalil", "Youssef")
        self.assertTrue(matches)
        self.assertEqual(matches[0][0]["id"], self.ids["khalil_y"])

    def test_misspelling_still_matches(self):
        matches = db.find_probable_matches(self.conn, "Khaleel", "Youssef")
        self.assertTrue(matches)
        self.assertEqual(matches[0][0]["id"], self.ids["khalil_y"])

    def test_fathers_name_separates_the_two_khalils(self):
        matches = db.find_probable_matches(self.conn, "Khalil", "Antoun")
        self.assertEqual(matches[0][0]["id"], self.ids["khalil_a"])

    def test_case_is_ignored(self):
        matches = db.find_probable_matches(self.conn, "khalil", "youssef")
        self.assertTrue(matches)

    def test_unrelated_name_does_not_match(self):
        self.assertEqual(db.find_probable_matches(self.conn, "Zaher", "Fadi"), [])

    def test_matching_can_be_scoped_to_a_branch(self):
        boutros = db.get_branch_by_key(self.conn, "boutros")["id"]
        matches = db.find_probable_matches(
            self.conn, "Khalil", "Youssef", branch_id=boutros
        )
        self.assertNotIn(
            self.ids["khalil_y"], [row["id"] for row, _ in matches]
        )

    def test_results_are_ordered_best_first(self):
        matches = db.find_probable_matches(self.conn, "Khalil", "Youssef", threshold=0.0)
        scores = [score for _, score in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))


class SeedValidationTests(unittest.TestCase):
    def setUp(self):
        self._people = list(seed.PEOPLE)
        self._unions = list(seed.UNIONS)

    def tearDown(self):
        seed.PEOPLE[:] = self._people
        seed.UNIONS[:] = self._unions

    def test_shipped_seed_data_validates(self):
        self.assertEqual(seed.validate(), [])

    def test_duplicate_key_is_caught(self):
        seed.PEOPLE.append({"key": "elias", "given": "Elias"})
        self.assertTrue(any("duplicate key" in p for p in seed.validate()))

    def test_unknown_parent_reference_is_caught(self):
        seed.PEOPLE.append({"key": "ghost", "given": "Ghost", "father": "nobody"})
        self.assertTrue(any("nobody" in p for p in seed.validate()))

    def test_full_name_in_given_is_caught(self):
        seed.PEOPLE.append({"key": "x", "given": "Khalil Youssef Sukkar"})
        self.assertTrue(any("first name only" in p for p in seed.validate()))

    def test_date_field_is_caught(self):
        seed.PEOPLE.append({"key": "x", "given": "Sami", "birth": "1901"})
        problems = seed.validate()
        self.assertTrue(any("no dates" in p for p in problems), problems)

    def test_typo_in_a_field_name_is_caught(self):
        seed.PEOPLE.append({"key": "x", "given": "Sami", "fathr": "elias"})
        self.assertTrue(any("unknown field" in p for p in seed.validate()))

    def test_self_parent_is_caught(self):
        seed.PEOPLE.append({"key": "x", "given": "Sami", "father": "x"})
        self.assertTrue(any("their own father" in p for p in seed.validate()))

    def test_union_with_unknown_person_is_caught(self):
        seed.UNIONS.append({"a": "elias", "b": "nobody"})
        self.assertTrue(any("nobody" in p for p in seed.validate()))

    def test_branch_without_a_matching_person_is_caught(self):
        original = config.FOUNDING_ANCESTORS
        config.FOUNDING_ANCESTORS = original + [
            {"key": "missing", "given_name": "Missing", "display_name": "Line of Missing"}
        ]
        try:
            self.assertTrue(any("'missing'" in p for p in seed.validate()))
        finally:
            config.FOUNDING_ANCESTORS = original


class SeedLoadTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_load_inserts_everything(self):
        ids = seed.load(self.conn)
        self.assertEqual(len(ids), len(seed.PEOPLE))
        self.assertEqual(db.count_people(self.conn), len(seed.PEOPLE))
        self.assertEqual(len(db.get_unions(self.conn)), len(seed.UNIONS))

    def test_parents_may_be_listed_after_their_children(self):
        seed.load(self.conn)
        khalil = db.get_person(self.conn, 1)  # elias, listed first
        self.assertIsNotNone(khalil)
        joseph = [
            row
            for row in db.get_people(self.conn)
            if row["given_name"] == "Joseph"
        ][0]
        self.assertIsNotNone(joseph["father_id"])

    def test_reset_empties_every_table(self):
        seed.load(self.conn)
        db.add_submission(self.conn, 1, {"n": 1})
        db.upsert_contributor(self.conn, 1)
        seed.reset(self.conn)
        for table in ("people", "unions", "branches", "submissions", "contributors"):
            count = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(count, 0, table)

    def test_load_is_atomic(self):
        """A failure part way through must leave nothing behind."""
        broken = list(seed.PEOPLE) + [{"key": "bad", "given": "Bad", "sex": "unknown"}]
        original = seed.PEOPLE[:]
        seed.PEOPLE[:] = broken
        try:
            with self.assertRaises(Exception):
                seed.load(self.conn)
        finally:
            seed.PEOPLE[:] = original
        self.assertEqual(db.count_people(self.conn), 0)


class ConfigTests(unittest.TestCase):
    """White-label: nothing family-specific outside config.py."""

    def test_family_name_appears_in_no_other_python_file(self):
        root = Path(__file__).resolve().parents[1]
        needles = [config.FAMILY_NAME, config.VILLAGE]
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "config.py" or "/.venv/" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(root)} contains {needle!r}")
        # tests/ is allowed to name the family; it is asserting on output.
        offenders = [o for o in offenders if not o.startswith("tests/")]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_every_configured_branch_has_the_fields_the_code_reads(self):
        for entry in config.FOUNDING_ANCESTORS:
            self.assertIn("key", entry)
            self.assertIn("given_name", entry)
            self.assertIn("display_name", entry)

    def test_branch_keys_are_unique(self):
        keys = [entry["key"] for entry in config.FOUNDING_ANCESTORS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_secrets_are_not_hardcoded(self):
        source = (Path(__file__).resolve().parents[1] / "config.py").read_text()
        self.assertIn("os.environ", source)
        for line in source.splitlines():
            if "TELEGRAM_BOT_TOKEN" in line and "=" in line and "os.environ" not in line:
                self.assertNotIn(":", line.split("=", 1)[1], f"token literal? {line}")


def load_tests(loader, tests, ignore):
    """Run the doctests in db.py as part of the suite.

    The `display_name` examples are the canonical statement of constraint 3,
    so they should fail the build if they ever stop being true.
    """
    tests.addTests(doctest.DocTestSuite(db))
    return tests


if __name__ == "__main__":
    unittest.main(verbosity=2)
