"""
Tests for the review queue.

Nothing here should ever be possible without an explicit decision: no
auto-merge, no auto-reject, and no silently creating a second Youssef.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import review  # noqa: E402
import seed  # noqa: E402
import submissions as S  # noqa: E402


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original = config.DATABASE_PATH
        config.DATABASE_PATH = Path(self._tmp.name) / "review.db"
        self.conn = db.connect()
        db.init_db(self.conn)
        self.ids = seed.load(self.conn)

    def tearDown(self):
        self.conn.close()
        config.DATABASE_PATH = self._original
        self._tmp.cleanup()

    def queue(self, kind, people, about, user=8001, source=None):
        payload = S.build(
            kind,
            submitted_by=S.submitter(user),
            about=about,
            people=people,
            source=source,
        )
        return db.add_submission(self.conn, user, payload)


class ApprovalTests(ReviewTestCase):
    def test_approving_a_child_creates_them_and_links_the_parent(self):
        khalil = self.ids["khalil_y"]
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1)
        self.assertEqual(len(created), 1)

        rita = db.get_person(self.conn, created[0])
        self.assertEqual(rita["father_id"], khalil)
        self.assertEqual(db.row_display_name(rita), "Rita Khalil Sukkar")

    def test_approving_a_sibling_copies_both_parents(self):
        khalil = self.ids["khalil_y"]
        sid = self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Rita", sex="F")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1)
        subject = db.get_person(self.conn, khalil)
        rita = db.get_person(self.conn, created[0])
        self.assertEqual(rita["father_id"], subject["father_id"])
        self.assertEqual(rita["mother_id"], subject["mother_id"])

    def test_approving_a_spouse_creates_the_union(self):
        antoun = self.ids["antoun"]
        sid = self.queue(
            S.ADD_SPOUSE,
            [S.person(S.SPOUSE, "Rima", sex="F", family_name="Haddad")],
            S.subject(person_id=antoun, label="Antoun"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1)
        partners = [p["id"] for p in db.get_partners(self.conn, antoun)]
        self.assertIn(created[0], partners)

    def test_approving_parents_marries_them_to_each_other(self):
        joseph = self.ids["joseph"]
        # Joseph already has parents in the seed; use someone who does not.
        loner = db.create_person(self.conn, "Sami", sex="M")
        sid = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Tanios", sex="M"),
                S.person(S.MOTHER, "Zeina", sex="F", family_name="Abi"),
            ],
            S.subject(person_id=loner, label="Sami"),
        )
        review.approve(self.conn, sid, reviewed_by=1)
        sami = db.get_person(self.conn, loner)
        self.assertIsNotNone(sami["father_id"])
        self.assertIsNotNone(sami["mother_id"])
        partners = [p["id"] for p in db.get_partners(self.conn, sami["father_id"])]
        self.assertIn(sami["mother_id"], partners)

    def test_branch_is_assigned_after_approval(self):
        khalil = self.ids["khalil_y"]
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1)
        rita = db.get_person(self.conn, created[0])
        self.assertEqual(
            rita["branch_id"], db.get_branch_by_key(self.conn, "youssef")["id"]
        )

    def test_a_submission_cannot_be_approved_twice(self):
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        review.approve(self.conn, sid, reviewed_by=1)
        with self.assertRaises(review.Blocked):
            review.approve(self.conn, sid, reviewed_by=1)

    def test_audit_trail_is_recorded(self):
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        review.approve(self.conn, sid, reviewed_by=4242)
        row = db.get_submission(self.conn, sid)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["reviewed_by"], 4242)
        self.assertIsNotNone(row["reviewed_at"])
        self.assertIsNotNone(row["resulting_person_id"])


class DependencyTests(ReviewTestCase):
    """A contributor can name a grandfather before the father is approved."""

    def test_approving_out_of_order_is_blocked_with_the_reason(self):
        khalil = self.ids["khalil_y"]
        first = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Tanios", sex="M")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        second = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Semaan", sex="M")],
            S.subject(submission_id=first, label="Tanios"),
        )
        with self.assertRaises(review.Blocked) as caught:
            review.approve(self.conn, second, reviewed_by=1)
        self.assertIn(f"#{first}", str(caught.exception))

    def test_in_order_builds_the_chain(self):
        loner = db.create_person(self.conn, "Sami", sex="M")
        first = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Tanios", sex="M")],
            S.subject(person_id=loner, label="Sami"),
        )
        second = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Semaan", sex="M")],
            S.subject(submission_id=first, label="Tanios"),
        )
        review.approve(self.conn, first, reviewed_by=1, force=True)
        review.approve(self.conn, second, reviewed_by=1, force=True)

        sami = db.get_person(self.conn, loner)
        tanios = db.get_person(self.conn, sami["father_id"])
        semaan = db.get_person(self.conn, tanios["father_id"])
        self.assertEqual(semaan["given_name"], "Semaan")
        self.assertEqual(db.row_display_name(tanios), "Tanios Semaan Sukkar")


class DuplicateGuardTests(ReviewTestCase):
    def test_a_near_certain_duplicate_blocks_approval(self):
        """The tired-admin guard: don't let one tap create a second Youssef."""
        khalil = self.ids["khalil_y"]
        sid = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Youssef", sex="M")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        with self.assertRaises(review.Blocked) as caught:
            review.approve(self.conn, sid, reviewed_by=1)
        message = str(caught.exception)
        self.assertIn("already in the tree", message)
        self.assertIn("--merge", message)

    def test_the_guard_can_be_overridden_deliberately(self):
        khalil = self.ids["khalil_y"]
        sid = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Youssef", sex="M")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1, force=True)
        self.assertEqual(len(created), 1)

    def test_merging_applies_the_relationship_rather_than_discarding_it(self):
        loner = db.create_person(self.conn, "Sami", sex="M")
        youssef = self.ids["youssef"]
        sid = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Youssef", sex="M")],
            S.subject(person_id=loner, label="Sami"),
        )
        before = db.count_people(self.conn)
        review.merge(self.conn, sid, youssef, reviewed_by=1)

        self.assertEqual(db.count_people(self.conn), before, "merge created a person")
        self.assertEqual(db.get_person(self.conn, loner)["father_id"], youssef)
        row = db.get_submission(self.conn, sid)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["resulting_person_id"], youssef)

    def test_merging_never_overwrites_what_an_admin_recorded(self):
        khalil = self.ids["khalil_y"]
        georges = self.ids["georges"]
        sid = self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Georges", sex="M")],
            S.subject(person_id=khalil, label="Khalil"),
        )
        before = db.get_person(self.conn, georges)
        review.merge(self.conn, sid, georges, reviewed_by=1)
        after = db.get_person(self.conn, georges)
        self.assertEqual(after["father_id"], before["father_id"])
        self.assertEqual(after["mother_id"], before["mother_id"])

    def test_merging_into_a_person_who_does_not_exist_is_refused(self):
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        with self.assertRaises(review.Blocked):
            review.merge(self.conn, sid, 99999, reviewed_by=1)


