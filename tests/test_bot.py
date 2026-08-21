"""
Tests for the Telegram capture step.

The conversation is driven through `tests/harness.py`, which fakes the
transport but uses the real handlers and the real routing table, so the
callback patterns in `bot/main.py` are covered too.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402
import submissions  # noqa: E402
from telegram.ext import CommandHandler, ConversationHandler  # noqa: E402

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

    async def identified_as_khalil(self) -> Conversation:
        """A contributor who has been through /start and linked themselves."""
        chat = Conversation(user_id=5001)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(texts.YES)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.tap("Khalil Youssef")
        return chat


class IdentificationTests(BotTestCase):
    async def test_start_greets_and_asks_for_a_first_name(self):
        chat = Conversation()
        await chat.start()
        self.assertIn(config.FAMILY_NAME, chat.transcript())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)

    async def test_one_question_per_message(self):
        """The spec's rule: a conversation, not a form."""
        chat = Conversation()
        await chat.start()
        # Welcome, then exactly one question.
        self.assertEqual(len(chat.sent), 2)
        self.assertEqual(chat.text.count("?"), 1)

    async def test_typed_name_is_confirmed_before_use(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Steven")
        self.assertIn("Steven", chat.text)
        self.assertIn(texts.YES, chat.buttons)
        self.assertIn(texts.NO_RETYPE, chat.buttons)

    async def test_saying_no_re_asks_the_same_question(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Stevn")
        await chat.tap(texts.NO_RETYPE)
        self.assertIn(texts.RETYPE, chat.transcript())
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)
        await chat.say("Steven")
        self.assertIn("Steven", chat.text)

    async def test_full_name_is_rejected_with_an_explanation(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Khalil Youssef Sukkar")
        self.assertIn("first name", chat.transcript().lower())
        # And it re-asks rather than giving up.
        self.assertIn(texts.ASK_SELF_GIVEN, chat.text)

    async def test_known_person_is_offered_as_a_match(self):
        chat = Conversation()
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(texts.YES)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        self.assertIn("Khalil Youssef Sukkar", str(chat.buttons))

    async def test_confirming_a_match_links_the_contributor(self):
        chat = await self.identified_as_khalil()
        self.assertIn("Khalil Youssef Sukkar", chat.transcript())

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
        await chat.tap(texts.YES)
        await chat.say("Fadi")
        await chat.tap(texts.YES)

        self.assertEqual(self.people_count(), before, "constraint 4 violated")
        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["payload"]["kind"], submissions.IDENTIFY)
        self.assertEqual(queued[0]["status"], "pending")

    async def test_none_of_these_queues_them_as_new(self):
        chat = Conversation(user_id=5003)
        await chat.start()
        await chat.say("Khalil")
        await chat.tap(texts.YES)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.tap(texts.IDENTITY_NONE_OF_THESE)

        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["payload"]["kind"], submissions.IDENTIFY)
        self.assertEqual(self.people_count(), 12)

    async def test_returning_user_is_greeted_by_name_and_skips_identification(self):
        await self.identified_as_khalil()
        again = Conversation(user_id=5001)
        await again.start()
        self.assertIn("Khalil Youssef Sukkar", again.text)
        self.assertIn(texts.MENU_ADD_CHILD, again.buttons)

    async def test_returning_user_with_identity_pending_is_not_asked_twice(self):
        chat = Conversation(user_id=5004)
        await chat.start()
        await chat.say("Zaher")
        await chat.tap(texts.YES)
        await chat.say("Fadi")
        await chat.tap(texts.YES)

        again = Conversation(user_id=5004)
        await again.start()
        self.assertNotIn(texts.ASK_SELF_GIVEN, again.transcript())
        self.assertIn(texts.MENU_ADD_CHILD, again.buttons)
        self.assertEqual(len(self.queued()), 1)


