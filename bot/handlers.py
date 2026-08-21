"""
The Telegram conversation.

The spec asks for one question per message, and for every typed answer to be
confirmed before it is used. Rather than write that twice for each of the five
flows, the flows are data (`flows.py`) and this module is one loop over them:

    ask  ->  read the answer  ->  confirm it  ->  advance  ->  ask

which gives five conversation states instead of about twenty-five.

The only thing this module ever writes to the family data is a row in
`submissions`, via `store.queue()`.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import config
import submissions
from bot import dictation, flows, store, texts, understand

log = logging.getLogger(__name__)

# --- conversation states ---------------------------------------------------

(
    MENU,
    ASK,
    CONFIRM_ANSWER,
    CONFIRM_SUBMIT,
    IDENTITY_MATCH,
    PICK_SUBMISSION,
    PICK_SUBJECT,
    CLIMB,
    ASK_SOURCE,
    REVIEW,
    EDIT_VALUE,
) = range(11)

# --- callback data ---------------------------------------------------------

CB_MENU = "menu"
CB_ANSWER = "ans"
CB_YES = "yes"
CB_NO = "no"
CB_SKIP = "skip"
CB_SEND = "send"
CB_OVER = "over"
CB_CANCEL = "cancel"
CB_IDENTITY = "who"
CB_NOBODY = "who:none"
CB_FIX = "fix"
CB_SUBJECT = "subj"
CB_SWITCH = "menu:switch"
CB_CLIMB_YES = "climb:yes"
CB_CLIMB_NO = "climb:no"
CB_SOURCE = "source"
CB_REVIEW = "menu:review"
CB_EDIT = "edit"
CB_SEND_ALL = "sendall"
CB_REMOVE = "remove"


# ===========================================================================
# Small helpers
# ===========================================================================


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


async def _say(update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None):
    """Send a message, whether we arrived here by text or by button press."""
    if update.callback_query is not None:
        await update.callback_query.answer()
        return await update.callback_query.message.reply_text(
            text, reply_markup=keyboard, disable_web_page_preview=True
        )
    return await update.effective_message.reply_text(
        text, reply_markup=keyboard, disable_web_page_preview=True
    )


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault("flow", {})


def _reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear the current flow. The basket and the cursor survive."""
    context.user_data.pop("flow", None)


def _basket(context: ContextTypes.DEFAULT_TYPE) -> list[dict[str, Any]]:
    """Everything collected but not yet sent.

    Nothing goes to the queue until the contributor has seen the whole list.
    They are typing names from memory, so the place to catch a misspelling is
    a list they can read, not a yes/no after every single answer.
    """
    return context.user_data.setdefault("basket", [])


def _draft_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    counter = context.user_data.get("draft_counter", 0) + 1
    context.user_data["draft_counter"] = counter
    return f"d{counter}"