class CorroborationTests(ReviewTestCase):
    def test_shared_parents_beat_a_misspelling(self):
        """The whole point: 'Khaleel' is the same man as 'Khalil'."""
        georges = self.ids["georges"]
        sid = self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Khaleel", sex="M")],
            S.subject(person_id=georges, label="Georges"),
        )
        payload = db.submission_payload(db.get_submission(self.conn, sid))
        matches = review.evidence(self.conn, payload, sid)
        top = matches[0]
        self.assertEqual(top["person_id"], self.ids["khalil_y"])
        self.assertIn("same father", top["reasons"])

    def test_a_submission_is_not_evidence_for_itself(self):
        sid = self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Zaher", sex="M")],
            S.subject(person_id=self.ids["georges"], label="Georges"),
        )
        payload = db.submission_payload(db.get_submission(self.conn, sid))
        matches = review.evidence(self.conn, payload, sid)
        self.assertFalse(
            [m for m in matches if m["kind"] == "submission" and m["id"] == sid]
        )

    def test_two_independent_claims_corroborate_each_other(self):
        khalil = self.ids["khalil_y"]
        about = S.subject(person_id=khalil, label="Khalil")
        first = self.queue(
            S.ADD_SIBLING, [S.person(S.SIBLING, "Tanios", sex="M")], about, user=1
        )
        second = self.queue(
            S.ADD_SIBLING, [S.person(S.SIBLING, "Tanios", sex="M")], about, user=2
        )
        payload = db.submission_payload(db.get_submission(self.conn, second))
        matches = review.evidence(self.conn, payload, second)
        agreeing = [m for m in matches if m["kind"] == "submission" and m["id"] == first]
        self.assertTrue(agreeing)
        self.assertIn("someone else described", agreeing[0]["reasons"][0])


