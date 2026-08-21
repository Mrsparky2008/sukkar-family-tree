"""
Tests for the Telegram capture step.

Driven through `tests/harness.py`, which fakes the transport but uses the real
handlers and the real routing table, so the callback patterns in `bot/main.py`
are covered too.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram.ext import CommandHandler, ConversationHandler  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402
import submissions  # noqa: E402
from bot import flows, texts  # noqa: E402
from tests.harness import Conversation  # noqa: E402


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """A temporary database, seeded, with config pointed at it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original_path = config.DATABASE_PATH
        config.DATABASE_PATH = Path(self._tmp.name) / "test.db"

        conn = db.connect()
        db.init_db(conn)
        self.ids = seed.load(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        config.DATABASE_PATH = self._original_path
        self._tmp.cleanup()

    # --- helpers ----------------------------------------------------------

    def queued(self) -> list[dict]:
        conn = db.connect()
        try:
            rows = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
            return [
                {
                    "id": row["id"],
                    "telegram_user_id": row["telegram_user_id"],
                    "status": row["status"],
                    "matched_person_id": row["matched_person_id"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def people_count(self) -> int:
        conn = db.connect()
        try:
            return db.count_people(conn)
        finally:
            conn.close()

    async def identified_as_khalil(self, user_id: int = 5001) -> Conversation:
        """A contributor who has signed up and linked to a seeded person."""
        chat = Conversation(user_id=user_id)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Youssef")
        await chat.tap("Khalil Youssef")
        return chat

    async def send_basket(self, chat: Conversation):
        await chat.tap("Review and send")
        await chat.tap("Send all")


# ===========================================================================
# Identification
# ===========================================================================


class IdentificationTests(BotTestCase):
    async def test_start_greets_and_asks_for_a_first_name(self):
        chat = Conversation()
        await chat.start()
        self.assertIn(config.FAMILY_NAME, chat.transcript())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)

    async def test_one_question_per_message(self):
        chat = Conversation()
        await chat.start()
        self.assertEqual(len(chat.sent), 2)
        self.assertEqual(chat.text.count("?"), 1)

    async def test_a_typed_name_is_not_read_back(self):
        """Confirming every answer doubled the taps; the review screen does it."""
        chat = Conversation()
        await chat.start()
        await chat.say("Steven")
        self.assertNotIn(texts.YES, chat.buttons)
        self.assertIn(texts.ASK_SELF_FAMILY, chat.text)

    async def test_full_name_is_rejected_with_an_explanation(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Khalil Youssef Sukkar")
        self.assertIn("first name", chat.transcript().lower())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)

    async def test_known_person_is_offered_as_a_match(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Youssef")
        self.assertIn("Khalil Youssef Sukkar", str(chat.buttons))

    async def test_confirming_a_match_links_the_contributor(self):
        await self.identified_as_khalil()
        conn = db.connect()
        try:
            contributor = db.get_contributor(conn, 5001)
            self.assertEqual(contributor["linked_person_id"], self.ids["khalil_y"])
            self.assertIsNotNone(contributor["branch_id"])
        finally:
            conn.close()

    async def test_linking_does_not_create_a_person(self):
        before = self.people_count()
        await self.identified_as_khalil()
        self.assertEqual(self.people_count(), before)

    async def test_unknown_person_is_queued_not_created(self):
        chat = Conversation(user_id=5002)
        before = self.people_count()
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Fadi")

        self.assertEqual(self.people_count(), before, "constraint 4 violated")
        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["payload"]["kind"], submissions.IDENTIFY)
        self.assertEqual(queued[0]["status"], "pending")

    async def test_none_of_these_queues_them_as_new(self):
        chat = Conversation(user_id=5003)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Youssef")
        await chat.tap(texts.IDENTITY_NONE_OF_THESE)

        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["payload"]["kind"], submissions.IDENTIFY)
        self.assertEqual(self.people_count(), 12)

    async def test_returning_user_is_greeted_by_name(self):
        await self.identified_as_khalil()
        again = Conversation(user_id=5001)
        await again.start()
        self.assertIn("Khalil Youssef Sukkar", again.text)
        self.assertIn(texts.MENU_ADD_CHILD, again.buttons)

    async def test_returning_user_with_identity_pending_is_not_asked_twice(self):
        chat = Conversation(user_id=5004)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Fadi")

        again = Conversation(user_id=5004)
        await again.start()
        self.assertNotIn(texts.ASK_SELF_GIVEN, again.transcript())
        self.assertIn(texts.MENU_ADD_CHILD, again.buttons)
        self.assertEqual(len(self.queued()), 1)


# ===========================================================================
# The family name
# ===========================================================================


class FamilyNameTests(BotTestCase):
    """Several spellings of one family. Ask, never assume."""

    async def test_every_configured_spelling_is_offered(self):
        chat = Conversation(user_id=5100)
        await chat.start()
        await chat.say("Steven")
        for variant in config.FAMILY_NAME_VARIANTS:
            self.assertIn(variant, chat.buttons)
        self.assertIn(texts.FAMILY_OTHER, chat.buttons)

    async def test_the_chosen_spelling_is_what_gets_stored(self):
        chat = Conversation(user_id=5101)
        await chat.start()
        await chat.say("Steven")
        await chat.tap("Succar")
        await chat.say("Kalim")

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["family_name"], "Succar")

    async def test_an_unlisted_spelling_can_be_typed(self):
        chat = Conversation(user_id=5102)
        await chat.start()
        await chat.say("Steven")
        await chat.tap(texts.FAMILY_OTHER)
        self.assertIn(texts.ASK_FAMILY_OTHER, chat.text)
        await chat.say("Soukar")
        await chat.say("Kalim")

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["family_name"], "Soukar")

    def test_variants_are_one_family_for_matching(self):
        for variant in config.FAMILY_NAME_VARIANTS:
            self.assertEqual(db.canonical_family_name(variant), config.FAMILY_NAME)
        self.assertTrue(db.same_family("Succar", "Soukkar"))

    def test_a_married_in_family_name_is_left_alone(self):
        self.assertEqual(db.canonical_family_name("Karam"), "Karam")
        self.assertFalse(db.same_family("Karam", config.FAMILY_NAME))

    async def test_an_unlisted_spelling_becomes_a_known_variant(self):
        """All of them are one family, including spellings nobody listed."""
        conn = db.connect()
        try:
            self.assertNotEqual(
                db.canonical_family_name("Soukar", conn), config.FAMILY_NAME
            )
        finally:
            conn.close()

        chat = Conversation(user_id=5103)
        await chat.start()
        await chat.say("Steven")
        await chat.tap(texts.FAMILY_OTHER)
        await chat.say("Soukar")
        await chat.say("Kalim")

        conn = db.connect()
        try:
            self.assertEqual(
                db.canonical_family_name("Soukar", conn), config.FAMILY_NAME
            )
            self.assertTrue(db.same_family("Soukar", "Succar", conn))
        finally:
            conn.close()

    async def test_a_mothers_maiden_name_is_never_learned_as_a_variant(self):
        """Only the "how do you spell OUR name" answer counts."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Nada")
        await chat.say("Karam")
        await self.send_basket(chat)

        conn = db.connect()
        try:
            self.assertEqual(db.canonical_family_name("Karam", conn), "Karam")
        finally:
            conn.close()

    async def test_a_different_family_scores_lower(self):
        conn = db.connect()
        try:
            same = db.corroborate(
                conn, "Georges", role="sibling",
                subject_person_id=self.ids["khalil_y"],
                family_name="Succar", threshold=0.0,
            )
            other = db.corroborate(
                conn, "Georges", role="sibling",
                subject_person_id=self.ids["khalil_y"],
                family_name="Karam", threshold=0.0,
            )
        finally:
            conn.close()
        self.assertGreater(same[0]["score"], other[0]["score"])


# ===========================================================================
# The menu and the cursor
# ===========================================================================


class MenuTests(BotTestCase):
    async def test_menu_offers_every_option(self):
        chat = await self.identified_as_khalil()
        self.assertEqual(
            set(chat.buttons),
            {
                texts.MENU_ADD_PARENTS,
                texts.MENU_ADD_SIBLING,
                texts.MENU_ADD_SPOUSE,
                texts.MENU_ADD_CHILD,
                texts.MENU_SWITCH,
                texts.MENU_FIX,
                texts.MENU_VIEW,
            },
        )

    async def test_view_tree_says_so_when_unpublished(self):
        chat = await self.identified_as_khalil()
        original, config.PUBLIC_URL = config.PUBLIC_URL, ""
        try:
            await chat.tap(texts.MENU_VIEW)
            self.assertIn("isn't published yet", chat.transcript())
        finally:
            config.PUBLIC_URL = original

    async def test_view_tree_sends_the_link_when_published(self):
        chat = await self.identified_as_khalil()
        original, config.PUBLIC_URL = config.PUBLIC_URL, "https://example.org/tree"
        try:
            await chat.tap(texts.MENU_VIEW)
            self.assertIn("https://example.org/tree", chat.transcript())
        finally:
            config.PUBLIC_URL = original


# ===========================================================================
# Collecting
# ===========================================================================


class AddChildTests(BotTestCase):
    async def test_nothing_is_queued_until_the_basket_is_sent(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")

        self.assertEqual(self.queued(), [], "queued before the contributor sent it")
        await self.send_basket(chat)

        queued = self.queued()
        self.assertEqual(len(queued), 1)
        payload = queued[0]["payload"]
        self.assertEqual(payload["kind"], submissions.ADD_CHILD)
        self.assertEqual(payload["people"][0]["given_name"], "Rita")
        self.assertEqual(payload["people"][0]["sex"], "F")
        self.assertEqual(payload["about"]["person_id"], self.ids["khalil_y"])

    async def test_adding_never_creates_a_person(self):
        before = self.people_count()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        await self.send_basket(chat)
        self.assertEqual(self.people_count(), before, "constraint 4 violated")

    async def test_a_save_offers_the_next_step(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        self.assertIn("Rita", chat.text)
        self.assertIn(texts.CLIMB_YES, chat.buttons)

    async def test_carrying_on_returns_to_the_menu(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        await chat.tap(texts.ADD_MORE)
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)


class AddParentsTests(BotTestCase):
    async def test_the_father_is_not_asked_for_twice(self):
        """It was given at signup. Asking again reads as not listening."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        self.assertIn("mother", chat.text.lower())

        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        roles = {e["role"]: e for e in self.queued()[0]["payload"]["people"]}
        self.assertEqual(roles["father"]["given_name"], "Youssef")
        self.assertEqual(roles["mother"]["given_name"], "Nada")

    async def test_both_parents_when_the_father_is_unknown(self):
        chat = Conversation(user_id=5200)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Fares")             # own father: always known, now required
        await chat.tap(texts.MENU_ADD_PARENTS)
        # father pre-filled from signup; only the mother is asked
        await chat.say("Nada")
        await chat.say("Karam")
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        payload = [q for q in self.queued() if q["payload"]["kind"] == submissions.ADD_PARENTS][0]
        roles = {e["role"]: e for e in payload["payload"]["people"]}
        self.assertEqual(roles["father"]["given_name"], "Fares")
        self.assertEqual(roles["mother"]["family_name"], "Karam")

    async def test_skipping_the_mother_skips_her_family_name_too(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        self.assertNotIn("before she married", chat.text)

    async def test_giving_nothing_explains_and_saves_nothing(self):
        chat = Conversation(user_id=5201)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Fares")
        before = len(self.queued())
        # Point the cursor at their own pending record: no pre-filled father
        # there, so both parents can be skipped — and skipping both is nothing.
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap("Zaher")
        await chat.tap("Add Zaher Sukkar")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.SKIP)
        self.assertIn("nothing to send", chat.transcript())
        self.assertEqual(len(self.queued()), before)


