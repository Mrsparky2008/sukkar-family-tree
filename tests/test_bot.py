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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Youssef")
        await chat.tap(texts.SELF_MAN)
        await chat.tap("Khalil Youssef")
        return chat

    async def fresh_contributor(self, user_id: int = 5900) -> Conversation:
        """A contributor the tree knows nothing about: father Fares at signup,
        mother unrecorded — so "Add my parents" is still on the menu. Khalil's
        parents are both seeded, which rightly hides that button for him."""
        chat = Conversation(user_id=user_id)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        # New signups get the guided tour; these tests drive the menu.
        await chat.tap(texts.TOUR_MENU)
        return chat

    async def send_basket(self, chat: Conversation):
        """Confirmed batches send themselves; this mops up whatever remains."""
        if texts.CONFIRM_CORRECT in chat.buttons:
            await chat.tap(texts.CONFIRM_CORRECT)
        if any("Review and send" in label for label in chat.buttons):
            await chat.tap("Review and send")
        if any("Send all" in label for label in chat.buttons):
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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Youssef")
        await chat.tap(texts.SELF_MAN)
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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fadi")
        await chat.tap(texts.SELF_MAN)

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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Youssef")
        await chat.tap(texts.SELF_MAN)
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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fadi")
        await chat.tap(texts.SELF_MAN)

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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim")
        await chat.tap(texts.SELF_MAN)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["family_name"], "Succar")

    async def test_an_unlisted_spelling_can_be_typed(self):
        chat = Conversation(user_id=5102)
        await chat.start()
        await chat.say("Steven")
        await chat.tap(texts.FAMILY_OTHER)
        self.assertIn(texts.ASK_FAMILY_OTHER, chat.text)
        await chat.say("Soukar")
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim")
        await chat.tap(texts.SELF_MAN)

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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim")
        await chat.tap(texts.SELF_MAN)

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
        chat = await self.fresh_contributor()
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
        chat = await self.fresh_contributor()
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

    async def test_recorded_parents_are_not_offered_again(self):
        """Khalil's parents are on the tree. Offering to add them reads as
        if the bot forgot, and answering would only manufacture a duplicate."""
        chat = await self.identified_as_khalil()
        self.assertNotIn(texts.MENU_ADD_PARENTS, chat.buttons)
        self.assertIn("already down", chat.text)
        self.assertIn("Youssef", chat.text)
        self.assertIn("Nada", chat.text)

    async def test_view_tree_says_so_when_unpublished(self):
        chat = await self.identified_as_khalil()
        original, config.PUBLIC_URL = config.PUBLIC_URL, ""
        try:
            await chat.tap(texts.MENU_VIEW)
            self.assertIn("published yet", chat.transcript())
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
        await chat.tap("0")
        await chat.tap("1")
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

    async def test_a_first_hand_uncontested_claim_lands_instantly(self):
        # The system reviewer: an admitted contributor telling their own
        # story, resembling nobody, contradicting nothing — straight onto
        # the tree, marked as the system's forever.
        before = self.people_count()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("0")
        await chat.tap("1")
        await chat.say("Rita")
        await self.send_basket(chat)
        self.assertEqual(self.people_count(), before + 1)
        record = self.queued()[-1]
        self.assertEqual(record["status"], "approved")
        self.assertIn("Straight onto the tree", chat.transcript())
        self.assertIn("(#", chat.transcript())

    async def test_a_save_offers_the_next_step(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        self.assertIn("How many sons", chat.text)
        await chat.tap("0")
        self.assertIn("How many daughters", chat.text)
        await chat.tap("1")
        await chat.say("Rita")
        self.assertIn("Rita is your daughter", chat.text)
        await chat.tap(texts.CONFIRM_CORRECT)
        self.assertIn("1 added", chat.text)

    async def test_carrying_on_returns_to_the_menu(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("0")
        await chat.tap("1")
        await chat.say("Rita")
        await chat.tap(texts.CONFIRM_CORRECT)
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)


class AddParentsTests(BotTestCase):
    async def test_the_father_is_not_asked_for_twice(self):
        """It was given at signup. Asking again reads as not listening."""
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        self.assertIn("mother", chat.text.lower())

        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        payload = [
            q for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_PARENTS
        ][0]
        roles = {e["role"]: e for e in payload["payload"]["people"]}
        self.assertEqual(roles["father"]["given_name"], "Fares")
        self.assertEqual(roles["mother"]["given_name"], "Nada")

    async def test_both_parents_when_the_father_is_unknown(self):
        chat = Conversation(user_id=5200)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")             # own father: always known, now required
        await chat.tap(texts.SELF_MAN)
        await chat.tap(texts.TOUR_MENU)
        await chat.tap(texts.MENU_ADD_PARENTS)
        # father pre-filled from signup; only the mother is asked
        await chat.say("Nada")
        await chat.say("Karam")
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        payload = [q for q in self.queued() if q["payload"]["kind"] == submissions.ADD_PARENTS][0]
        roles = {e["role"]: e for e in payload["payload"]["people"]}
        self.assertEqual(roles["father"]["given_name"], "Fares")
        self.assertEqual(roles["mother"]["family_name"], "Karam")

    async def test_skipping_the_mother_skips_her_family_name_too(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        self.assertNotIn("before she married", chat.text)

    async def test_giving_nothing_explains_and_saves_nothing(self):
        chat = Conversation(user_id=5201)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        await chat.tap(texts.TOUR_MENU)
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
        await chat.tap("0")
        await chat.tap("1")
        await chat.tap(texts.YES_WORD)
        self.assertIn("first sister", chat.text)
        await chat.say("Mariam")
        await self.send_basket(chat)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SIBLING)
        self.assertEqual(entry["sex"], "F")

    async def test_spouse_records_a_family_name(self):
        """Khalil said he is a man at signup, so his spouse is a wife —
        no husband-or-wife question, straight to her name."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SPOUSE)
        self.assertIn("her first name", chat.text)
        await chat.say("Therese")
        await chat.say("Obeid")
        await self.send_basket(chat)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SPOUSE)
        self.assertEqual(entry["family_name"], "Obeid")


class FathersFamilyNameTests(BotTestCase):
    """The father's family name is asked, not assumed.

    Assuming it gave a whole branch a surname nobody stated — the case where
    somebody belongs to this family through their mother, and their father
    does not belong to it at all.
    """

    async def test_it_is_asked_when_climbing_to_someone_elses_parents(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Fares"))
        await chat.say("Elias")
        self.assertIn(config.FAMILY_NAME, chat.buttons)
        self.assertIn(texts.FATHER_FAMILY_OTHER, chat.buttons)

    async def test_a_father_from_another_family_can_be_named(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Fares"))
        await chat.say("Elias")
        await chat.tap(texts.FATHER_FAMILY_OTHER)
        await chat.say("Haddad")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await self.send_basket(chat)

        fathers = [
            e
            for q in self.queued()
            for e in q["payload"].get("people") or []
            if e.get("role") == submissions.FATHER and e["given_name"] == "Elias"
        ]
        self.assertTrue(fathers, self.queued())
        self.assertEqual(fathers[0]["family_name"], "Haddad")

    async def test_your_own_father_is_not_asked_about_twice(self):
        # His given name came from signing in, and the family name they gave
        # for themselves is his — that is where theirs came from.
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        self.assertIn("mother", chat.text.lower())
        self.assertNotIn(texts.FATHER_FAMILY_OTHER, chat.buttons)


# ===========================================================================
# Climbing
# ===========================================================================


class ClimbTests(BotTestCase):
    async def test_the_cursor_follows_the_person_just_named(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Nada")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Fares"))
        self.assertIn("Fares", chat.text)
        self.assertIn("father", chat.text)

    async def test_three_generations_in_one_sitting(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)              # mother unknown
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Fares"))
        await chat.say("Elias")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Elias"))
        await chat.say("Semaan")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        names = [
            q["payload"]["people"][0]["given_name"]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_PARENTS
        ]
        self.assertEqual(names, ["Fares", "Elias", "Semaan"])

    async def test_the_chain_is_anchored_when_it_sends(self):
        """Each generation must point at the one below, not float free."""
        chat = await self.identified_as_khalil()
        # His own parents are on the tree already; the open end is his wife's
        # side, so the climb starts from her.
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap("Therese Obeid")
        await chat.tap("parents")
        await chat.say("Tanios")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Tanios"))
        await chat.say("Boulos")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)
        await self.send_basket(chat)

        parents = [
            q for q in self.queued() if q["payload"]["kind"] == submissions.ADD_PARENTS
        ]
        first, second = parents[0], parents[1]
        self.assertEqual(first["payload"]["about"]["person_id"], self.ids["therese"])
        self.assertEqual(second["payload"]["about"]["submission_id"], first["id"])
        self.assertNotIn("draft_id", second["payload"]["about"])

    async def test_the_prompt_names_whoever_the_cursor_is_on(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_PARENTS_OF.format(name="Fares"))
        await chat.say("Elias")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        # The panel names whoever the cursor is on.
        self.assertIn(
            texts.NEXT_SIBLINGS_OF.format(name="Elias"), chat.buttons
        )
        await chat.tap(texts.NEXT_SIBLINGS_OF.format(name="Elias"))
        self.assertIn("How many brothers did Elias have?", chat.text)


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
        await chat.tap("1")
        await chat.tap("0")
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
        """Dictation is where the pre-send review still lives — buttons send
        on Correct, but a typed paragraph gets read back as a list first."""
        chat = await self.identified_as_khalil()
        await chat.say("My daughter Ritta and my brother Sami")
        return chat  # sitting on the review screen, basket of 2, unsent

    async def test_review_lists_everything_collected(self):
        chat = await self.collect_two()
        self.assertIn("Ritta", chat.text)
        self.assertIn("Sami", chat.text)
        self.assertIn("Send all 2", " ".join(chat.buttons))

    async def test_a_misspelling_can_be_corrected_before_it_sends(self):
        chat = await self.collect_two()
        await chat.tap("Ritta")
        self.assertIn("Fixing Ritta", chat.text)
        await chat.say("Rita")
        self.assertIn("Rita", chat.text)

        await chat.tap("Send all")
        names = [q["payload"]["people"][0]["given_name"] for q in self.queued()]
        self.assertIn("Rita", names)
        self.assertNotIn("Ritta", names)

    async def test_an_entry_can_be_removed(self):
        chat = await self.collect_two()
        await chat.tap("Sami")
        await chat.tap(texts.REMOVE)
        await chat.tap("Send all")

        names = [q["payload"]["people"][0]["given_name"] for q in self.queued()]
        self.assertNotIn("Sami", names)
        self.assertIn("Ritta", names)

    async def test_removing_a_parent_removes_what_hung_off_it(self):
        """Otherwise a grandfather is left anchored to nothing."""
        chat = await self.fresh_contributor()
        await chat.say("My mother is Nada")
        await chat.tap(texts.ADD_MORE)
        await chat.say("Nada's parents are Elias and Salma")
        await chat.tap("1. Nada")
        await chat.tap(texts.REMOVE)
        self.assertIn(texts.REVIEW_EMPTY, chat.transcript())

    async def test_the_basket_survives_going_back_for_more(self):
        chat = await self.collect_two()
        await chat.tap(texts.ADD_MORE)
        await chat.say("My son Tanios")
        self.assertIn("Send all 3", " ".join(chat.buttons))

    async def test_sending_empties_the_basket(self):
        chat = await self.collect_two()
        await chat.tap("Send all")
        self.assertEqual(len(self.queued()), 2)
        await chat.say("My son Tanios")
        self.assertIn("Send all 1", " ".join(chat.buttons))


# ===========================================================================
# Duplicates
# ===========================================================================


class DuplicateFlaggingTests(BotTestCase):
    async def test_probable_duplicate_is_flagged_for_the_admin(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap("1")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Georges")
        await self.send_basket(chat)
        self.assertEqual(self.queued()[0]["matched_person_id"], self.ids["georges"])

    async def test_flagging_never_merges_or_rejects(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap("1")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Georges")
        await self.send_basket(chat)
        self.assertEqual(self.queued()[0]["status"], "pending")
        self.assertEqual(self.people_count(), 12)

    async def test_unrelated_name_is_not_flagged(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("Zaher")
        await self.send_basket(chat)
        self.assertIsNone(self.queued()[0]["matched_person_id"])


# ===========================================================================
# Fix something I submitted
# ===========================================================================


class TreeCorrectionTests(BotTestCase):
    """Names are for humans; the numbers are the permanent reference."""

    async def start_tree_fix(self) -> Conversation:
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_FIX)
        await chat.tap(texts.FIX_TREE)
        return chat

    async def test_a_correction_by_number_names_its_people_back(self):
        khalil = self.ids["khalil_y"]
        chat = await self.start_tree_fix()
        await chat.say(f"#{khalil} is spelled Khaleel")

        # Numbers in, names AND numbers out — confirmed before it sends.
        self.assertIn(f"(#{khalil})", chat.text)
        await chat.tap(texts.SEND_IT)

        record = self.queued()[-1]
        self.assertEqual(record["payload"]["kind"], submissions.CORRECTION)
        self.assertEqual(record["payload"]["target_person_id"], khalil)

    async def test_an_unknown_number_is_bounced_back(self):
        chat = await self.start_tree_fix()
        await chat.say("#9999 has the wrong wife")
        self.assertIn("#9999", chat.text)
        self.assertNotIn(texts.SEND_IT, chat.buttons)

    async def test_a_shared_name_asks_for_the_number(self):
        # The seed holds two Khalils — the name alone settles nothing.
        chat = await self.start_tree_fix()
        await chat.say("Khalil is not married to Nada")

        self.assertIn("more than one Khalil", chat.text)
        self.assertIn(f"(#{self.ids['khalil_y']})", chat.text)
        self.assertIn(f"(#{self.ids['khalil_a']})", chat.text)

        # Sending it again with the number settles it.
        await chat.say(f"#{self.ids['khalil_a']} is not married to Nada")
        self.assertIn(texts.SEND_IT, chat.buttons)

    async def test_a_unique_name_resolves_without_being_asked(self):
        antoun = self.ids["antoun"]
        chat = await self.start_tree_fix()
        await chat.say("Antoun died in the village")
        self.assertIn(f"(#{antoun})", chat.text)
        await chat.tap(texts.SEND_IT)
        self.assertEqual(self.queued()[-1]["payload"]["target_person_id"], antoun)


class NumberedNamesTests(BotTestCase):
    """The bot shows the permanent number wherever it names a tree person,
    so the family learns the numbers by osmosis."""

    async def test_the_picker_and_the_questions_carry_the_number(self):
        boutros = self.ids["boutros"]
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        # The picker buttons already carry numbers — tap the uncle by his.
        await chat.tap(f"(#{boutros})")
        self.assertIn(f"(#{boutros})", chat.text)

        # And the questions about him keep saying it.
        await chat.tap("brother or sister of")
        self.assertIn(f"(#{boutros})", chat.text)


class SystemReviewerTests(BotTestCase):
    """Green flows, yellow talks, people referee.

    The system reviewer approves exactly one shape of claim: first-hand,
    resembling nobody, contradicting nothing. Everything else waits."""

    async def test_an_overlapping_name_goes_yellow_not_green(self):
        # Mariam is already Khalil's daughter — re-entering her must wait
        # for a person, and say so.
        before = self.people_count()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("0")
        await chat.tap("1")
        await chat.say("Mariam")
        await self.send_basket(chat)
        self.assertEqual(self.people_count(), before)
        self.assertEqual(self.queued()[-1]["status"], "pending")
        self.assertIn(texts.QUEUED_FOR_CHECK, chat.transcript())

    async def test_second_hand_claims_wait_quietly(self):
        # About the uncle, not about themselves: the ordinary queue,
        # no announcement either way.
        before = self.people_count()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap(f"(#{self.ids['boutros']})")
        await chat.tap("child of")
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("Fares")
        await chat.tap(texts.CONFIRM_CORRECT)
        self.assertEqual(self.people_count(), before)
        self.assertEqual(self.queued()[-1]["status"], "pending")
        self.assertNotIn("Straight onto the tree", chat.transcript())

    async def test_a_conflicting_claim_over_a_recorded_spouse_waits(self):
        # Khalil's wife Therese is on the tree — a second spouse claim is
        # exactly the single-slot conflict that must never flow green.
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SPOUSE)
        await chat.say("Marta")
        if texts.SKIP in chat.buttons:
            await chat.tap(texts.SKIP)
        await self.send_basket(chat)
        self.assertEqual(self.queued()[-1]["status"], "pending")


class IdentityCheckTests(BotTestCase):
    """"Is one of these you?" answered by looking, not guessing."""

    async def test_the_guess_carries_the_number_and_a_look_button(self):
        original = config.PUBLIC_URL
        config.PUBLIC_URL = "https://example.test/tree.html"
        try:
            chat = Conversation(user_id=5700)
            await chat.start()
            await chat.say("Khalil")
            await chat.tap(config.FAMILY_NAME)
            await chat.tap(config.HOUSES[0]["display_name"])
            await chat.say("Youssef")
            await chat.tap(texts.SELF_MAN)
            self.assertIn(texts.IDENTITY_GUESS, chat.text)
            khalil = self.ids["khalil_y"]
            self.assertTrue(
                any(f"(#{khalil})" in label for label in chat.buttons),
                list(chat.buttons),
            )
            self.assertIn(texts.check_on_tree(khalil), chat.buttons)
        finally:
            config.PUBLIC_URL = original


class TreeLinkButtonTests(BotTestCase):
    """The whole tree, one tap from the end of every menu — once published."""

    async def test_the_menu_ends_with_the_tree_once_published(self):
        original = config.PUBLIC_URL
        config.PUBLIC_URL = "https://example.test/tree.html"
        try:
            chat = await self.identified_as_khalil()
            self.assertIn(texts.SEE_TREE, chat.buttons)
            self.assertEqual(list(chat.buttons)[-1], texts.SEE_TREE)
        finally:
            config.PUBLIC_URL = original

    async def test_unpublished_means_no_dead_link(self):
        original = config.PUBLIC_URL
        config.PUBLIC_URL = ""
        try:
            chat = await self.identified_as_khalil()
            self.assertNotIn(texts.SEE_TREE, chat.buttons)
        finally:
            config.PUBLIC_URL = original


class PhoneReviewTests(BotTestCase):
    """/review — approving from a phone, super admins only."""

    def setUp(self):
        super().setUp()
        config.SUPER_ADMIN_TELEGRAM_IDS.append(5001)

    def tearDown(self):
        config.SUPER_ADMIN_TELEGRAM_IDS.remove(5001)
        super().tearDown()

    async def open_desk(self, chat):
        from bot import handlers

        chat.state = await handlers.on_review_command(
            chat._update("/review"), chat.context
        )
        return chat.state

    async def queue_a_child(self, chat, name="Rita"):
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("0")   # sons
        await chat.tap("1")   # daughters
        await chat.say(name)
        await chat.tap(texts.CONFIRM_CORRECT)

    async def queue_for_desk(self, chat, name="Rita"):
        """A claim about somebody else — second-hand, so it always waits."""
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap(f"(#{self.ids['boutros']})")
        await chat.tap("child of")
        await chat.tap("0")
        await chat.tap("1")
        await chat.say(name)
        await chat.tap(texts.CONFIRM_CORRECT)

    async def test_a_claim_is_not_read_out_twice(self):
        chat = await self.identified_as_khalil()
        await self.queue_for_desk(chat, name="Rita")
        await self.open_desk(chat)
        body = chat.text.split("\n")
        claims = [line for line in body if "Rita" in line]
        self.assertEqual(len(claims), 1, chat.text)

    async def test_the_desk_is_for_admins_only(self):
        chat = await self.identified_as_khalil(user_id=5002)
        await self.open_desk(chat)
        self.assertIn(texts.REVIEW_NOT_ADMIN, chat.text)

    async def test_an_admin_approves_from_the_phone(self):
        chat = await self.identified_as_khalil()
        before = self.people_count()
        await self.queue_for_desk(chat)

        await self.open_desk(chat)
        self.assertIn("Reviewing #", chat.text)
        await chat.tap(texts.REVIEW_APPROVE)

        self.assertEqual(self.people_count(), before + 1)
        self.assertEqual(self.queued()[-1]["status"], "approved")

    async def test_a_duplicate_leads_with_the_merge_button(self):
        chat = await self.identified_as_khalil()
        # Mariam is already on the tree as Khalil's daughter; entering her
        # again must offer "same person" first, not a plain approve.
        await self.queue_a_child(chat, name="Mariam")

        await self.open_desk(chat)
        self.assertIn(texts.REVIEW_MERGE, chat.buttons)
        before = self.people_count()
        await chat.tap(texts.REVIEW_MERGE)

        self.assertEqual(self.people_count(), before)
        self.assertEqual(self.queued()[-1]["status"], "merged")

    async def test_skip_leaves_it_pending(self):
        chat = await self.identified_as_khalil()
        await self.queue_for_desk(chat)
        await self.open_desk(chat)
        await chat.tap(texts.REVIEW_SKIP)
        self.assertEqual(self.queued()[-1]["status"], "pending")

    async def test_reject_keeps_the_record(self):
        chat = await self.identified_as_khalil()
        await self.queue_for_desk(chat)
        await self.open_desk(chat)
        await chat.tap(texts.REVIEW_REJECT)
        record = self.queued()[-1]
        self.assertEqual(record["status"], "rejected")


class FixSomethingTests(BotTestCase):
    async def test_with_nothing_sent_the_tree_is_still_fixable(self):
        # No dead end: the tree itself can always be corrected, even by
        # somebody who has sent nothing of their own yet.
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_FIX)
        self.assertIn(texts.FIX_TREE, chat.buttons)

    async def test_correction_goes_to_the_queue_as_a_suggestion(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("0")
        await chat.tap("1")
        await chat.say("Ritta")
        await self.send_basket(chat)

        await chat.tap(texts.MENU_FIX)
        self.assertIn("Ritta", str(chat.buttons))
        await chat.tap("Ritta")
        # A tap opens the full story first; fixing is a second, deliberate tap.
        self.assertIn("Status:", chat.text)
        await chat.tap(texts.FIX_THIS)
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
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("Sami")
        await self.send_basket(chat)
        original = self.queued()[0]["payload"]

        await chat.tap(texts.MENU_FIX)
        await chat.tap("Sami")
        await chat.tap(texts.FIX_THIS)
        await chat.say("Actually he is Samir")
        await chat.tap(texts.SEND_IT)

        # Sami landed instantly (first-hand, uncontested) — but the
        # correction changes nothing by itself, and never will.
        self.assertEqual(self.people_count(), 13)
        self.assertEqual(self.queued()[0]["payload"], original)
        correction = self.queued()[-1]
        self.assertEqual(correction["payload"]["kind"], submissions.CORRECTION)
        self.assertEqual(correction["status"], "pending")
        conn = db.connect()
        sami = [r for r in db.get_people(conn) if r["given_name"] == "Sami"]
        conn.close()
        self.assertEqual(len(sami), 1)
        self.assertEqual(sami[0]["given_name"], "Sami", "correction auto-applied")


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
        chat = Conversation(user_id=5310)
        await chat.start()
        await chat.say("Rima")
        await chat.say("a boy I think")
        self.assertIn(texts.NOT_UNDERSTOOD, chat.transcript())
        self.assertIn(texts.ASK_SELF_FAMILY, chat.text)

    async def test_empty_message_is_rejected_kindly(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("   ")
        self.assertIn(texts.NAME_EMPTY, chat.transcript())

    async def test_absurdly_long_name_is_rejected(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("K" * 200)
        self.assertIn(texts.NAME_TOO_LONG, chat.transcript())

    async def test_whitespace_around_a_name_is_trimmed(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("1")
        await chat.tap("0")
        await chat.say("  Sami  ")
        await self.send_basket(chat)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Sami"
        )

    async def test_arabic_name_survives_the_round_trip(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap("1")
        await chat.tap("0")
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
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim,")
        await chat.tap(texts.SELF_MAN)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["given_name"], "Steven")
        self.assertEqual(entry["father_given_name"], "Kalim")

    async def test_punctuation_inside_a_name_survives(self):
        chat = Conversation(user_id=5301)
        await chat.start()
        await chat.say("Abou-Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Abou-Khalil"
        )

    async def test_a_spelling_typed_out_loud_finds_its_button(self):
        """"Su K ar" is somebody spelling their own surname."""
        chat = Conversation(user_id=5302)
        await chat.start()
        await chat.say("Steven")
        await chat.say("Su K ar")
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim")
        await chat.tap(texts.SELF_MAN)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["family_name"], "Sukar"
        )

    async def test_a_typed_choice_works_for_any_button_question(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SPOUSE)
        await chat.say("Rita")
        await chat.say("Obeid")
        await self.send_basket(chat)
        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["sex"], "F")
        self.assertEqual(entry["family_name"], "Obeid")

    async def test_gibberish_at_a_choice_still_re_asks(self):
        chat = Conversation(user_id=5311)
        await chat.start()
        await chat.say("Rima")
        await chat.say("qqqq zzz")
        self.assertIn(texts.NOT_UNDERSTOOD, chat.transcript())
        self.assertIn(texts.ASK_SELF_FAMILY, chat.text)

    async def test_dunno_counts_as_i_dont_know(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("dunno")
        self.assertIn(texts.CONFIRM_CORRECT, chat.buttons)

    async def test_ok_is_a_yes_at_the_climb(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.say("Ok")
        self.assertIn(texts.NEXT_PROMPT, chat.text)

    async def test_nah_is_a_no_at_the_climb(self):
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.say("nah")
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)

    async def test_unclear_text_re_shows_the_question_with_its_buttons(self):
        """The old behaviour scolded and left them with no buttons at all."""
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.say("hmm what")
        self.assertIn(texts.CONFIRM_CORRECT, chat.buttons)
        self.assertIn("Correct?", chat.text)

    async def test_typing_send_at_the_review_screen_sends(self):
        chat = await self.identified_as_khalil()
        await chat.say("My son Sami")
        await chat.say("yes")
        self.assertEqual(len(self.queued()), 1)

    async def test_unclear_text_at_the_review_screen_re_shows_the_list(self):
        chat = await self.identified_as_khalil()
        await chat.say("My son Sami")
        await chat.say("what is this")
        self.assertIn("Sami", chat.text)
        self.assertIn("Send all 1", " ".join(chat.buttons))
        self.assertEqual(self.queued(), [])


class CursorAwarenessTests(BotTestCase):
    """The menu must sound like it knows who it is pointed at."""

    async def test_the_menu_names_the_relationship(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap("Georges Youssef")
        self.assertIn("your brother", chat.text)

    async def test_a_brothers_shared_parents_are_not_asked_for(self):
        """"Add Toufic's parents" about your own brother reads as if the bot
        does not know his parents are your parents."""
        chat = await self.fresh_contributor()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Wadiha")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)

        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap("1")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Toufic")
        self.assertIn("their father is Fares", chat.text)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.MENU_SWITCH)
        await chat.tap("Toufic")

        self.assertIn("Toufic — your brother", chat.text)
        self.assertNotIn("Add Toufic's parents", chat.buttons)
        self.assertIn("already down", chat.text)
        self.assertIn("Fares", chat.text)
        self.assertIn("Wadiha", chat.text)

    async def test_the_switch_list_says_who_everyone_is(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_SWITCH)
        offered = " ".join(chat.buttons)
        self.assertIn("your father", offered)
        self.assertIn("your wife", offered)


class BrotherOrSisterTests(BotTestCase):
    """"My siblings are Toufic and Nawal" never said who is which. Ask —
    once per person — rather than draw the tree wrong quietly."""

    async def test_a_dictated_sibling_with_no_sex_is_asked_about(self):
        chat = await self.fresh_contributor()
        await chat.say("My siblings are Toufic and Nawal")
        self.assertIn("Is Toufic your brother or sister?", chat.text)
        await chat.tap(texts.SIBLING_BROTHER)
        self.assertIn("Is Nawal your brother or sister?", chat.text)
        await chat.tap(texts.SIBLING_SISTER)
        self.assertIn("Check the spelling", chat.text)

        await chat.tap("Send all")
        entries = [
            q["payload"]["people"][0]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_SIBLING
        ]
        self.assertEqual([e["sex"] for e in entries], ["M", "F"])

    async def test_a_named_brother_is_not_asked(self):
        chat = await self.fresh_contributor()
        await chat.say("My brothers Toufic and Youssef and sister Nawal")
        self.assertNotIn("Is Toufic", chat.transcript())
        self.assertIn("Check the spelling", chat.text)

    async def test_a_typed_answer_is_understood(self):
        chat = await self.fresh_contributor()
        await chat.say("My siblings are Toufic and Nawal")
        await chat.say("he's a boy")
        self.assertIn("Is Nawal", chat.text)
        await chat.say("sister")
        self.assertIn("Check the spelling", chat.text)

    async def test_skip_leaves_the_sex_unknown(self):
        chat = await self.fresh_contributor()
        await chat.say("My siblings are Toufic and Nawal")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.SIBLING_SISTER)
        await chat.tap("Send all")
        entries = [
            q["payload"]["people"][0]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_SIBLING
        ]
        self.assertEqual([e.get("sex") for e in entries], [None, "F"])

    async def test_a_run_on_sister_is_not_a_brother(self):
        """"My brothers are Toufic and Joseph and my sister Nawal" — the
        first live tester's exact words, and Nawal came out a brother."""
        chat = await self.fresh_contributor()
        await chat.say("My brothers are Toufic and Joseph and my sister Nawal")
        self.assertIn("Check the spelling", chat.text)
        self.assertIn("Nawal as sister of", chat.text)
        self.assertNotIn("one person or two", chat.transcript())

    async def test_a_mixed_group_asks_about_everyone(self):
        chat = await self.fresh_contributor()
        await chat.say("My brothers and sisters are Tony, Mary and Sam")
        self.assertIn("Is Tony your brother or sister?", chat.text)

    async def test_naming_yourself_is_not_a_stranger(self):
        """"My name is Steven my wife is Louisa and my children are Henri
        and Sabine" — the children became his wives, hung off a stranger
        called Steven Sukar who was in fact the man typing."""
        chat = Conversation(user_id=7100)
        await chat.start()
        await chat.say("Steven")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Kalim")
        await chat.tap(texts.SELF_MAN)
        await chat.tap(texts.TOUR_MENU)
        await chat.say(
            "My name is Steven my wife is Louisa and my children are "
            "Henri and Sabine"
        )
        self.assertNotIn("belong to", chat.transcript())
        self.assertIn("did you mean yourself?", chat.text)
        await chat.tap(texts.MEANT_MYSELF)
        self.assertIn("Is Henri your son or daughter?", chat.text)
        await chat.tap(texts.CHILD_SON)
        await chat.tap(texts.CHILD_DAUGHTER)
        self.assertIn("Louisa as wife of", chat.text)
        self.assertIn("Henri as son of", chat.text)
        self.assertIn("Sabine as daughter of", chat.text)

    async def test_children_are_asked_as_son_or_daughter(self):
        chat = await self.identified_as_khalil()
        await chat.say("My kids are Rohnda and Jason")
        self.assertIn("Is Rohnda your son or daughter?", chat.text)

    async def test_the_familys_own_names_shape_the_question(self):
        """Every Antoun on this tree is a man, so lead with the guess —
        one confirming tap for the common case, and the Hanna case is
        still one tap from being put right."""
        chat = await self.identified_as_khalil()
        await chat.say("My kids are Antoun and Layla")
        self.assertIn("Antoun — your son, I'm guessing?", chat.text)
        await chat.tap("Yes — son")
        self.assertIn("Layla — your daughter, I'm guessing?", chat.text)
        await chat.tap("No — son")
        await chat.tap("Send all")

        entries = [
            q["payload"]["people"][0]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_CHILD
        ]
        self.assertEqual([e["sex"] for e in entries], ["M", "M"])

    def test_a_single_disagreement_kills_the_name_guess(self):
        conn = db.connect()
        try:
            self.assertEqual(db.name_sex_hint(conn, "Antoun"), "M")
            db.create_person(conn, "Antoun", sex="F")
            self.assertIsNone(db.name_sex_hint(conn, "Antoun"))
            self.assertIsNone(db.name_sex_hint(conn, "Somebody-Unheard-Of"))
        finally:
            conn.close()


class GuidedTourTests(BotTestCase):
    """A new contributor is led through their family, not dropped at a menu."""

    async def raw_signup(self, user_id: int = 7001) -> Conversation:
        chat = Conversation(user_id=user_id)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        return chat

    async def test_a_new_signup_is_led_not_dropped_at_a_menu(self):
        chat = await self.raw_signup()
        self.assertIn("your parents first", chat.text)
        self.assertIn(texts.TOUR_LETS_GO, chat.buttons)
        self.assertIn(texts.TOUR_MENU, chat.buttons)

    async def test_nobody_is_told_about_branches(self):
        chat = await self.raw_signup()
        self.assertNotIn("branch", chat.transcript())

    async def test_the_tour_walks_the_whole_family(self):
        chat = await self.raw_signup(user_id=7002)

        await chat.tap(texts.TOUR_LETS_GO)      # my parents
        await chat.say("Wadiha")                # mother; father known from signup
        await chat.tap(texts.SKIP)              # her maiden name
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)          # back to the tour

        self.assertIn("brothers and sisters", chat.text)
        await chat.tap(texts.TOUR_LETS_GO)
        self.assertIn("How many brothers", chat.text)
        await chat.tap("1")
        self.assertIn("How many sisters", chat.text)
        await chat.tap("1")
        await chat.tap(texts.YES_WORD)          # all the same father
        self.assertIn("first brother", chat.text)
        await chat.say("Tony")
        self.assertIn("first sister", chat.text)
        await chat.say("Mary")
        self.assertIn("their father is Fares", chat.text)
        await chat.tap(texts.CONFIRM_CORRECT)

        self.assertIn("married", chat.text)     # own household next
        await chat.tap(texts.TOUR_NOT_MARRIED)
        self.assertIn("children", chat.text.lower())
        await chat.tap(texts.TOUR_NO_CHILDREN)

        self.assertIn("Fares's parents", chat.text)   # grandparents
        await chat.tap(texts.TOUR_LETS_GO)
        await chat.say("Elias")
        await chat.tap(config.FAMILY_NAME)            # how his family spells it
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.ADD_MORE)

        self.assertIn("Did Fares have brothers and sisters", chat.text)
        await chat.tap(texts.TOUR_SKIP)
        self.assertIn("Wadiha's parents", chat.text)  # mother's side next
        await chat.tap(texts.TOUR_SKIP)
        self.assertIn("Did Wadiha have brothers and sisters", chat.text)
        await chat.tap(texts.TOUR_SKIP)

        self.assertIn("people so far", chat.text)     # the tour signs off
        self.assertIn(texts.MENU_ADD_CHILD, chat.buttons)

    async def test_a_skipped_step_is_not_asked_again(self):
        chat = await self.raw_signup(user_id=7003)
        await chat.tap(texts.TOUR_SKIP)         # parents skipped
        self.assertIn("brothers and sisters", chat.text)
        await chat.tap(texts.TOUR_NONE_SIBLINGS)
        self.assertIn("married", chat.text)
        await chat.say("My wife is Laila")
        await chat.tap("Send all")
        self.assertIn("children", chat.text.lower())
        await chat.tap(texts.TOUR_NO_CHILDREN)
        # Parents and siblings never come back; with no parents named there
        # is no grandparent side to offer, so the tour signs off.
        self.assertNotIn("your parents first", chat.text)
        self.assertIn("people so far", chat.text)

    async def test_a_linked_contributor_skips_the_tour(self):
        chat = await self.identified_as_khalil()
        self.assertIn(texts.MENU_ADD_CHILD, chat.buttons)