class MenuTests(BotTestCase):
    async def test_menu_offers_every_option_from_the_spec(self):
        chat = await self.identified_as_khalil()
        labels = set(chat.buttons)
        self.assertEqual(
            labels,
            {
                texts.MENU_ADD_PARENTS,
                texts.MENU_ADD_SIBLING,
                texts.MENU_ADD_SPOUSE,
                texts.MENU_ADD_CHILD,
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


class AddChildTests(BotTestCase):
    async def test_full_flow_queues_a_submission(self):
        chat = await self.identified_as_khalil()
        before = self.people_count()

        await chat.tap(texts.MENU_ADD_CHILD)
        self.assertIn(texts.CHILD_DAUGHTER, chat.buttons)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        await chat.tap(texts.YES)

        # Final confirmation before anything is stored.
        self.assertIn(texts.SEND_IT, chat.buttons)
        self.assertIn("Rita", chat.text)
        self.assertEqual(len(self.queued()), 0, "stored before the final yes")

        await chat.tap(texts.SEND_IT)
        self.assertIn(texts.SAVED, chat.transcript())

        self.assertEqual(self.people_count(), before, "constraint 4 violated")
        queued = self.queued()
        self.assertEqual(len(queued), 1)
        payload = queued[0]["payload"]
        self.assertEqual(payload["kind"], submissions.ADD_CHILD)
        self.assertEqual(payload["people"][0]["given_name"], "Rita")
        self.assertEqual(payload["people"][0]["sex"], "F")
        self.assertEqual(payload["about"]["person_id"], self.ids["khalil_y"])

    async def test_cancelling_saves_nothing(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.YES)
        await chat.tap(texts.CANCEL)

        self.assertIn(texts.CANCELLED, chat.transcript())
        self.assertEqual(self.queued(), [])

    async def test_start_over_re_asks_from_the_beginning(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.YES)
        await chat.tap(texts.START_OVER)

        self.assertIn(texts.ASK_CHILD_SEX, chat.text)
        self.assertEqual(self.queued(), [])

    async def test_returns_to_the_menu_afterwards(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        self.assertIn(texts.MENU_ADD_SIBLING, chat.buttons)


class AddParentsTests(BotTestCase):
    async def test_both_parents(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.say("Nada")
        await chat.tap(texts.YES)
        await chat.say("Karam")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        payload = self.queued()[0]["payload"]
        roles = {entry["role"]: entry for entry in payload["people"]}
        self.assertEqual(roles["father"]["given_name"], "Youssef")
        self.assertEqual(roles["father"]["sex"], "M")
        self.assertEqual(roles["mother"]["given_name"], "Nada")
        self.assertEqual(roles["mother"]["family_name"], "Karam")

    async def test_mothers_family_name_is_skippable(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.say("Nada")
        await chat.tap(texts.YES)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.SEND_IT)

        roles = {e["role"]: e for e in self.queued()[0]["payload"]["people"]}
        self.assertIsNone(roles["mother"]["family_name"])

    async def test_skipping_the_mother_skips_her_family_name_too(self):
        """The only_if rule: don't ask about someone who wasn't named."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.tap(texts.SKIP)  # mother's given name
        self.assertNotIn(texts.ASK_MOTHER_FAMILY, chat.text)
        self.assertIn(texts.SEND_IT, chat.buttons)

    async def test_skipping_both_parents_explains_and_saves_nothing(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.tap(texts.SKIP)
        await chat.tap(texts.SKIP)
        self.assertIn("nothing to send", chat.transcript())
        self.assertEqual(self.queued(), [])


class AddSiblingAndSpouseTests(BotTestCase):
    async def test_sibling_wording_follows_the_choice(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_SISTER)
        self.assertIn("her first name", chat.text)

        await chat.say("Mariam")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SIBLING)
        self.assertEqual(entry["sex"], "F")
        self.assertIn("sister", submissions.describe(self.queued()[0]["payload"]))

    async def test_spouse_records_a_family_name(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SPOUSE)
        await chat.tap(texts.SPOUSE_WIFE)
        await chat.say("Therese")
        await chat.tap(texts.YES)
        await chat.say("Obeid")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        entry = self.queued()[0]["payload"]["people"][0]
        self.assertEqual(entry["role"], submissions.SPOUSE)
        self.assertEqual(entry["family_name"], "Obeid")


class DuplicateFlaggingTests(BotTestCase):
    async def test_probable_duplicate_is_flagged_for_the_admin(self):
        """Two relatives submitting the same person is the common case."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_BROTHER)
        await chat.say("Georges")  # already in the tree, same branch
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        queued = self.queued()[0]
        self.assertEqual(queued["matched_person_id"], self.ids["georges"])

    async def test_flagging_never_merges_or_rejects(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_SIBLING)
        await chat.tap(texts.SIBLING_BROTHER)
        await chat.say("Georges")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        queued = self.queued()[0]
        self.assertEqual(queued["status"], "pending")
        self.assertEqual(self.people_count(), 12)

    async def test_unrelated_name_is_not_flagged(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Zaher")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        self.assertIsNone(self.queued()[0]["matched_person_id"])


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
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        await chat.tap(texts.MENU_FIX)
        self.assertIn("Ritta", str(chat.buttons))
        await chat.tap("Ritta")
        await chat.say("Her name is Rita, one t")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        queued = self.queued()
        self.assertEqual(len(queued), 2)
        correction = queued[1]["payload"]
        self.assertEqual(correction["kind"], submissions.CORRECTION)
        self.assertEqual(correction["target_submission_id"], queued[0]["id"])
        self.assertIn("one t", correction["note"])
        self.assertIn(texts.FIX_SAVED, chat.transcript())

    async def test_a_correction_changes_nothing_live(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        await chat.tap(texts.MENU_FIX)
        await chat.tap("Sami")
        await chat.say("Actually he is Samir")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        self.assertEqual(self.people_count(), 12)
        for row in self.queued():
            self.assertEqual(row["status"], "pending")

    async def test_the_original_submission_stays_untouched(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Sami")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        original = self.queued()[0]["payload"]

        await chat.tap(texts.MENU_FIX)
        await chat.tap("Sami")
        await chat.say("Actually he is Samir")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)

        self.assertEqual(self.queued()[0]["payload"], original)


class ConstraintTests(BotTestCase):
    """The rules the bot could quietly break."""

    def test_bot_never_calls_a_privileged_write(self):
        """Constraint 4, enforced against future edits.

        Looks for actual calls, so a comment explaining why the bot must not
        call these does not itself fail the test.
        """
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
        """No handler should be opening its own connection or writing SQL."""
        root = Path(__file__).resolve().parents[1] / "bot"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "store.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "INSERT" in text.upper():
                offenders.append(f"bot/{path.name} writes SQL")
            if "db.connect" in text:
                offenders.append(f"bot/{path.name} opens its own connection")
        self.assertEqual(offenders, [], "; ".join(offenders))

    def test_no_flow_can_ask_for_a_date(self):
        """Constraint 2 — the bot cannot express a date even if asked to."""
        # Whole words only: "marriage" is not a request for an age.
        forbidden = re.compile(
            r"\b(year|years|born|birth|birthday|died|death|date|dates|age|ages|when)\b"
        )
        for kind, flow in flows.BY_KIND.items():
            for step in flow.steps:
                prompt = step.text({"sex": "M"}).lower()
                found = forbidden.search(prompt)
                self.assertIsNone(
                    found,
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
            if label in (texts.MENU_VIEW, texts.MENU_FIX):
                continue
            fresh = Conversation(user_id=5001)
            await fresh.start()
            await fresh.tap(label)
            self.assertTrue(fresh.text, f"{label} produced no question")


class RobustnessTests(BotTestCase):
    async def test_typing_instead_of_tapping_a_choice_re_asks(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.say("a boy I think")
        self.assertIn(texts.NOT_UNDERSTOOD, chat.transcript())
        self.assertIn(texts.ASK_CHILD_SEX, chat.text)

    async def test_retyping_instead_of_tapping_no_just_works(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("Smai")
        await chat.say("Sami")  # correcting without touching the buttons
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Sami"
        )

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
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "Sami"
        )

    async def test_arabic_name_survives_the_round_trip(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_SON)
        await chat.say("خليل")
        await chat.tap(texts.YES)
        await chat.tap(texts.SEND_IT)
        self.assertEqual(
            self.queued()[0]["payload"]["people"][0]["given_name"], "خليل"
        )


class ConfirmationTests(BotTestCase):
    async def test_confirmation_does_not_repeat_itself(self):
        """A summary shown twice trains people to stop reading it."""
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_CHILD)
        await chat.tap(texts.CHILD_DAUGHTER)
        await chat.say("Rita")
        await chat.tap(texts.YES)

        body = chat.text.split(texts.CONFIRM_SUBMISSION)[-1].strip()
        self.assertEqual(body.count("Rita"), 1, body)

    async def test_confirmation_shows_detail_the_summary_omits(self):
        chat = await self.identified_as_khalil()
        await chat.tap(texts.MENU_ADD_PARENTS)
        await chat.say("Youssef")
        await chat.tap(texts.YES)
        await chat.say("Nada")
        await chat.tap(texts.YES)
        await chat.say("Karam")
        await chat.tap(texts.YES)

        self.assertIn("Youssef", chat.text)
        self.assertIn("Nada", chat.text)
        self.assertIn("Karam", chat.text)


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
        """No state should be a dead end with no cancel and no fallback."""
        from bot.main import build_conversation

        conversation = build_conversation()
        for state, handlers_in_state in conversation.states.items():
            self.assertTrue(handlers_in_state, f"state {state} has no handlers")
        self.assertTrue(conversation.fallbacks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
