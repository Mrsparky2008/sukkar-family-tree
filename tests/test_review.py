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


class BothSpellingsShownTests(ReviewTestCase):
    """Where a name is genuinely written two ways, show both."""

    def setUp(self):
        super().setUp()
        self.semaan = db.create_person(
            self.conn, "Semaan", sex="M", family_name="Succar"
        )
        self.wadiha = db.create_person(
            self.conn, "Wadiha", sex="F", family_name="Karam"
        )
        self.steven = db.create_person(
            self.conn,
            "Steven",
            sex="M",
            family_name="Sukar",
            father_id=self.semaan,
            mother_id=self.wadiha,
        )

    def test_one_spelling_shows_plainly(self):
        self.assertEqual(
            db.display_name_with_spellings(
                self.conn, db.get_person(self.conn, self.steven)
            ),
            "Steven Semaan Sukar",
        )

    def test_a_second_spelling_is_shown_alongside(self):
        sid = self.queue(
            S.ADD_CHILD,
            [S.person(S.CHILD, "Steven", sex="M", family_name="Sukkar")],
            S.subject(person_id=self.semaan, label="Semaan"),
        )
        review.merge(self.conn, sid, self.steven, reviewed_by=1)
        self.assertEqual(
            db.display_name_with_spellings(
                self.conn, db.get_person(self.conn, self.steven)
            ),
            "Steven Semaan Sukar / Sukkar",
        )

    def test_the_name_rule_underneath_is_untouched(self):
        """Constraint 3: still exactly one place a name gets built."""
        row = db.get_person(self.conn, self.steven)
        self.assertTrue(
            db.display_name_with_spellings(self.conn, row).startswith(
                db.row_display_name(row)
            )
        )

    def test_same_parents_and_name_beats_a_different_surname(self):
        matches = db.corroborate(
            self.conn,
            "Steven",
            role="child",
            subject_person_id=self.semaan,
            family_name="Sukkar",
            threshold=0.5,
        )
        self.assertEqual(matches[0]["person_id"], self.steven)
        self.assertGreaterEqual(matches[0]["score"], 0.9)

    def test_more_agreeing_relatives_means_more_confidence(self):
        one = db.corroborate(
            self.conn, "Stephen", role="child",
            subject_person_id=self.semaan, threshold=0.0,
        )
        both = db.corroborate(
            self.conn, "Stephen", role="sibling",
            subject_person_id=db.create_person(
                self.conn, "Tony", sex="M",
                father_id=self.semaan, mother_id=self.wadiha,
            ),
            threshold=0.0,
        )
        self.assertGreater(
            [m for m in both if m["person_id"] == self.steven][0]["score"],
            [m for m in one if m["person_id"] == self.steven][0]["score"],
        )


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