class TourAwarenessTests(BotTestCase):
    """The tour never asks a question the data has already answered."""

    async def test_no_any_children_after_the_children_are_in(self):
        chat = Conversation(user_id=7400)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        await chat.tap(texts.TOUR_SKIP)          # parents later
        await chat.tap(texts.TOUR_NONE_SIBLINGS)
        await chat.tap(texts.TOUR_LETS_GO)       # married — add her
        await chat.say("Louisa")
        await chat.tap(texts.SKIP)
        await chat.tap(texts.CONFIRM_CORRECT)
        await chat.tap(texts.NEXT_CHILDREN_MINE)  # the panel, not the tour
        await chat.tap("1")
        await chat.tap("1")
        await chat.say("Henri")
        await chat.say("Sabine")
        await chat.tap(texts.CONFIRM_CORRECT)
        # The children are in; the tour must not ask "Any children?"
        self.assertNotIn("Any children?", chat.text)
        self.assertIn("people so far", chat.text)


class CountedCaptureTests(BotTestCase):
    """How many brothers, how many sisters — then exactly that many names."""

    async def start_siblings(self, user_id: int) -> Conversation:
        chat = Conversation(user_id=user_id)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(config.HOUSES[0]["display_name"])
        await chat.say("Fares")
        await chat.tap(texts.SELF_MAN)
        await chat.tap(texts.TOUR_SKIP)          # parents later
        await chat.tap(texts.TOUR_LETS_GO)       # siblings
        return chat

    async def test_counts_drive_the_questions(self):
        chat = await self.start_siblings(7300)
        await chat.tap("2")
        await chat.tap("1")
        await chat.tap(texts.YES_WORD)
        await chat.say("Toufic")
        self.assertIn("second brother", chat.text)
        await chat.say("Joseph")
        self.assertIn("first sister", chat.text)
        await chat.say("Nawal")
        self.assertIn("Toufic and Joseph are your brothers", chat.text)
        self.assertIn("Nawal is your sister", chat.text)
        self.assertIn("their father is Fares", chat.text)
        await chat.tap(texts.CONFIRM_CORRECT)
        entries = [
            q["payload"]["people"][0]
            for q in self.queued()
            if q["payload"]["kind"] == submissions.ADD_SIBLING
        ]
        self.assertEqual(
            [(e["given_name"], e["sex"]) for e in entries],
            [("Toufic", "M"), ("Joseph", "M"), ("Nawal", "F")],
        )
        self.assertTrue(all(e["father_given_name"] == "Fares" for e in entries))

    async def test_zero_and_zero_moves_on(self):
        chat = await self.start_siblings(7301)
        await chat.tap("0")
        await chat.tap("0")
        self.assertIn("married", chat.text)      # straight to the next step

    async def test_one_wrong_name_costs_only_that_name(self):
        chat = await self.start_siblings(7302)
        await chat.tap("2")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Tony")
        await chat.say("Walked")        # autocorrect, mid-list
        self.assertIn("Walked", chat.text)

        await chat.tap(texts.COUNT_FIX.format(name="Walked"))
        await chat.say("Waleed")

        # Back at the confirmation, with the other name untouched.
        self.assertIn("Waleed", chat.text)
        self.assertIn("Tony", chat.text)
        self.assertNotIn("Walked", chat.text.split("Changed")[-1])
        self.assertIn(texts.CONFIRM_CORRECT, chat.buttons)

    async def test_the_fixed_name_is_what_gets_sent(self):
        chat = await self.start_siblings(7304)
        await chat.tap("1")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Walked")
        await chat.tap(texts.COUNT_FIX.format(name="Walked"))
        await chat.say("Waleed")
        await chat.tap(texts.CONFIRM_CORRECT)

        names = [
            e["given_name"]
            for q in self.queued()
            for e in q["payload"].get("people") or []
        ]
        self.assertIn("Waleed", names)
        self.assertNotIn("Walked", names)

    async def test_starting_the_whole_list_again_is_still_there(self):
        chat = await self.start_siblings(7305)
        await chat.tap("1")
        await chat.tap("0")
        await chat.tap(texts.YES_WORD)
        await chat.say("Tonyy")
        await chat.tap(texts.COUNT_START_OVER)
        self.assertIn("How many brothers", chat.text)

    async def test_typed_numbers_work(self):
        chat = await self.start_siblings(7303)
        await chat.say("two")
        self.assertIn("a number", chat.text.lower())
        await chat.say("2")
        self.assertIn("How many sisters", chat.text)