class AddSiblingAndSpouseTests(BotTestCase):
    async def test_sibling_wording_follows_the_choice(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_SISTER)
        self.assertIn("her first name", chat.text)

        await chat.say("Mariam")
        await self.send_basket(chat)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SIBLING)
        self.assertEqual(entry["sex"], "F")

    async def test_spouse_records_a_family_name(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SPOUSE)
        await chat.tap(texts.SPOUSE_WIFE)
        await chat.say("Therese")
        await chat.say("Obeid")
        await self.send_basket(chat)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SPOUSE)
        self.assertEqual(entry["family_name"], "Obeid")


# ===========================================================================
# Climbing
# ===========================================================================


class ClimbTests(BotTestCase):
    async def test_the_cursor_follows_the_person_just_named(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CLIMB_YES)
        self.assertIn("Youssef's father", chat.text)

    async def test_three_generations_in_one_sitting(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)              # mother unknown
        await chat.tap(texts.CLIMB_YES)         # now on Youssef
        await chat.say("Elias")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CLIMB_YES)         # now on Elias
        await chat.say("Semaan")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        names = [
            q["payload"]["people"][0]["given_name"]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_PARENTS
        ]
        self.assertEqual(names, ["Youssef", "Elias", "Semaan"])

    async def test_the_chain_is_anchored_when_it_sends(self):
        """Each generation must point at the one below, not float free."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CLIMB_YES)
        await chat.say("Elias")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        parents = [
            q for q in self.queued() if q["payload"]["kind"] == submissions.ADD_PARENTS
        ]
        first, second = parents[0], parents[1]
        self.assertEqual(first["payload"]["about"]["person_id"], self.ids["khalil_y"])
        self.assertEqual(second["payload"]["about"]["submission_id"], first["id"])
        self.assertNotIn("draft_id", second["payload"]["about"])

    async def test_the_prompt_names_whoever_the_cursor_is_on(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CLIMB_YES)
        await chat.say("Elias")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.ADD_MORE)
        # Declining the climb leaves the cursor where it was, on Youssef.
        self.assertIn("Adding relatives for: Youssef", chat.text)
        self.assertIn("Add Youssef's parents", chat.buttons)
        await chat.tap("Add a brother or sister of Youssef")
        self.assertIn("brother or a sister of Youssef", chat.text)


class SwitchSubjectTests(BotTestCase):
    async def test_the_contributors_relatives_are_offered(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        offered = " ".join(chat.buttons)
        self.assertIn("Georges Youssef Sukkar", offered)   # his brother
        self.assertIn("Youssef Elias Sukkar", offered)     # his father

    async def test_switching_moves_the_cursor(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap("Georges Youssef")
        self.assertIn("Adding relatives for: Georges Youssef Sukkar", chat.text)

        await chat.tap("Add a child of Georges")
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await self.send_basket(chat)
        self.assertEqual(
            self.queued()[0]["payload"]["about"]["person_id"], self.ids["georges"]
        )


# ===========================================================================
# Review and send
# ===========================================================================


class ReviewTests(BotTestCase):
    async def collect_two(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Ritta")
        await chat.tap(texts.ADD_MORE)
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_BROTHER)
        await chat.say("Sami")
        await chat.tap(texts.ADD_MORE)
        return chat

    async def test_review_lists_everything_collected(self):
        chat = await self.collect_two()
        await chat.tap("Review and send")
        self.assertIn("Ritta", chat.text)
        self.assertIn("Sami", chat.text)
        self.assertIn("Send all 2", " ".join(chat.buttons))

    async def test_a_misspelling_can_be_corrected_before_it_sends(self):
        chat = await self.collect_two()
        await chat.tap("Review and send")
        await chat.tap("Ritta")
        self.assertIn("What should Ritta be?", chat.text)
        await chat.say("Rita")
        self.assertIn("Rita", chat.text)

        await chat.tap("Send all")
        names = [q["payload"]["people"][0]["given_name"] for q in self.queued()]
        self.assertIn("Rita", names)
        self.assertNotIn("Ritta", names)

    async def test_an_entry_can_be_removed(self):
        chat = await self.collect_two()
        await chat.tap("Review and send")
        await chat.tap("Sami")
        await chat.tap(texts.REMOVE)
        await chat.tap("Send all")

        names = [q["payload"]["people"][0]["given_name"] for q in self.queued()]
        self.assertNotIn("Sami", names)
        self.assertIn("Ritta", names)

    async def test_removing_a_parent_removes_what_hung_off_it(self):
        """Otherwise a grandfather is left anchored to nothing."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CLIMB_YES)
        await chat.say("Elias")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.ADD_MORE)

        await chat.tap("Review and send")
        await chat.tap("1. Youssef")
        await chat.tap(texts.REMOVE)
        self.assertIn(texts.REVIEW_EMPTY, chat.transcript())

    async def test_the_basket_survives_going_back_for_more(self):
        chat = await self.collect_two()
        await chat.tap("Review and send")
        await chat.tap(texts.ADD_MORE)
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Tanios")
        await chat.tap(texts.ADD_MORE)
        await chat.tap("Review and send")
        self.assertIn("Send all 3", " ".join(chat.buttons))

    async def test_sending_empties_the_basket(self):
        chat = await self.collect_two()
        await self.send_basket(chat)
        self.assertEqual(len(self.queued()), 2)
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Tanios")
        await chat.tap(texts.ADD_MORE)
        await chat.tap("Review and send")
        self.assertIn("Send all 1", " ".join(chat.buttons))