class AnchorTests(ReviewTestCase):
    """Which person in a submission an anchor means.

    "Add my parents" creates two people but records one resulting_person_id.
    Resolving the mother to the father put her parents on her husband and gave
    her siblings the wrong pair — silently, and three generations deep.
    """

    def add_parents_of(self, person_id, father, mother):
        sid = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, father, sex="M"),
                S.person(S.MOTHER, mother, sex="F"),
            ],
            S.subject(person_id=person_id, label="child"),
        )
        review.approve(self.conn, sid, reviewed_by=1, force=True)
        return sid

    def person_named(self, name):
        return [
            row for row in db.get_people(self.conn) if row["given_name"] == name
        ][0]

    def test_both_parents_are_traceable_to_the_submission(self):
        child = db.create_person(self.conn, "Sami", sex="M")
        sid = self.add_parents_of(child, "Tanios", "Zeina")
        created = {p["given_name"] for p in db.people_from_submission(self.conn, sid)}
        self.assertEqual(created, {"Tanios", "Zeina"})

    def test_an_anchor_naming_the_mother_resolves_to_the_mother(self):
        child = db.create_person(self.conn, "Sami", sex="M")
        first = self.add_parents_of(child, "Tanios", "Zeina")

        # Now: "Zeina's parents are Semaan and Rima".
        second = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Semaan", sex="M"),
                S.person(S.MOTHER, "Rima", sex="F"),
            ],
            S.subject(submission_id=first, label="Zeina"),
        )
        review.approve(self.conn, second, reviewed_by=1, force=True)

        zeina = self.person_named("Zeina")
        tanios = self.person_named("Tanios")
        self.assertEqual(
            db.get_person(self.conn, zeina["father_id"])["given_name"], "Semaan"
        )
        self.assertIsNone(tanios["father_id"], "the father inherited her parents")

    def test_an_anchor_through_a_merged_pair_still_finds_the_mother(self):
        # The couple is already on the tree, and the child is linked to them.
        tanios = db.create_person(self.conn, "Tanios", sex="M")
        zeina = db.create_person(self.conn, "Zeina", sex="F")
        child = db.create_person(
            self.conn, "Sami", sex="M", father_id=tanios, mother_id=zeina
        )

        # A second contributor sends the same pair. The reviewer merges it:
        # nothing is created, the claim is kept as corroboration.
        duplicate = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Tanios", sex="M"),
                S.person(S.MOTHER, "Zeina", sex="F"),
            ],
            S.subject(person_id=child, label="Sami"),
        )
        db.resolve_submission(
            self.conn, duplicate, "merged", 1, resulting_person_id=tanios
        )

        # The same contributor continues: "Zeina's parents are Semaan and
        # Rima" — anchored to their own merged submission, naming the mother.
        second = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Semaan", sex="M"),
                S.person(S.MOTHER, "Rima", sex="F"),
            ],
            S.subject(submission_id=duplicate, label="Zeina"),
        )
        review.approve(self.conn, second, reviewed_by=1, force=True)

        zeina_row = db.get_person(self.conn, zeina)
        tanios_row = db.get_person(self.conn, tanios)
        self.assertEqual(
            db.get_person(self.conn, zeina_row["father_id"])["given_name"],
            "Semaan",
        )
        self.assertIsNone(
            tanios_row["father_id"], "her parents landed on her husband"
        )

    def test_replacing_a_recorded_parent_must_be_said_out_loud(self):
        tanios = db.create_person(self.conn, "Tanios", sex="M")
        child = db.create_person(self.conn, "Sami", sex="M", father_id=tanios)

        sid = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Semaan", sex="M"),
                S.person(S.MOTHER, "Rima", sex="F"),
            ],
            S.subject(person_id=child, label="Sami"),
        )
        self.conn.commit()
        with self.assertRaises(review.Blocked):
            review.approve(self.conn, sid, reviewed_by=1)

        # Nothing was half-applied by the refused attempt.
        self.assertEqual(
            db.get_person(self.conn, child)["father_id"], tanios
        )
        self.assertEqual(
            db.get_submission(self.conn, sid)["status"], "pending"
        )

        # Saying it deliberately replaces the link.
        review.approve(self.conn, sid, reviewed_by=1, force=True)
        new_father = db.get_person(self.conn, child)["father_id"]
        self.assertEqual(
            db.get_person(self.conn, new_father)["given_name"], "Semaan"
        )

    def test_a_stale_submission_ref_resolves_to_the_approved_person(self):
        # A contributor's saved session can point at a submission from
        # before a round of approvals. The bot must find the person the
        # ref means — or it re-asks what the tree already answers.
        import asyncio

        from bot import store

        child = db.create_person(self.conn, "Sami", sex="M")
        sid = self.add_parents_of(child, "Tanios", "Zeina")
        self.conn.commit()

        zeina = self.person_named("Zeina")
        resolved = asyncio.run(
            store.resolved_person_id({"submission_id": sid, "label": "Zeina"})
        )
        self.assertEqual(resolved, zeina["id"])

        # A ref at a still-pending submission resolves to nobody — quietly.
        pending = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Semaan", sex="M"),
                S.person(S.MOTHER, "Rima", sex="F"),
            ],
            S.subject(submission_id=sid, label="Zeina"),
        )
        follow_up = self.queue(
            S.ADD_PARENTS,
            [S.person(S.FATHER, "Antoun", sex="M")],
            S.subject(submission_id=pending, label="Semaan"),
        )
        self.conn.commit()
        payload = db.submission_payload(db.get_submission(self.conn, follow_up))
        self.assertIsNone(
            asyncio.run(store.resolved_person_id(payload["about"]))
        )

    def test_a_conflicting_first_hand_claim_asks_the_standing_author(self):
        # 8001 put Sami's parents down, reviewed and approved. Later Sami
        # himself signs in and names different parents. The system asks
        # 8001 how confident they are — and only 8001 may answer.
        import asyncio

        from bot import store

        sami = db.create_person(self.conn, "Sami", sex="M")
        standing = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Tanios", sex="M"),
                S.person(S.MOTHER, "Zeina", sex="F"),
            ],
            S.subject(person_id=sami, label="Sami"),
            user=8001,
        )
        review.approve(self.conn, standing, reviewed_by=1, force=True)
        db.upsert_contributor(self.conn, 8002, linked_person_id=sami)
        disputed = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Semaan", sex="M"),
                S.person(S.MOTHER, "Rima", sex="F"),
            ],
            S.subject(person_id=sami, label="Sami"),
            user=8002,
        )
        self.conn.commit()

        verdict = asyncio.run(store.auto_review(8002, disputed))
        self.assertEqual(verdict["tier"], "yellow")
        self.assertIsNotNone(verdict["outreach"], "nobody was asked")
        self.assertEqual(verdict["outreach"]["chat_id"], 8001)
        self.assertEqual(
            db.get_submission(self.conn, disputed)["status"], "pending"
        )

        check_id = verdict["outreach"]["check_id"]
        # A stranger's answer is refused; the person asked gets recorded.
        self.assertFalse(
            asyncio.run(store.answer_peer_check(9999, check_id, "stands"))
        )
        self.assertTrue(
            asyncio.run(store.answer_peer_check(8001, check_id, "concedes"))
        )
        answered = db.peer_checks_for(self.conn, disputed)[0]
        self.assertEqual(answered["verdict"], "concedes")

    def test_a_mothers_maiden_name_is_not_a_variant_of_her_husbands(self):
        child = db.create_person(self.conn, "Sami", sex="M")
        sid = self.queue(
            S.ADD_PARENTS,
            [
                S.person(S.FATHER, "Tanios", sex="M"),
                S.person(S.MOTHER, "Zeina", sex="F", family_name="Taouk"),
            ],
            S.subject(person_id=child, label="Sami"),
        )
        review.approve(self.conn, sid, reviewed_by=1, force=True)

        tanios = self.person_named("Tanios")
        spellings = {c["spelling"] for c in db.spelling_claims(self.conn, tanios["id"])}
        self.assertNotIn("Taouk", spellings)