class LinkQuestionTests(BotTestCase):
    """When a name looks like somebody already recorded, ask the person
    typing — they know. The answer is evidence; merging stays an admin's."""

    async def test_a_match_on_the_tree_is_asked_about(self):
        chat = await self.identified_as_khalil()
        await chat.say("My brother Georges")
        self.assertIn("same person as Georges Youssef Sukkar", chat.text)
        self.assertIn(texts.SAME_PERSON, chat.buttons)

    async def test_yes_travels_with_the_submission(self):
        chat = await self.identified_as_khalil()
        await chat.say("My brother Georges")
        await chat.tap(texts.SAME_PERSON)
        await chat.tap("Send all")

        queued = self.queued()[-1]
        entry = queued["payload"]["people"][0]
        self.assertEqual(entry["same_person_id"], self.ids["georges"])
        self.assertEqual(queued["matched_person_id"], self.ids["georges"])

    async def test_no_blocks_the_matchers_guess(self):
        """A denial must stop the admin merging on a hunch."""
        chat = await self.identified_as_khalil()
        await chat.say("My brother Georges")
        await chat.tap(texts.DIFFERENT_PERSON)
        await chat.tap("Send all")

        queued = self.queued()[-1]
        entry = queued["payload"]["people"][0]
        self.assertEqual(entry["not_person_id"], self.ids["georges"])
        self.assertIsNone(queued["matched_person_id"])

    async def test_not_sure_records_nothing(self):
        chat = await self.identified_as_khalil()
        await chat.say("My brother Georges")
        await chat.tap(texts.NOT_SURE)
        await chat.tap("Send all")

        entry = self.queued()[-1]["payload"]["people"][0]
        self.assertNotIn("same_person_id", entry)
        self.assertNotIn("not_person_id", entry)

    async def test_two_contributors_naming_the_same_person_collide_early(self):
        """The common case: two brothers each entering the same third brother."""
        first = await self.identified_as_khalil()
        await first.say("My brother Tanios")
        await first.tap("Send all")

        second = Conversation(user_id=5002)
        await second.start()
        await second.say("Georges")
        await second.tap(config.FAMILY_NAME)
        await second.tap(config.HOUSES[0]["display_name"])
        await second.say("Youssef")
        await second.tap(texts.SELF_MAN)
        await second.tap("Georges Youssef")
        await second.tap(texts.MENU_SWITCH)
        await second.tap("Khalil Youssef")
        await second.say("His brother is Tanios")

        # Khalil's first-hand Tanios landed instantly, so the collision is
        # now with a numbered person, not a pending row.
        self.assertIn("Is this the same person as Tanios", second.text)
        self.assertIn("(#", second.text)
        await second.tap(texts.SAME_PERSON)
        await second.tap("Send all")

        entry = self.queued()[-1]["payload"]["people"][0]
        self.assertIn("same_person_id", entry)

    async def test_the_link_fires_before_any_admin_has_approved_anything(self):
        """One person enters their siblings; later a sibling signs up and
        enters the family from her side. Different subjects, nothing
        approved — the shared father is what ties the claims together."""
        first = Conversation(user_id=6001)
        await first.start()
        await first.say("Steven")
        await first.tap(config.FAMILY_NAME)
        await first.tap(config.HOUSES[0]["display_name"])
        await first.say("Kalim")
        await first.tap(texts.SELF_MAN)
        await first.say("My brother Joseph and sister Nawal")
        await first.tap("Send all")

        second = Conversation(user_id=6002)
        await second.start()
        await second.say("Nawal")
        await second.tap(config.FAMILY_NAME)
        await second.tap(config.HOUSES[0]["display_name"])
        await second.say("Kalim")
        await second.tap(texts.SELF_WOMAN)
        await second.say("My brothers Steven and Joseph")

        self.assertIn("Is this the same person as Steven", second.text)
        self.assertIn(texts.MATCH_PENDING_SUFFIX.strip(), second.text)
        await second.tap(texts.SAME_PERSON)
        self.assertIn("Is this the same person as Joseph", second.text)
        await second.tap(texts.SAME_PERSON)
        await second.tap("Send all")

        linked = [
            q["payload"]["people"][0].get("same_submission_id")
            for q in self.queued()
            if q["telegram_user_id"] == 6002
            and q["payload"]["kind"] == submissions.ADD_SIBLING
        ]
        self.assertTrue(all(linked), linked)

    async def test_a_bare_shared_name_is_not_interrogated(self):
        """Half the family answers to the same given names. No relational
        evidence, no question."""
        chat = await self.fresh_contributor()
        await chat.say("My brother Georges")
        self.assertNotIn("same person", chat.text)
        self.assertIn("Check the spelling", chat.text)


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