def _cursor(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    """Who we are currently adding relatives for. None means the contributor."""
    return context.user_data.get("subject")


def _set_cursor(context: ContextTypes.DEFAULT_TYPE, subject: dict[str, Any] | None):
    if subject is None:
        context.user_data.pop("subject", None)
    else:
        context.user_data["subject"] = subject


async def _subject_of(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """The cursor as a payload subject, defaulting to the contributor."""
    cursor = _cursor(context)
    if cursor is not None:
        about = submissions.subject(
            person_id=cursor.get("person_id"),
            submission_id=cursor.get("submission_id"),
            label=cursor.get("label"),
        )
        if cursor.get("draft_id"):
            about["draft_id"] = cursor["draft_id"]
        return about
    who = await store.contributor_state(update.effective_user.id)
    return submissions.subject(
        person_id=who["person_id"],
        submission_id=who["identify_submission_id"],
        label=who["label"] or "themselves",
    )


def _subject_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    cursor = _cursor(context)
    return cursor["label"] if cursor else texts.SUBJECT_YOU


def _menu_keyboard(
    subject: str | None = None, basket_size: int = 0
) -> InlineKeyboardMarkup:
    labels = texts.menu_labels(subject)
    rows = [
        [_button(labels[key], f"{CB_MENU}:{flow.kind}")] for key, flow in flows.MENU
    ]
    if basket_size:
        rows.insert(
            0, [_button(texts.REVIEW_SEND.format(count=basket_size), CB_REVIEW)]
        )
    rows.append([_button(texts.MENU_SWITCH, CB_SWITCH)])
    rows.append([_button(texts.MENU_FIX, f"{CB_MENU}:{submissions.CORRECTION}")])
    rows.append([_button(texts.MENU_VIEW, f"{CB_MENU}:view")])
    return _kb(rows)


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, lead: str | None = None):
    _reset(context)
    cursor = _cursor(context)
    name = cursor["label"] if cursor else None
    parts = [lead] if lead else []
    if name:
        # Only worth saying when it is not the obvious default.
        parts.append(texts.SUBJECT_HEADING.format(name=name))
    parts.append(texts.MENU_PROMPT)
    await _say(update, "\n\n".join(parts), _menu_keyboard(name, len(_basket(context))))
    return MENU


# ===========================================================================
# Driving a flow
# ===========================================================================


def _begin(context: ContextTypes.DEFAULT_TYPE, flow: flows.Flow, **extra: Any) -> None:
    context.user_data["flow"] = {
        "kind": flow.kind,
        "answers": {},
        "index": 0,
        "pending": None,
        "extra": extra,
    }


def _current(context: ContextTypes.DEFAULT_TYPE) -> tuple[flows.Flow, flows.Step | None]:
    state = _state(context)
    flow = flows.BY_KIND[state["kind"]]
    step, index = flows.next_step(flow.steps, state["answers"], state["index"])
    state["index"] = index
    return flow, step


async def _ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Put the current question, or finish the flow if there are none left."""
    state = _state(context)
    cursor = _cursor(context)
    if flows.SUBJECT_KEY not in state["answers"]:
        state["answers"][flows.SUBJECT_KEY] = cursor["label"] if cursor else None

    flow, step = _current(context)

    if step is None:
        return await _complete(update, context, flow)

    rows: list[list[InlineKeyboardButton]] = []
    if step.type == flows.CHOICE:
        rows.extend(
            [[_button(label, f"{CB_ANSWER}:{value}")] for label, value in step.choices]
        )
    if step.optional:
        rows.append([_button(texts.SKIP, CB_SKIP)])
    rows.append([_button(texts.CANCEL, CB_CANCEL)])

    await _say(update, step.text(state["answers"]), _kb(rows))
    return ASK


async def _complete(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: flows.Flow):
    """All questions answered: build the payload and ask for a final yes."""
    state = _state(context)
    user_id = update.effective_user.id
    who = await store.contributor_state(user_id)

    # Identification is always about the contributor; everything else follows
    # the cursor, which is how they climb past their own parents.
    if flow.kind == submissions.IDENTIFY:
        about = submissions.subject(
            person_id=who["person_id"],
            submission_id=who["identify_submission_id"],
            label=who["label"] or "themselves",
        )
    else:
        about = await _subject_of(update, context)

    submitted_by = submissions.submitter(
        user_id, person_id=who["person_id"], label=who["label"]
    )

    try:
        payload = flow.build(
            state["answers"], submitted_by, about, **state.get("extra", {})
        )
    except flows.FlowError as problem:
        return await _show_menu(update, context, str(problem))
    except ValueError as problem:  # a payload the contract rejects
        log.warning("payload rejected for user %s: %s", user_id, problem)
        return await _show_menu(update, context, texts.ERROR)

    state["payload"] = payload

    if flow.kind == submissions.IDENTIFY:
        return await _offer_identity_matches(update, context, payload)

    if flow.kind == submissions.CORRECTION:
        # A correction is one thing about one thing; batching it would be odd.
        lines = "\n".join(submissions.detail_lines(payload))
        await _say(
            update,
            f"{texts.CONFIRM_SUBMISSION}\n\n{lines}",
            _kb(
                [
                    [_button(texts.SEND_IT, CB_SEND)],
                    [_button(texts.CANCEL, CB_CANCEL)],
                ]
            ),
        )
        return CONFIRM_SUBMIT

    draft_id = _draft_id(context)
    payload["_draft_id"] = draft_id
    _basket(context).append(payload)
    _reset(context)
    return await _after_add(update, context, payload)


async def _after_add(update: Update, context: ContextTypes.DEFAULT_TYPE, payload):
    """Confirm what went in the basket and offer the obvious next step."""
    added = texts.ADDED.format(summary=submissions.describe(payload))

    entries = [
        entry
        for entry in payload.get("people") or []
        if entry.get("role") != submissions.SELF
    ]
    entries.sort(key=lambda e: 0 if e["role"] == submissions.FATHER else 1)

    if entries:
        target = entries[0]
        context.user_data["climb_to"] = {
            "label": submissions.person_label(target),
            "draft_id": payload["_draft_id"],
        }
        rows = [
            [_button(texts.CLIMB_YES, CB_CLIMB_YES)],
            [_button(texts.ADD_MORE, CB_CLIMB_NO)],
            [
                _button(
                    texts.REVIEW_SEND.format(count=len(_basket(context))), CB_REVIEW
                )
            ],
        ]
        await _say(
            update,
            f"{added}\n\n" + texts.CLIMB_PARENTS.format(name=target["given_name"]),
            _kb(rows),
        )
        return CLIMB

    return await _show_menu(update, context, added)


# ===========================================================================
# /start and identification
# ===========================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    who = await store.contributor_state(user_id)

    if who["person_id"] is not None:
        return await _show_menu(
            update, context, texts.IDENTITY_ALREADY_LINKED.format(name=who["label"])
        )

    if who["identify_submission_id"] is not None:
        # They introduced themselves already and are waiting on a reviewer.
        # No reason to make them do it twice.
        return await _show_menu(update, context, texts.IDENTITY_QUEUED)

    await _say(update, texts.WELCOME)
    _begin(context, flows.IDENTIFY)
    return await _ask(update, context)


async def _offer_identity_matches(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: dict[str, Any]
):
    """Show who we think they are. They decide; the matcher only suggests."""
    entry = payload["people"][0]
    candidates = await store.identity_candidates(
        entry["given_name"], entry.get("father_given_name")
    )

    if not candidates:
        return await _queue_identity(update, context, payload)

    _state(context)["candidates"] = candidates
    rows = [
        [_button(candidate["label"], f"{CB_IDENTITY}:{candidate['person_id']}")]
        for candidate in candidates
    ]
    rows.append([_button(texts.IDENTITY_NONE_OF_THESE, CB_NOBODY)])
    await _say(update, texts.IDENTITY_GUESS, _kb(rows))
    return IDENTITY_MATCH


async def _queue_identity(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: dict[str, Any]
):
    user_id = update.effective_user.id
    await store.queue(user_id, payload)
    await store.remember_label(user_id, submissions.person_label(payload["people"][0]))
    return await _show_menu(update, context, texts.IDENTITY_QUEUED)


async def on_identity_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == CB_NOBODY:
        payload = _state(context).get("payload")
        if payload is None:
            return await start(update, context)
        return await _queue_identity(update, context, payload)

    person_id = int(query.data.split(":", 1)[1])
    linked = await store.link_contributor(update.effective_user.id, person_id)
    return await _show_menu(
        update, context, texts.IDENTITY_CONFIRMED.format(name=linked["label"])
    )


# ===========================================================================
# The menu
# ===========================================================================


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "view":
        text = (
            texts.VIEW_TREE.format(url=config.PUBLIC_URL)
            if config.PUBLIC_URL
            else texts.VIEW_TREE_UNPUBLISHED
        )
        return await _show_menu(update, context, text)

    if choice == "review":
        return await _show_review(update, context)

    if choice == "switch":
        return await _pick_subject(update, context)

    if choice == submissions.CORRECTION:
        return await _start_correction(update, context)

    flow = flows.BY_KIND[choice]
    _begin(context, flow)

    if flow.kind == submissions.ADD_PARENTS and _cursor(context) is None:
        # They told us their father's name when they signed up. Asking again
        # two minutes later reads as if the bot was not listening.
        who = await store.contributor_state(update.effective_user.id)
        known = who.get("father_given_name")
        if known:
            state = _state(context)
            state["answers"]["father_given"] = known
            state["index"] = 1

    return await _ask(update, context)


# ===========================================================================
# Answering
# ===========================================================================


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A typed answer. Validate it, then read it back for confirmation."""
    state = _state(context)
    if not state:
        return await start(update, context)

    _flow, step = _current(context)
    if step is None:
        return await _ask(update, context)

    typed = update.effective_message.text or ""

    if step.optional and understand.is_skip(typed):
        # "dunno" is an answer. Making them find the button for it is not.
        state["answers"][step.id] = None
        state["index"] += 1
        return await _ask(update, context)

    if step.type == flows.CHOICE:
        chosen = understand.match_choice(step.choices, typed)
        if chosen is None:
            await _say(update, texts.NOT_UNDERSTOOD)
            return await _ask(update, context)
        state["answers"][step.id] = chosen
        state["index"] += 1
        return await _ask(update, context)

    # Somebody who already knows will type the whole family in one go. The
    # question asked for one name, but refusing the answer loses the person
    # worth listening to most.
    if step.type in (flows.NAME, flows.TEXT) and dictation.looks_like_dictation(typed):
        reading = dictation.parse(
            typed,
            default_role=flows.default_role(_state(context)["kind"], step.id),
            subject_name=_subject_name_or_none(context),
            known_names=await _known_names(update, context),
        )
        if reading:
            return await _absorb_dictation(update, context, reading)

    try:
        value = flows.clean(step, typed)
    except flows.FlowError as problem:
        await _say(update, str(problem))
        return await _ask(update, context)

    # No read-back. Confirming every single name doubled the taps and made the
    # whole thing feel like a form. Everything is checkable and editable on the
    # review screen before it sends, which is where a misspelling actually gets
    # noticed anyway.
    state["answers"][step.id] = value
    state["index"] += 1
    return await _ask(update, context)


async def on_answer_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = _state(context)
    _flow, step = _current(context)

    if query.data == CB_NO or step is None:
        state["pending"] = None
        await _say(update, texts.RETYPE)
        return await _ask(update, context)

    state["answers"][step.id] = state.pop("pending", None)
    state["index"] += 1
    return await _ask(update, context)


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A button answer, or a skip. Buttons need no confirmation."""
    query = update.callback_query
    await query.answer()
    state = _state(context)
    _flow, step = _current(context)
    if step is None:
        return await _ask(update, context)

    if query.data == CB_SKIP:
        state["answers"][step.id] = None
    else:
        state["answers"][step.id] = query.data.split(":", 1)[1]

    state["index"] += 1
    return await _ask(update, context)


# ===========================================================================
# Sending
# ===========================================================================


async def on_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = _state(context)

    if query.data == CB_OVER:
        state["answers"] = {}
        state["index"] = 0
        state["pending"] = None
        return await _ask(update, context)

    payload = state.get("payload")
    if payload is None:
        return await _show_menu(update, context, texts.ERROR)
    return await _send(update, context, payload)


async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, payload):
    result = await store.queue(update.effective_user.id, payload)
    context.user_data["last_submission_id"] = result["submission_id"]

    if payload["kind"] == submissions.CORRECTION:
        return await _show_menu(update, context, texts.FIX_SAVED)
    if payload["kind"] == submissions.IDENTIFY:
        return await _show_menu(update, context, texts.SAVED)
    return await _offer_climb(update, context, payload)


# ===========================================================================
# Moving the cursor
# ===========================================================================
#
# An uncle is your father's brother. Rather than a flow per relation, the
# subject moves and the same five questions cover everything.
# ===========================================================================


async def _pick_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    candidates = await store.subject_candidates(update.effective_user.id)
    if not candidates:
        return await _show_menu(update, context, texts.SWITCH_NOBODY)

    context.user_data["subject_choices"] = candidates
    rows = []
    for index, candidate in enumerate(candidates):
        label = candidate["label"]
        if candidate.get("note"):
            label = f"{label} ({candidate['note']})"
        rows.append([_button(_trim(label), f"{CB_SUBJECT}:{index}")])
    rows.append([_button(texts.CANCEL, CB_CANCEL)])

    await _say(update, texts.SWITCH_PICK, _kb(rows))
    return PICK_SUBJECT


async def on_pick_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    index = int(query.data.split(":", 1)[1])
    choices = context.user_data.get("subject_choices") or []
    if index >= len(choices):
        return await _show_menu(update, context, texts.ERROR)

    chosen = choices[index]
    _set_cursor(context, None if chosen.get("note") == "you" else chosen)
    return await _show_menu(
        update, context, texts.SWITCHED.format(name=chosen["label"])
    )


# ===========================================================================
# Climbing
# ===========================================================================
#
# After a save, offer the obvious next step. This is what walks a contributor
# up the generations instead of leaving them stranded at their own parents.
# ===========================================================================


async def _offer_climb(update: Update, context: ContextTypes.DEFAULT_TYPE, payload):
    """Suggest moving to the person just added and asking about their parents."""
    entries = [
        entry
        for entry in payload.get("people") or []
        if entry.get("role") in (submissions.FATHER, submissions.MOTHER,
                                 submissions.SIBLING, submissions.CHILD,
                                 submissions.SPOUSE)
    ]
    if not entries:
        return await _show_menu(update, context, texts.SAVED)

    # Prefer the father: the patriline is what carries a branch upward.
    entries.sort(key=lambda e: 0 if e["role"] == submissions.FATHER else 1)
    target = entries[0]

    context.user_data["climb_to"] = {
        "label": submissions.person_label(target),
        "sex": target.get("sex"),
    }
    await _say(
        update,
        f"{texts.SAVED}\n\n"
        + texts.CLIMB_PARENTS.format(name=target["given_name"]),
        _kb(
            [
                [_button(texts.CLIMB_YES, CB_CLIMB_YES)],
                [_button(texts.CLIMB_NO, CB_CLIMB_NO)],
            ]
        ),
    )
    return CLIMB


async def on_climb_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """"yes", "nah", "ok" — all perfectly good answers to a yes/no question."""
    answer = understand.yes_no(update.effective_message.text or "")
    if answer is True:
        return await _climb_yes(update, context)
    if answer is False:
        return await _show_menu(update, context)

    target = context.user_data.get("climb_to") or {}
    name = target.get("label", "them").split()[0]
    await _say(
        update,
        texts.CLIMB_PARENTS.format(name=name),
        _kb(
            [
                [_button(texts.CLIMB_YES, CB_CLIMB_YES)],
                [_button(texts.ADD_MORE, CB_CLIMB_NO)],
            ]
        ),
    )
    return CLIMB


async def on_climb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == CB_CLIMB_NO:
        return await _show_menu(update, context)

    # The person we just named is still in the basket, so point the cursor at
    # the draft. Send resolves it to a real submission id.
    target = context.user_data.get("climb_to") or {}
    if not target:
        return await _show_menu(update, context, texts.ERROR)

    return await _climb_yes(update, context)


async def _climb_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = context.user_data.get("climb_to") or {}
    if not target:
        return await _show_menu(update, context, texts.ERROR)

    _set_cursor(
        context,
        {
            "person_id": None,
            "submission_id": None,
            "draft_id": target.get("draft_id"),
            "label": target["label"],
        },
    )
    _begin(context, flows.ADD_PARENTS)
    return await _ask(update, context)


# ===========================================================================
# Who told you this
# ===========================================================================


async def on_source_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _say(update, texts.SOURCE_ASK, _kb([[_button(texts.CANCEL, CB_CANCEL)]]))
    return ASK_SOURCE


async def on_source_given(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = _state(context)
    payload = state.get("payload")
    if payload is None:
        return await _show_menu(update, context, texts.ERROR)
    try:
        payload["source"] = flows.clean_text(update.effective_message.text or "")
    except flows.FlowError as problem:
        await _say(update, str(problem))
        return ASK_SOURCE
    return await _send(update, context, payload)


# ===========================================================================
# A whole family in one message
# ===========================================================================


def _subject_name_or_none(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    cursor = _cursor(context)
    return cursor["label"] if cursor else None


def _join(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


async def _known_names(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> set[str]:
    """Given names this contributor could plausibly be talking about."""
    names = set()
    for payload in _basket(context):
        for entry in payload.get("people") or []:
            if entry.get("given_name"):
                names.add(entry["given_name"])
    for candidate in await store.subject_candidates(update.effective_user.id):
        first = (candidate["label"] or "").split()
        if first:
            names.add(first[0])
    return names


def _find_in_basket(
    context: ContextTypes.DEFAULT_TYPE, name: str
) -> dict[str, Any] | None:
    target = name.casefold()
    for payload in _basket(context):
        for entry in payload.get("people") or []:
            if entry.get("given_name", "").casefold() == target:
                return entry
    return None


def _note_in_basket(
    context: ContextTypes.DEFAULT_TYPE, name: str, remark: str
) -> bool:
    """Attach a fact to somebody still in the basket. True if it landed."""
    entry = _find_in_basket(context, name)
    if entry is None:
        return False
    entry["notes"] = f"{entry['notes']}; {remark}" if entry.get("notes") else remark
    return True


def _alias_in_basket(
    context: ContextTypes.DEFAULT_TYPE, name: str, alias: str
) -> bool:
    entry = _find_in_basket(context, name)
    if entry is None or entry.get("also_known_as"):
        return False
    entry["also_known_as"] = alias
    return True


async def _resolve_named_subject(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> dict[str, Any] | None:
    """Find who a message means when it names its own subject.

    "Wadiha is the daughter of..." is about Wadiha, whoever the bot happened
    to be asking about. She may be in the tree, or still in this contributor's
    basket — both count, because making somebody wait for an admin before they
    can name their grandmother's parents is how a session ends.
    """
    target = name.casefold()

    for payload in _basket(context):
        for entry in payload.get("people") or []:
            if entry.get("given_name", "").casefold() == target:
                return {
                    "person_id": None,
                    "submission_id": None,
                    "draft_id": payload.get("_draft_id"),
                    "label": submissions.person_label(entry),
                }

    for candidate in await store.subject_candidates(update.effective_user.id):
        first = (candidate["label"] or "").split()
        if first and first[0].casefold() == target:
            return {
                "person_id": candidate["person_id"],
                "submission_id": candidate["submission_id"],
                "draft_id": None,
                "label": candidate["label"],
            }
    return None


async def _absorb_dictation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reading: dictation.Reading
):
    """Turn a dictated family into basket entries, then show them for checking.

    One entry per person, so each can be corrected or dropped on its own — and
    so a spouse can hang off the specific relative they married rather than
    off the group.
    """
    user_id = update.effective_user.id
    who = await store.contributor_state(user_id)

    lead_extra = ""
    if reading.subject:
        found = await _resolve_named_subject(update, context, reading.subject)
        if found is None:
            return await _show_menu(
                update,
                context,
                texts.DICTATED_SUBJECT_UNKNOWN.format(name=reading.subject),
            )
        _set_cursor(context, found)
        lead_extra = "\n\n" + texts.DICTATED_SUBJECT.format(name=found["label"])

    # Lines can name people other than whoever the bot was asking about:
    # "Hanna married Therese, kids are ...". Those hang off Hanna.
    anchors: dict[str, dict[str, Any] | None] = {}
    for name in dict.fromkeys(m.about for m in reading.people if m.about):
        anchors[name] = await _resolve_named_subject(update, context, name)

    unplaced = sorted(name for name, found in anchors.items() if found is None)
    if unplaced:
        lead_extra += texts.DICTATED_UNKNOWN_PEOPLE.format(
            names=_join(unplaced)
        )
    placed = sorted(name for name, found in anchors.items() if found is not None)
    if placed:
        lead_extra += "\n\n" + texts.DICTATED_ABOUT_OTHERS.format(
            names=_join(placed)
        )

    # Facts and other names for people already recorded — "Khalil never
    # married", "Hanna (John)". Not new relatives; new information about
    # existing ones, which would otherwise fall on the floor.
    for name, remark in reading.remarks:
        if _note_in_basket(context, name, remark):
            lead_extra += "\n\n" + texts.DICTATED_REMARK.format(
                name=name, remark=remark
            )
    for name, alias in reading.aliases:
        _alias_in_basket(context, name, alias)

    about = await _subject_of(update, context)
    submitted_by = submissions.submitter(
        user_id, person_id=who["person_id"], label=who["label"]
    )
    note = "; ".join(reading.notes) or None

    _reset(context)
    drafts_by_label: dict[str, str] = {}

    def subject_for(mention) -> dict[str, Any] | None:
        """Whoever this mention hangs off: the line's own subject, or the cursor."""
        if not mention.about:
            return dict(about)
        found = anchors.get(mention.about)
        if found is None:
            return None
        subject = submissions.subject(
            person_id=found["person_id"],
            submission_id=found["submission_id"],
            label=found["label"],
        )
        if found.get("draft_id"):
            subject["draft_id"] = found["draft_id"]
        return subject

    # Parents are grouped so a father and mother arrive married to each other,
    # but they must be grouped BY SUBJECT: "Kalim's parents are Toufic and
    # Cilene" is about Kalim, not about whoever the cursor was left on.
    parent_groups: dict[str | None, list] = {}
    others = []
    for mention in reading.people:
        if mention.role in (submissions.FATHER, submissions.MOTHER):
            parent_groups.setdefault(mention.about, []).append(mention)
        else:
            others.append(mention)

    def entry_of(mention: dictation.Mention):
        return submissions.person(
            mention.role,
            mention.given_name,
            sex=mention.sex,
            family_name=mention.family_name,
            also_known_as=mention.also_known_as,
            notes=mention.note,
        )

    def stash(payload) -> str:
        draft_id = _draft_id(context)
        payload["_draft_id"] = draft_id
        _basket(context).append(payload)
        return draft_id

    try:
        for owner, parents in parent_groups.items():
            subject = subject_for(parents[0])
            if subject is None:
                continue  # nobody to hang them off; already reported above
            stash(
                submissions.build(
                    submissions.ADD_PARENTS,
                    submitted_by=submitted_by,
                    about=subject,
                    people=[entry_of(m) for m in parents],
                    note=note,
                )
            )

        for mention in others:
            if mention.role == submissions.SPOUSE and mention.spouse_of:
                # A wife hangs off the man she married, not off whoever we
                # started from. He may be in this batch, or already recorded
                # from an earlier one — dropping her when it is the latter was
                # losing whole marriages silently.
                anchor = drafts_by_label.get(mention.spouse_of)
                if anchor is not None:
                    subject = submissions.subject(label=mention.spouse_of)
                    subject["draft_id"] = anchor
                elif anchors.get(mention.about):
                    found = anchors[mention.about]
                    subject = submissions.subject(
                        person_id=found["person_id"],
                        submission_id=found["submission_id"],
                        label=found["label"],
                    )
                    if found.get("draft_id"):
                        subject["draft_id"] = found["draft_id"]
                else:
                    continue
            else:
                subject = subject_for(mention)
                if subject is None:
                    continue  # nobody to hang it off; already reported above

            kind = {
                submissions.SIBLING: submissions.ADD_SIBLING,
                submissions.CHILD: submissions.ADD_CHILD,
                submissions.SPOUSE: submissions.ADD_SPOUSE,
            }[mention.role]
            draft_id = stash(
                submissions.build(
                    kind,
                    submitted_by=submitted_by,
                    about=subject,
                    people=[entry_of(mention)],
                    note=note,
                )
            )
            drafts_by_label[mention.label()] = draft_id
    except ValueError as problem:
        log.warning("dictation rejected for %s: %s", user_id, problem)
        return await _show_menu(update, context, texts.ERROR)

    lead = texts.DICTATED.format(count=len(reading)) + lead_extra
    guesses = list(
        dict.fromkeys(reason for m in reading.people for reason in m.uncertain)
    )
    if guesses:
        template = (
            texts.DICTATED_UNSURE_ONE if len(guesses) == 1 else texts.DICTATED_UNSURE
        )
        lead += template.format(reasons=", and ".join(guesses))
    return await _show_review(update, context, lead)


# ===========================================================================
# Review and send
# ===========================================================================
#
# One screen showing everything collected, every line editable. This is where
# a misspelling actually gets caught — reading back a list is a different act
# from answering yes to one name at a time.
# ===========================================================================


def _basket_lines(basket: list[dict[str, Any]]) -> list[str]:
    return [
        f"{index}. {submissions.describe(payload)}"
        for index, payload in enumerate(basket, start=1)
    ]


async def _show_review(update: Update, context: ContextTypes.DEFAULT_TYPE, lead=None):
    basket = _basket(context)
    if not basket:
        return await _show_menu(update, context, texts.REVIEW_EMPTY)

    rows = []
    for index, payload in enumerate(basket):
        entry = submissions.primary_person(payload)
        label = submissions.person_label(entry) if entry else f"item {index + 1}"
        rows.append([_button(_trim(f"{index + 1}. {label}"), f"{CB_EDIT}:{index}")])
    rows.append(
        [_button(texts.SEND_ALL.format(count=len(basket)), CB_SEND_ALL)]
    )
    rows.append([_button(texts.ADD_MORE, CB_CANCEL)])

    body = "\n".join(_basket_lines(basket))
    parts = [part for part in (lead, texts.REVIEW_HEADING, body) if part]
    await _say(update, "\n\n".join(parts), _kb(rows))
    return REVIEW


async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _show_review(update, context)


async def on_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    index = int(query.data.split(":", 1)[1])
    basket = _basket(context)
    if index >= len(basket):
        return await _show_review(update, context, texts.ERROR)

    entry = submissions.primary_person(basket[index])
    context.user_data["editing"] = index
    await _say(
        update,
        texts.EDIT_ASK.format(name=submissions.person_label(entry)),
        _kb(
            [
                [_button(texts.REMOVE, CB_REMOVE)],
                [_button(texts.CANCEL, CB_CANCEL)],
            ]
        ),
    )
    return EDIT_VALUE


async def on_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    index = context.user_data.get("editing")
    basket = _basket(context)
    if index is None or index >= len(basket):
        return await _show_review(update, context, texts.ERROR)

    try:
        value = flows.clean_name(update.effective_message.text or "")
    except flows.FlowError as problem:
        await _say(update, str(problem))
        return EDIT_VALUE

    entry = submissions.primary_person(basket[index])
    entry["given_name"] = value
    context.user_data.pop("editing", None)
    return await _show_review(update, context, texts.EDITED.format(name=value))


async def on_edit_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    index = context.user_data.pop("editing", None)
    basket = _basket(context)
    if index is not None and index < len(basket):
        removed = basket.pop(index)
        # Anything that hung off the removed draft has lost its anchor.
        orphan = removed.get("_draft_id")
        for payload in list(basket):
            if orphan and (payload.get("about") or {}).get("draft_id") == orphan:
                basket.remove(payload)
    return await _show_review(update, context, texts.REMOVED)


async def on_review_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.effective_message.text or ""
    if understand.match_choice([(texts.SEND_ALL.format(count=""), "send")], typed):
        return await on_send_all(update, context)
    if understand.yes_no(typed) is True:
        return await on_send_all(update, context)
    return await _show_review(update, context)


async def on_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A typed yes at the final screen should send, not be refused."""
    if understand.yes_no(update.effective_message.text or "") is True:
        payload = _state(context).get("payload")
        if payload is not None:
            return await _send(update, context, payload)
    return await _show_menu(update, context, texts.CANCELLED)


async def on_send_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Queue the whole basket, oldest first, resolving draft references."""
    if update.callback_query is not None:
        await update.callback_query.answer()
    basket = _basket(context)
    if not basket:
        return await _show_menu(update, context, texts.REVIEW_EMPTY)

    user_id = update.effective_user.id
    draft_to_submission: dict[str, int] = {}
    sent = 0

    for payload in basket:
        about = payload.get("about") or {}
        anchor = about.pop("draft_id", None)
        if anchor:
            # The parent draft was sent a moment ago; point at its real row.
            about["submission_id"] = draft_to_submission.get(anchor)
        draft_id = payload.pop("_draft_id", None)

        result = await store.queue(user_id, payload)
        if draft_id:
            draft_to_submission[draft_id] = result["submission_id"]
        sent += 1

    context.user_data["basket"] = []
    context.user_data.pop("draft_counter", None)
    _set_cursor(context, None)
    return await _show_menu(update, context, texts.SAVED)


# ===========================================================================
# Fix something I submitted
# ===========================================================================


async def _start_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mine = await store.recent_submissions(update.effective_user.id)
    if not mine:
        return await _show_menu(update, context, texts.FIX_NOTHING_YET)

    rows = []
    for item in mine:
        status = texts.FIX_STATUS.get(item["status"], item["status"])
        label = f"{item['summary']} — {status}"
        rows.append([_button(_trim(label), f"{CB_FIX}:{item['id']}")])
    rows.append([_button(texts.BACK_TO_MENU, CB_CANCEL)])

    await _say(update, texts.FIX_PICK, _kb(rows))
    return PICK_SUBMISSION


def _trim(label: str, limit: int = 60) -> str:
    """Telegram truncates long button labels awkwardly; do it deliberately."""
    return label if len(label) <= limit else label[: limit - 1] + "…"


async def on_pick_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    submission_id = int(query.data.split(":", 1)[1])

    chosen = None
    for item in await store.recent_submissions(update.effective_user.id):
        if item["id"] == submission_id:
            chosen = item
            break
    if chosen is None:
        return await _show_menu(update, context, texts.ERROR)

    _begin(
        context,
        flows.CORRECTION,
        target_submission_id=submission_id,
        target_person_id=chosen["person_id"],
        target_label=chosen["summary"],
    )
    return await _ask(update, context)


# ===========================================================================
# Commands and fallbacks
# ===========================================================================


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _show_menu(update, context, texts.CANCELLED)


async def on_cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _show_menu(update, context, texts.CANCELLED)


async def share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _say(update, texts.SHARE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _say(update, texts.HELP)


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The menu is where people rest, so it is where they start typing."""
    typed = update.effective_message.text or ""
    if dictation.looks_like_dictation(typed):
        reading = dictation.parse(
            typed,
            subject_name=_subject_name_or_none(context),
            known_names=await _known_names(update, context),
        )
        if reading:
            return await _absorb_dictation(update, context, reading)
        return await _show_menu(update, context, texts.DICTATED_NOTHING)
    return await _show_menu(update, context, texts.MENU_TYPE_HINT)


async def on_stray_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text arriving when we are waiting for a button press."""
    await _say(update, texts.NOT_UNDERSTOOD)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(texts.ERROR)
        except Exception:  # the reply itself can fail; nothing more to do
            log.exception("could not deliver the error message")
