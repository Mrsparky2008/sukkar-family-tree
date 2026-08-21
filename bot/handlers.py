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
from bot import flows, store, texts

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
) = range(9)

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
    context.user_data.pop("flow", None)


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
        return submissions.subject(
            person_id=cursor.get("person_id"),
            submission_id=cursor.get("submission_id"),
            label=cursor.get("label"),
        )
    who = await store.contributor_state(update.effective_user.id)
    return submissions.subject(
        person_id=who["person_id"],
        submission_id=who["identify_submission_id"],
        label=who["label"] or "themselves",
    )


def _subject_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    cursor = _cursor(context)
    return cursor["label"] if cursor else texts.SUBJECT_YOU


def _menu_keyboard(subject: str | None = None) -> InlineKeyboardMarkup:
    labels = texts.menu_labels(subject)
    rows = [
        [_button(labels[key], f"{CB_MENU}:{flow.kind}")] for key, flow in flows.MENU
    ]
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
    await _say(update, "\n\n".join(parts), _menu_keyboard(name))
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

    lines = "\n".join(submissions.detail_lines(payload))
    await _say(
        update,
        f"{texts.CONFIRM_SUBMISSION}\n\n{lines}",
        _kb(
            [
                [_button(texts.SEND_IT, CB_SEND)],
                [_button(texts.SOURCE_BUTTON, CB_SOURCE)],
                [_button(texts.START_OVER, CB_OVER)],
                [_button(texts.CANCEL, CB_CANCEL)],
            ]
        ),
    )
    return CONFIRM_SUBMIT


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

    if choice == "switch":
        return await _pick_subject(update, context)

    if choice == submissions.CORRECTION:
        return await _start_correction(update, context)

    _begin(context, flows.BY_KIND[choice])
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

    if step.type == flows.CHOICE:
        await _say(update, texts.NOT_UNDERSTOOD)
        return await _ask(update, context)

    try:
        value = flows.clean(step, update.effective_message.text or "")
    except flows.FlowError as problem:
        await _say(update, str(problem))
        return await _ask(update, context)

    state["pending"] = value
    await _say(
        update,
        texts.CONFIRM_NAME.format(name=value),
        _kb(
            [
                [_button(texts.YES, CB_YES)],
                [_button(texts.NO_RETYPE, CB_NO)],
                [_button(texts.CANCEL, CB_CANCEL)],
            ]
        ),
    )
    return CONFIRM_ANSWER


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


async def on_climb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == CB_CLIMB_NO:
        return await _show_menu(update, context, texts.DEAD_END)

    # The person we just named is still in the queue, so point the cursor at
    # their submission rather than waiting on an admin.
    target = context.user_data.get("climb_to") or {}
    submission_id = context.user_data.get("last_submission_id")
    if not target or submission_id is None:
        return await _show_menu(update, context, texts.ERROR)

    _set_cursor(
        context,
        {
            "person_id": None,
            "submission_id": submission_id,
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