class SignInIsNotOptionalTests(BotTestCase):
    """Nobody reaches the menu without saying who they are.

    Every flow adds relatives *for* somebody. Cancelling out of sign-in
    left that somebody undefined, and a contributor spent a day and a half
    entering thirty-five submissions that anchored to nobody and could
    never be approved.
    """

    async def test_signing_in_offers_no_way_out(self):
        chat = Conversation(user_id=99101)
        await chat.start()
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)
        self.assertNotIn(texts.CANCEL, chat.buttons, "a way to skip signing in")

    async def test_a_stranger_at_the_menu_is_sent_back_to_sign_in(self):
        from bot import handlers

        chat = Conversation(user_id=99102)
        await chat.start()
        # However they got there — stray text, a stale button, a restart.
        chat.state = await handlers._show_menu(chat._update("x"), chat.context)
        self.assertIn(texts.SIGN_IN_FIRST, chat.transcript())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)
        self.assertNotIn(texts.MENU_ADD_PARENTS, chat.buttons)

    async def test_a_stale_menu_button_does_not_get_through_either(self):
        # Their phone still shows a menu from before. Tapping it must not
        # start a flow that anchors to nobody.
        from bot import handlers

        chat = Conversation(user_id=99103)
        await chat.start()
        # What persistence restores after a restart: parked at the menu.
        chat.state = handlers.MENU
        await chat.tap_data("menu:add_sibling")
        self.assertIn(texts.SIGN_IN_FIRST, chat.transcript())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)

    async def test_somebody_signed_in_still_gets_their_menu(self):
        chat = await self.identified_as_khalil()
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)
        self.assertNotIn(texts.SIGN_IN_FIRST, chat.text)