class IdentityTests(ReviewTestCase):
    """Two men called Toufic in one conversation is not a hypothetical."""

    def test_the_english_name_survives_approval(self):
        """It was being dropped at the moment of approval, silently."""
        sid = self.queue(
            S.ADD_SIBLING,
            [S.person(S.SIBLING, "Hanna", sex="M", also_known_as="John")],
            S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
        )
        created = review.approve(self.conn, sid, reviewed_by=1, force=True)
        row = db.get_person(self.conn, created[0])
        self.assertEqual(row["also_known_as"], "John")
        self.assertIn("(John)", db.display_name_with_also_known_as(row))

    def test_the_number_is_unique_and_never_reused(self):
        first = db.create_person(self.conn, "Toufic", sex="M")
        second = db.create_person(self.conn, "Toufic", sex="M")
        self.assertNotEqual(first, second)

    def test_the_fathers_name_separates_two_men_who_share_a_given_name(self):
        """Steven's brother Toufic and his grandfather Toufic."""
        grandfather = db.create_person(self.conn, "Toufic", sex="M")
        father = db.create_person(self.conn, "Kalim", sex="M", father_id=grandfather)
        brother = db.create_person(self.conn, "Toufic", sex="M", father_id=father)

        self.assertEqual(
            db.row_display_name(db.get_person(self.conn, grandfather)),
            "Toufic Sukkar",
        )
        self.assertEqual(
            db.row_display_name(db.get_person(self.conn, brother)),
            "Toufic Kalim Sukkar",
        )

    def test_a_genuine_collision_is_surfaced_not_hidden(self):
        """Two Joseph Kalim Sukkars. The number is the only thing left."""
        father = db.create_person(self.conn, "Kalim", sex="M")
        a = db.create_person(self.conn, "Joseph", sex="M", father_id=father)
        b = db.create_person(self.conn, "Joseph", sex="M", father_id=father)

        collisions = db.find_name_collisions(self.conn)
        names = [name for name, _rows in collisions]
        self.assertIn("Joseph Kalim Sukkar", names)
        self.assertNotEqual(a, b)