class NothingIsAutomaticTests(ReviewTestCase):
    def test_queuing_alone_never_changes_the_family(self):
        before = db.count_people(self.conn)
        self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        self.assertEqual(db.count_people(self.conn), before)

    def test_a_correction_is_never_applied_automatically(self):
        payload = S.build(
            S.CORRECTION,
            submitted_by=S.submitter(1),
            about=S.subject(label="Rita"),
            note="Her name is Rita, one t",
            target_submission_id=1,
        )
        sid = db.add_submission(self.conn, 1, payload)
        with self.assertRaises(review.Blocked) as caught:
            review.approve(self.conn, sid, reviewed_by=1)
        self.assertIn("note, not a change", str(caught.exception))

    def test_rejecting_keeps_the_original_payload(self):
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Rita", sex="F")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        before = db.get_submission(self.conn, sid)["payload_json"]
        review.reject(self.conn, sid, reviewed_by=1, note="not a relative")
        after = db.get_submission(self.conn, sid)
        self.assertEqual(after["payload_json"], before)
        self.assertEqual(after["status"], "rejected")
        self.assertEqual(after["review_note"], "not a relative")

    def test_a_failed_approval_leaves_nothing_behind(self):
        loner = db.create_person(self.conn, "Sami", sex="M")
        first = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Tanios", sex="M")],
            S.subject(person_id=loner, label="Sami"),
        )
        second = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Semaan", sex="M")],
            S.subject(submission_id=first, label="Tanios"),
        )
        before = db.count_people(self.conn)
        with self.assertRaises(review.Blocked):
            review.approve(self.conn, second, reviewed_by=1)
        self.assertEqual(db.count_people(self.conn), before)