class CornerReachTests(BotTestCase):
    """How far "your corner of the tree" reaches.

    Two generations either side: grandparents down to the children of
    everyone in the contributor's own generation. Reaching two up and only
    one down hid the nephews and nieces a contributor had just entered
    themselves, from the very drawing they use to check their work.
    """

    def add_kin(self):
        """A nephew for Khalil, and a cousin — neither is in the seed."""
        conn = db.connect()
        try:
            nephew = db.create_person(
                conn, "Elie", sex="M", father_id=self.ids["georges"]
            )
            cousin = db.create_person(
                conn, "Rita", sex="F", father_id=self.ids["boutros"]
            )
            conn.commit()
            return nephew, cousin
        finally:
            conn.close()

    async def test_a_brothers_children_are_in_the_corner(self):
        self.add_kin()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_VIEW)
        self.assertIn("Elie", chat.text, chat.text)

    async def test_an_uncles_children_are_too(self):
        self.add_kin()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_VIEW)
        self.assertIn("Rita", chat.text, chat.text)

    async def test_they_hang_below_their_own_parent(self):
        self.add_kin()
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_VIEW)
        lines = chat.text.split("\n")
        nephew = next(i for i, line in enumerate(lines) if "Elie" in line)
        georges = next(i for i, line in enumerate(lines) if "Georges" in line)
        self.assertGreater(nephew, georges, chat.text)
        self.assertGreater(
            len(lines[nephew]) - len(lines[nephew].lstrip("│ ")),
            len(lines[georges]) - len(lines[georges].lstrip("│ ")),
            chat.text,
        )