# ===========================================================================
# Duplicates
# ===========================================================================


class DuplicateFlaggingTests(BotTestCase):
    async def test_probable_duplicate_is_flagged_for_the_admin(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_BROTHER)
        await chat.say("Georges")
        await self.send_basket(chat)
        self.assertEqual(self.queued()[0]["matched_person_id"], self.ids["georges"])

    async def test_flagging_never_merges_or_rejects(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_BROTHER)
        await chat.say("Georges")
        await self.send_basket(chat)
        self.assertEqual(self.queued()[0]["status"], "pending")
        self.assertEqual(self.people_count(), 12)

    async def test_unrelated_name_is_not_flagged(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Zaher")
        await self.send_basket(chat)
        self.assertIsNone(self.queued()[0]["matched_person_id"])


# ===========================================================================
# Fix something I submitted
# ===========================================================================


class FixSomethingTests(BotTestCase):
    async def test_nothing_to_fix_yet(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_FIX)
        self.assertIn(texts.FIX_NOTHING_YET, chat.transcript())

    async def test_correction_goes_to_the_queue_as_a_suggestion(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Ritta")
        await self.send_basket(chat)

        await chat.tap(texts.MENU_FIX)
        self.assertIn("Ritta", str(chat.buttons))
        await chat.tap("Ritta")
        await chat.say("Her name is Rita, one t")
        await chat.tap(texts.SEND_IT)

        queued = self.queued()
        self.assertEqual(len(queued), 2)
        correction = queued[1]["payload"]
        self.assertEqual(correction["kind"], submissions.CORRECTION)
        self.assertEqual(correction["target_submission_id"], queued[0]["id"])
        self.assertIn("one t", correction["note"])

    async def test_a_correction_changes_nothing_live(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await self.send_basket(chat)
        original = self.queued()[0]["payload"]

        await chat.tap(texts.MENU_FIX)
        await chat.tap("Sami")
        await chat.say("Actually he is Samir")
        await chat.tap(texts.SEND_IT)

        self.assertEqual(self.people_count(), 12)
        self.assertEqual(self.queued()[0]["payload"], original)
        for row in self.queued():
            self.assertEqual(row["status"], "pending")


# ===========================================================================
# The rules the bot could quietly break
# ===========================================================================


class ConstraintTests(BotTestCase):
    def test_bot_never_calls_a_privileged_write(self):
        forbidden = re.compile(
            r"\b(?:db\.)?(create_person|update_person|create_union|"
            r"assign_branches|sync_branches|set_branch_founder)\s*\("
        )
        root = Path(__file__).resolve().parents[1] / "bot"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = forbidden.search(line)
                if match:
                    offenders.append(f"bot/{path.name}:{number} calls {match.group(1)}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_bot_writes_only_through_the_store(self):
        root = Path(__file__).resolve().parents[1] / "bot"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "store.py":
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bINSERT\s+INTO\b", text, re.IGNORECASE):
                offenders.append(f"bot/{path.name} writes SQL")
            if "db.connect" in text:
                offenders.append(f"bot/{path.name} opens its own connection")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_no_flow_can_ask_for_a_date(self):
        forbidden = re.compile(
            r"\b(year|years|born|birth|birthday|died|death|date|dates|age|ages|when)\b"
        )
        for kind, flow in flows.BY_KIND.items():
            for step in flow.steps:
                prompt = step.text({"sex": "M"}).lower()
                self.assertIsNone(
                    forbidden.search(prompt),
                    f"{kind}.{step.id} asks for a date: {prompt!r}",
                )

    def test_no_family_name_hardcoded_in_the_bot(self):
        root = Path(__file__).resolve().parents[1] / "bot"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in (config.FAMILY_NAME, config.VILLAGE):
                if needle in text:
                    offenders.append(f"bot/{path.name} contains {needle!r}")
        self.assertEqual(offenders, [], "; ".join(offenders))

    async def test_every_menu_option_reaches_a_working_flow(self):
        chat = await self.identified_as_khalil()
        for label in list(chat.buttons):
            # A linked contributor goes straight to the menu on /start.
            fresh = Conversation(user_id=5001)
            await fresh.start()
            await fresh.tap(label)
            self.assertTrue(fresh.text, f"{label} produced nothing")
            self.assertTrue(
                fresh.buttons or "?" in fresh.text,
                f"{label} left the contributor with no way forward",
            )


class RobustnessTests(BotTestCase):
    async def test_typing_instead_of_tapping_a_choice_re_asks(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.say("a boy I think")
        self.assertIn(texts.NOT_UNDERSTOOD, chat.transcript())
        self.assertIn(texts.ASK_CHILD_SEX, chat.text)

    async def test_empty_message_is_rejected_kindly(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("   ")
        self.assertIn(texts.NAME_EMPTY, chat.transcript())

    async def test_absurdly_long_name_is_rejected(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("K" * 200)
        self.assertIn(texts.NAME_TOO_LONG, chat.transcript())

    async def test_whitespace_around_a_name_is_trimmed(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("  Sami  ")
        await self.send_basket(chat)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Sami"
        )

    async def test_arabic_name_survives_the_round_trip(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("خليل")
        await self.send_basket(chat)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "خليل"
        )


class UnderstandingTypedAnswersTests(BotTestCase):
    """People answer button questions with words. Read them."""

    async def test_trailing_punctuation_never_reaches_a_name(self):
        chat = Conversation(user_id=5300)
        await chat.start()
        await chat.say("Steven.")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Kalim,")

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["given_name"], "Steven")
        self.assertEqual(entry["father_given_name"], "Kalim")

    async def test_punctuation_inside_a_name_survives(self):
        chat = Conversation(user_id=5301)
        await chat.start()
        await chat.say("Abou-Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.say("Fares")
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Abou-Khalil"
        )

    async def test_a_spelling_typed_out_loud_finds_its_button(self):
        """"Su K ar" is somebody spelling their own surname."""
        chat = Conversation(user_id=5302)
        await chat.start()
        await chat.say("Steven")
        await chat.say("Su K ar")
        await chat.say("Kalim")
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["family_name"], "Sukar"
        )

    async def test_a_typed_choice_works_for_any_button_question(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.say("daughter")
        await chat.say("Rita")
        await self.send_basket(chat)
        self.assertEqual(self.queued()[0]["payload"]["people"][0]["sex"], "F")

    async def test_gibberish_at_a_choice_still_re_asks(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.say("qqqq zzz")
        self.assertIn(texts.NOT_UNDERSTOOD, chat.transcript())
        self.assertIn(texts.ASK_CHILD_SEX, chat.text)

    async def test_dunno_counts_as_i_dont_know(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("dunno")
        self.assertIn(texts.CLIMB_YES, chat.buttons)

    async def test_ok_is_a_yes_at_the_climb(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.say("Ok")
        self.assertIn("Youssef's father", chat.text)

    async def test_nah_is_a_no_at_the_climb(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.say("nah")
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)

    async def test_unclear_text_re_shows_the_question_with_its_buttons(self):
        """The old behaviour scolded and left them with no buttons at all."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.say("hmm what")
        self.assertIn(texts.CLIMB_YES, chat.buttons)
        self.assertIn("parents", chat.text)

    async def test_typing_send_at_the_review_screen_sends(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.ADD_MORE)
        await chat.tap("Review and send")
        await chat.say("yes")
        self.assertEqual(len(self.queued()), 1)

    async def test_unclear_text_at_the_review_screen_re_shows_the_list(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.ADD_MORE)
        await chat.tap("Review and send")
        await chat.say("what is this")
        self.assertIn("Sami", chat.text)
        self.assertIn("Send all 1", " ".join(chat.buttons))
        self.assertEqual(self.queued(), [])


class WiringTests(unittest.TestCase):
    def test_application_builds_with_every_handler_registered(self):
        from bot.main import build_application

        app = build_application("123456:FAKE-TOKEN-FOR-CONSTRUCTION-ONLY")
        registered = [h for group in app.handlers.values() for h in group]
        self.assertTrue(any(isinstance(h, ConversationHandler) for h in registered))
        commands = {
            name
            for h in registered
            if isinstance(h, CommandHandler)
            for name in h.commands
        }
        self.assertLessEqual({"share", "help"}, commands)

    def test_every_state_can_be_left(self):
        from bot.main import build_conversation

        conversation = build_conversation()
        for state, handlers_in_state in conversation.states.items():
            self.assertTrue(handlers_in_state, f"state {state} has no handlers")
        self.assertTrue(conversation.fallbacks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