class SpellingPrecedenceTests(ReviewTestCase):
    """Three relatives, three spellings, one man. Whose answer wins?"""

    def setUp(self):
        super().setUp()
        self.semaan = db.create_person(
            self.conn, "Semaan", sex="M", family_name="Succar"
        )
        self.steven = db.create_person(
            self.conn, "Steven", sex="M", family_name="Sukar", father_id=self.semaan
        )

    def record_brother(self, spelling, user=111, label="Steven"):
        return self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Tony", sex="M", family_name=spelling)],
            S.subject(person_id=self.steven, label="Steven"),
            user=user,
        )

    def sign_up_as(self, spelling, user=222):
        payload = S.build(
            S.IDENTIFY,
            submitted_by=S.submitter(user),
            about=S.subject(),
            people=[
                S.person(
                    S.SELF, "Tony", family_name=spelling, father_given_name="Semaan"
                )
            ],
        )
        return db.add_submission(self.conn, user, payload)

    def test_a_relatives_guess_is_used_when_it_is_all_there_is(self):
        sid = self.record_brother("Succar")
        tony = review.approve(self.conn, sid, reviewed_by=1, force=True)[0]
        self.assertEqual(db.get_person(self.conn, tony)["family_name"], "Succar")
        self.assertFalse(db.get_person(self.conn, tony)["family_name_self_reported"])

    def test_the_persons_own_answer_overrides_a_guess(self):
        sid = self.record_brother("Succar")
        tony = review.approve(self.conn, sid, reviewed_by=1, force=True)[0]
        review.merge(self.conn, self.sign_up_as("Sukkar"), tony, reviewed_by=1)

        person = db.get_person(self.conn, tony)
        self.assertEqual(person["family_name"], "Sukkar")
        self.assertTrue(person["family_name_self_reported"])

    def test_a_later_guess_cannot_overwrite_their_own_answer(self):
        sid = self.record_brother("Succar")
        tony = review.approve(self.conn, sid, reviewed_by=1, force=True)[0]
        review.merge(self.conn, self.sign_up_as("Sukkar"), tony, reviewed_by=1)
        review.merge(
            self.conn, self.record_brother("Sukar", user=333), tony, reviewed_by=1
        )
        self.assertEqual(db.get_person(self.conn, tony)["family_name"], "Sukkar")

    def test_every_spelling_anyone_claimed_is_kept(self):
        """The same man is spelled differently on different countries' paper."""
        sid = self.record_brother("Succar")
        tony = review.approve(self.conn, sid, reviewed_by=1, force=True)[0]
        review.merge(self.conn, self.sign_up_as("Sukkar"), tony, reviewed_by=1)
        review.merge(
            self.conn, self.record_brother("Sukar", user=333), tony, reviewed_by=1
        )

        claims = db.spelling_claims(self.conn, tony)
        self.assertEqual(
            {c["spelling"] for c in claims}, {"Succar", "Sukkar", "Sukar"}
        )
        self.assertEqual(
            [c["spelling"] for c in claims if c["self_reported"]], ["Sukkar"]
        )

    def test_three_spellings_still_identify_the_same_man(self):
        """Spelling contributes nothing to identity — relatives do."""
        self.record_brother("Succar")
        self.sign_up_as("Sukkar")
        matches = db.corroborate(
            self.conn,
            "Tony",
            role="sibling",
            subject_person_id=self.steven,
            family_name="Sukar",
            threshold=0.5,
        )
        self.assertGreaterEqual(len(matches), 2)
        self.assertTrue(all(m["score"] >= 0.9 for m in matches[:2]))


class SpellingReportTests(ReviewTestCase):
    """A spelling is a border crossing, not a branch."""

    def build_split_line(self):
        semaan = db.create_person(self.conn, "Semaan", sex="M", family_name="Succar")
        kalim = db.create_person(
            self.conn, "Kalim", sex="M", family_name="Sukar", father_id=semaan
        )
        for name in ("Steven", "Tony"):
            db.create_person(
                self.conn, name, sex="M", family_name="Sukar", father_id=kalim
            )
        return semaan, kalim

    def test_every_spelling_is_still_one_family(self):
        self.build_split_line()
        for spelling in ("Succar", "Sukar", config.FAMILY_NAME):
            self.assertEqual(
                db.canonical_family_name(spelling, self.conn), config.FAMILY_NAME
            )

    def test_the_divergence_point_is_findable(self):
        _semaan, kalim = self.build_split_line()
        people = {row["id"]: row for row in db.get_people(self.conn)}
        splits = [
            person_id
            for person_id, row in people.items()
            if row["father_id"]
            and people[row["father_id"]]["family_name"] != row["family_name"]
        ]
        self.assertEqual(splits, [kalim])

    def test_the_report_runs_and_names_the_split(self):
        import io
        from contextlib import redirect_stdout

        self.build_split_line()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            review.show_spellings(self.conn)
        output = buffer.getvalue()
        self.assertIn("Sukar", output)
        self.assertIn("splits from Succar", output)
        self.assertIn("one family", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