class HouseSignInTests(BotTestCase):
    """The house is asked once, at sign-in, and inherited by everyone after.

    It is also what keeps two same-named men in two houses apart at the
    riskiest moment there is: the "is one of these you?" tap.
    """

    def house(self, position: int = 0) -> dict:
        return config.HOUSES[position]

    async def sign_in(self, chat, given, father, house_label, sex=None):
        await chat.start()
        await chat.say(given)
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(house_label)
        await chat.say(father)
        await chat.tap(sex or texts.SELF_MAN)
        return chat

    async def test_the_house_is_asked_and_kept(self):
        chat = Conversation(user_id=5801)
        await chat.start()
        await chat.say("Sarkis")
        await chat.tap(config.FAMILY_NAME)
        self.assertIn(texts.ASK_SELF_HOUSE, chat.text)
        # The reason travels with the question.
        self.assertIn(texts.ASK_SELF_HOUSE_WHY, chat.text)
        await chat.tap(self.house()["display_name"])
        await chat.say("Tanios")
        await chat.tap(texts.SELF_MAN)

        entry = self.queued()[-1]["payload"]["people"][0]
        self.assertEqual(entry["house"], self.house()["key"])

    async def test_not_knowing_is_a_real_answer(self):
        chat = Conversation(user_id=5802)
        await chat.start()
        await chat.say("Sarkis")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.HOUSE_UNKNOWN)
        await chat.say("Tanios")
        await chat.tap(texts.SELF_MAN)

        entry = self.queued()[-1]["payload"]["people"][0]
        self.assertIsNone(entry["house"], "a guess would be inherited downward")

    async def test_a_house_we_have_not_listed_is_kept_verbatim(self):
        chat = Conversation(user_id=5803)
        await chat.start()
        await chat.say("Sarkis")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.HOUSE_OTHER)
        await chat.say("Haddad")
        await chat.say("Tanios")
        await chat.tap(texts.SELF_MAN)

        entry = self.queued()[-1]["payload"]["people"][0]
        self.assertEqual(entry["house"], "Haddad")

    async def test_approving_a_sign_in_declares_the_house(self):
        import review

        chat = Conversation(user_id=5804)
        await self.sign_in(chat, "Sarkis", "Tanios", self.house()["display_name"])

        conn = db.connect()
        try:
            sid = self.queued()[-1]["id"]
            created = review.approve(conn, sid, reviewed_by=1, force=True)
            person = db.get_person(conn, created[0])
            branch = db.get_branch_by_key(conn, self.house()["key"])
            self.assertEqual(person["branch_id"], branch["id"])
            self.assertEqual(person["branch_declared"], 1)
        finally:
            conn.close()

    async def test_an_unlisted_house_is_flagged_rather_than_guessed(self):
        import review

        chat = Conversation(user_id=5805)
        await chat.start()
        await chat.say("Sarkis")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(texts.HOUSE_OTHER)
        await chat.say("Haddad")
        await chat.say("Tanios")
        await chat.tap(texts.SELF_MAN)

        conn = db.connect()
        try:
            sid = self.queued()[-1]["id"]
            created = review.approve(conn, sid, reviewed_by=1, force=True)
            self.assertIsNone(db.get_person(conn, created[0])["branch_id"])
            note = db.get_submission(conn, sid)["review_note"]
            self.assertIn("Haddad", note)
        finally:
            conn.close()

    async def test_the_same_name_in_another_house_is_not_offered_as_you(self):
        # Khalil Youssef is on the seeded tree. Declare him into one house,
        # then let a different Khalil Youssef sign in from another.
        conn = db.connect()
        try:
            db.declare_house(conn, self.ids["khalil_y"], self.house(0)["key"])
            conn.commit()
        finally:
            conn.close()

        chat = Conversation(user_id=5806)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(self.house(1)["display_name"])
        await chat.say("Youssef")
        await chat.tap(texts.SELF_MAN)

        self.assertNotIn(texts.IDENTITY_GUESS, chat.text)
        self.assertEqual(self.queued()[-1]["payload"]["kind"], submissions.IDENTIFY)

    async def test_the_same_house_still_collides_as_before(self):
        conn = db.connect()
        try:
            db.declare_house(conn, self.ids["khalil_y"], self.house(0)["key"])
            conn.commit()
        finally:
            conn.close()

        chat = Conversation(user_id=5807)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(config.FAMILY_NAME)
        await chat.tap(self.house(0)["display_name"])
        await chat.say("Youssef")
        await chat.tap(texts.SELF_MAN)

        self.assertIn(texts.IDENTITY_GUESS, chat.text)