class ClosenessTests(ReviewTestCase):
    def test_a_person_is_no_distance_from_themselves(self):
        self.assertEqual(
            db.relationship_distance(
                self.conn, self.ids["khalil_y"], self.ids["khalil_y"]
            ),
            0,
        )

    def test_a_father_is_one_step(self):
        self.assertEqual(
            db.relationship_distance(
                self.conn, self.ids["khalil_y"], self.ids["youssef"]
            ),
            1,
        )

    def test_a_spouse_is_one_step(self):
        self.assertEqual(
            db.relationship_distance(
                self.conn, self.ids["youssef"], self.ids["nada"]
            ),
            1,
        )

    def test_a_brother_is_two_steps(self):
        self.assertEqual(
            db.relationship_distance(
                self.conn, self.ids["khalil_y"], self.ids["georges"]
            ),
            2,
        )

    def test_a_cousin_is_further_than_a_sibling(self):
        cousin = db.relationship_distance(
            self.conn, self.ids["georges"], self.ids["antoun"]
        )
        sibling = db.relationship_distance(
            self.conn, self.ids["khalil_y"], self.ids["georges"]
        )
        self.assertGreater(cousin, sibling)

    def test_marrying_your_cousin_makes_you_close_by_two_routes(self):
        """Not a quirk — it is the reason this is a graph.

        Mariam and Khalil married, and they are also cousins. The marriage is
        the shorter path, so they come out one step apart. A tree structure
        could not hold both facts at once.
        """
        self.assertEqual(
            db.relationship_distance(
                self.conn, self.ids["mariam"], self.ids["khalil_a"]
            ),
            1,
        )
        ancestors = db.get_ancestors(self.conn, self.ids["joseph"])
        self.assertIn(self.ids["youssef"], ancestors)
        self.assertIn(self.ids["boutros"], ancestors)

    def test_somebody_unconnected_has_no_distance(self):
        stranger = db.create_person(self.conn, "Zaher", sex="M")
        self.assertIsNone(
            db.relationship_distance(self.conn, stranger, self.ids["khalil_y"])
        )

    def test_closeness_reads_as_a_sentence(self):
        self.assertEqual(db.closeness(0), "themselves")
        self.assertIn("parent", db.closeness(1))
        self.assertIn("not connected", db.closeness(None))


class ProvenanceTests(ReviewTestCase):
    """Who says so, and how would they know?"""

    def claim_about(self, person_id, teller_person_id, teller_user, label, **extra):
        payload = S.build(
            S.ADD_SIBLING,
            submitted_by=S.submitter(teller_user, teller_person_id, label),
            about=S.subject(person_id=self.ids["khalil_y"], label="Khalil"),
            people=[S.person(S.SIBLING, "Georges", sex="M")],
            **extra,
        )
        submission_id = db.add_submission(self.conn, teller_user, payload)
        db.resolve_submission(
            self.conn, submission_id, "merged", 1, resulting_person_id=person_id
        )
        return submission_id

    def test_every_claim_is_kept_with_who_made_it(self):
        georges = self.ids["georges"]
        self.claim_about(georges, self.ids["khalil_y"], 111, "Khalil")
        self.claim_about(georges, self.ids["mariam"], 222, "Mariam")

        claims = db.provenance(self.conn, georges)
        self.assertEqual(len(claims), 2)
        # The teller is named as they are called *now*, not as the label
        # happened to read when they pressed send.
        self.assertEqual(
            {c["told_by"] for c in claims},
            {"Khalil Youssef Sukkar", "Mariam Khalil Sukkar"},
        )

    def test_the_closest_teller_comes_first(self):
        """A brother sorts above a niece. It is an ordering, not a ruling."""
        georges = self.ids["georges"]
        self.claim_about(georges, self.ids["mariam"], 222, "Mariam")
        self.claim_about(georges, self.ids["khalil_y"], 111, "Khalil")

        claims = db.provenance(self.conn, georges)
        self.assertEqual(claims[0]["told_by"], "Khalil Youssef Sukkar")
        self.assertLess(claims[0]["distance"], claims[1]["distance"])

    def test_second_hand_knowledge_is_recorded_as_such(self):
        georges = self.ids["georges"]
        self.claim_about(
            georges, self.ids["khalil_y"], 111, "Khalil", source="his mother Nada"
        )
        self.assertEqual(
            db.provenance(self.conn, georges)[0]["heard_from"], "his mother Nada"
        )

    def test_a_teller_with_no_place_in_the_tree_sorts_last(self):
        georges = self.ids["georges"]
        self.claim_about(georges, None, 333, "someone new")
        self.claim_about(georges, self.ids["khalil_y"], 111, "Khalil")

        claims = db.provenance(self.conn, georges)
        self.assertEqual(claims[0]["told_by"], "Khalil Youssef Sukkar")
        self.assertIsNone(claims[-1]["distance"])

    def test_approving_never_erases_what_was_submitted(self):
        """The whole model rests on this: claims are append-only."""
        georges = self.ids["georges"]
        submission_id = self.claim_about(
            georges, self.ids["khalil_y"], 111, "Khalil"
        )
        before = db.get_submission(self.conn, submission_id)["payload_json"]

        # A second, contradicting claim.
        self.claim_about(georges, self.ids["mariam"], 222, "Mariam")

        after = db.get_submission(self.conn, submission_id)["payload_json"]
        self.assertEqual(after, before)
        self.assertEqual(len(db.provenance(self.conn, georges)), 2)

    def test_a_hand_seeded_person_has_no_claims_and_that_is_fine(self):
        self.assertEqual(db.provenance(self.conn, self.ids["elias"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
