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
import html as html_escape_module

from bot import dictation, flows, sketch, store, texts, understand

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
    CLARIFY,
    TOUR,
) = range(13)

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
CB_SEX = "sexq"
CB_LINK = "linkq"
CB_SELF = "selfq"
CB_TOUR = "tour"


# ===========================================================================
# Small helpers
# ===========================================================================


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def _button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


async def _say(
    update: Update,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    html: bool = False,
):
    """Send a message, whether we arrived here by text or by button press."""
    kwargs = dict(
        reply_markup=keyboard,
        disable_web_page_preview=True,
        parse_mode=ParseMode.HTML if html else None,
    )
    if update.callback_query is not None:
        await update.callback_query.answer()
        return await update.callback_query.message.reply_text(text, **kwargs)
    return await update.effective_message.reply_text(text, **kwargs)


async def _sketch_of(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """The contributor's work so far, as a monospace drawing. May be empty."""
    who = await store.contributor_state(update.effective_user.id)
    ids: dict[str, int] = {}
    taken: set[str] = set()
    for candidate in await store.subject_candidates(update.effective_user.id):
        if not candidate.get("person_id"):
            continue
        label = (candidate["label"] or "").split(" (")[0]
        ids[label] = candidate["person_id"]
        first = label.split()[0] if label else ""
        # A bare given name maps only while it is unambiguous — two Toufics
        # is the whole reason the numbers exist.
        if first in taken:
            ids.pop(first, None)
        elif first and first not in ids:
            ids[first] = candidate["person_id"]
            taken.add(first)
    drawing = sketch.build(
        await store.approved_payloads(update.effective_user.id)
        + list(_basket(context)),
        self_name=(who["label"] or "you").split(" (")[0],
        self_father=who.get("father_given_name"),
        ids=ids,
    )
    if not drawing:
        return ""
    return "<pre>" + html_escape_module.escape(drawing) + "</pre>"


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


#: Payload role -> the kin table's (kind, fixed sex). Parents carry their sex
#: in the role itself; everyone else carries it in the entry.
_ROLE_KIN: dict[str, tuple[str, str | None]] = {
    submissions.FATHER: ("parent", "M"),
    submissions.MOTHER: ("parent", "F"),
    submissions.SIBLING: ("sibling", None),
    submissions.SPOUSE: ("partner", None),
    submissions.CHILD: ("child", None),
}


def _is_self(about: dict[str, Any] | None, who: dict[str, Any]) -> bool:
    """Whether a payload's subject is the contributor themselves."""
    about = about or {}
    if about.get("draft_id"):
        return False
    if about.get("person_id") and about["person_id"] == who.get("person_id"):
        return True
    if (
        about.get("submission_id")
        and about["submission_id"] == who.get("identify_submission_id")
    ):
        return True
    return (about.get("label") or "") in ("themselves", who.get("label") or "")


def _origin_of(
    context: ContextTypes.DEFAULT_TYPE, cursor: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The basket payload and entry that introduced the cursor person."""
    draft_id = cursor.get("draft_id")
    if not draft_id:
        return None, None
    label = cursor.get("label") or ""
    for payload in _basket(context):
        if payload.get("_draft_id") != draft_id:
            continue
        for entry in payload.get("people") or []:
            if label in (submissions.person_label(entry), entry.get("given_name")):
                return payload, entry
        return payload, None
    return None, None


def _relation_of(
    context: ContextTypes.DEFAULT_TYPE,
    who: dict[str, Any],
    cursor: dict[str, Any],
) -> str | None:
    """"your brother", "Hanna's son" — how the cursor person relates back.

    "Adding relatives for: Toufic" about somebody's own brother reads as if
    the bot never listened; naming the relationship is what buys the
    confidence to keep typing."""
    note = cursor.get("note")
    if note and note != "waiting for review":
        return note
    payload, entry = _origin_of(context, cursor)
    if payload is None or entry is None:
        return None
    kind, fixed_sex = _ROLE_KIN.get(entry.get("role") or "", (None, None))
    if kind is None:
        return None
    kin = texts.kin_word(kind, fixed_sex or entry.get("sex"))
    about = payload.get("about") or {}
    owner = None if _is_self(about, who) else (about.get("label") or "").split()[0]
    return texts.relation_phrase(kin, owner or None)


async def _recorded_parents(
    context: ContextTypes.DEFAULT_TYPE,
    who: dict[str, Any],
    cursor: dict[str, Any] | None,
    depth: int = 0,
) -> list[str]:
    """Given names of parents already recorded for the cursor person.

    Recorded means anywhere: the tree, or still in this contributor's basket.
    A brother shares his parents with whoever he is a brother of, so the
    check follows one sibling hop — which is exactly the case that made the
    menu offer to add a man's parents to the person who shares them."""
    ref = cursor or {
        "person_id": who.get("person_id"),
        "submission_id": who.get("identify_submission_id"),
        "label": who.get("label"),
    }
    names: list[str] = []
    if ref.get("person_id"):
        names += await store.person_parents(ref["person_id"])

    for payload in _basket(context):
        if payload.get("kind") != submissions.ADD_PARENTS:
            continue
        about = payload.get("about") or {}
        aimed_here = (
            (ref.get("draft_id") and about.get("draft_id") == ref["draft_id"])
            or (ref.get("person_id") and about.get("person_id") == ref["person_id"])
            or (cursor is None and _is_self(about, who))
            or (
                ref.get("label")
                and not about.get("draft_id")
                and about.get("label") == ref["label"]
            )
        )
        if aimed_here:
            names += [
                entry["given_name"] for entry in payload.get("people") or []
            ]

    if not names and cursor is not None and depth < 3:
        payload, _entry = _origin_of(context, cursor)
        if payload is not None:
            about = payload.get("about") or {}
            if payload.get("kind") == submissions.ADD_SIBLING:
                up = None if _is_self(about, who) else dict(about)
                names = await _recorded_parents(context, who, up, depth + 1)
            elif payload.get("kind") == submissions.ADD_CHILD:
                # Their parent is the very person they were added under.
                names = [
                    part
                    for part in [(about.get("label") or "").split()[0]]
                    if part
                ]
    return list(dict.fromkeys(name for name in names if name))


def _menu_keyboard(
    subject: str | None = None, basket_size: int = 0, hide_parents: bool = False
) -> InlineKeyboardMarkup:
    labels = texts.menu_labels(subject)
    rows = [
        [_button(labels[key], f"{CB_MENU}:{flow.kind}")]
        for key, flow in flows.MENU
        if not (hide_parents and key == "parents")
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

    who = await store.contributor_state(update.effective_user.id)
    relation = _relation_of(context, who, cursor) if cursor else None
    parents = await _recorded_parents(context, who, cursor)
    # Both parents down: offering to add them again reads as if the bot
    # forgot, and answering would only manufacture a duplicate to review.
    hide_parents = len(parents) >= 2

    parts = [lead] if lead else []
    if name and relation:
        parts.append(
            texts.SUBJECT_HEADING_RELATED.format(name=name, relation=relation)
        )
    elif name:
        # Only worth saying when it is not the obvious default.
        parts.append(texts.SUBJECT_HEADING.format(name=name))
    if hide_parents:
        parts.append(texts.parents_already_down(name, " and ".join(parents[:2])))
    parts.append(texts.MENU_PROMPT)
    await _say(
        update,
        "\n\n".join(parts),
        _menu_keyboard(name, len(_basket(context)), hide_parents=hide_parents),
    )
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
        # Whose parents are unknown, climb toward them. A sibling's or child's
        # parents are already here — for them the useful question is their own
        # wife, husband and children.
        mode = (
            "parents"
            if target["role"] in (submissions.FATHER, submissions.MOTHER,
                                  submissions.SPOUSE)
            else "family"
        )
        context.user_data["climb_to"] = {
            "label": submissions.person_label(target),
            "draft_id": payload["_draft_id"],
            "mode": mode,
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
        message = html_escape_module.escape(
            f"{added}\n\n"
            + _climb_prompt(target["given_name"], target.get("sex"), mode)
        )
        if len(_basket(context)) % 3 == 0:
            drawing = await _sketch_of(update, context)
            if drawing:
                message = (
                    html_escape_module.escape(added)
                    + "\n\nSo far:\n" + drawing + "\n"
                    + html_escape_module.escape(
                        _climb_prompt(target["given_name"], target.get("sex"), mode)
                    )
                )
        await _say(update, message, _kb(rows), html=True)
        return CLIMB

    return await _show_menu(update, context, added)


def _climb_prompt(name: str, sex: str | None, mode: str) -> str:
    if mode == "parents":
        return texts.CLIMB_PARENTS.format(name=name)
    spouse = {"M": "a wife", "F": "a husband"}.get(sex or "", "a wife or husband")
    return texts.CLIMB_FAMILY.format(name=name, spouse=spouse)


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
    # A brand-new contributor: walk them through their family rather than
    # dropping them at a menu.
    context.user_data["tour_on"] = True
    return await _offer_tour(update, context, texts.IDENTITY_QUEUED)


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
# The guided tour
# ===========================================================================
#
# A brand-new contributor is not dropped in front of a menu. The bot leads,
# in the order that builds a family fastest: your parents, your brothers and
# sisters, your own household, then a generation up on each side —
# grandparents, uncles and aunties and who each of them married. Every step
# skippable, the menu one tap away, nothing asked twice — and any step made
# redundant by what they already entered is silently passed over.
# ===========================================================================

#: (step id, "flow" to launch Add-parents / "tell" to invite free text,
#:  whose corner of the family it is about)
_TOUR_STEPS = [
    ("own_parents", "flow", "self"),
    ("own_siblings", "tell", "self"),
    ("own_family", "tell", "self"),
    ("father_parents", "flow", "father"),
    ("father_siblings", "tell", "father"),
    ("mother_parents", "flow", "mother"),
    ("mother_siblings", "tell", "mother"),
]

#: What a bare name typed at a "tell" step most likely is.
_TOUR_DEFAULT_ROLE = {
    "own_siblings": submissions.SIBLING,
    "own_family": submissions.SPOUSE,
    "father_siblings": submissions.SIBLING,
    "mother_siblings": submissions.SIBLING,
}


async def _tour_next(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> dict[str, Any] | None:
    who = await store.contributor_state(update.effective_user.id)
    done = context.user_data.setdefault("tour_done", [])

    parents = await store.own_parent_names(update.effective_user.id)
    for payload in _basket(context):
        if payload.get("kind") == submissions.ADD_PARENTS and _is_self(
            payload.get("about") or {}, who
        ):
            for entry in payload.get("people") or []:
                key = "father" if entry["role"] == submissions.FATHER else "mother"
                parents.setdefault(key, entry["given_name"])

    for step, kind, side in _TOUR_STEPS:
        if step in done:
            continue
        if side == "self":
            if step == "own_parents" and len(
                await _recorded_parents(context, who, None)
            ) >= 2:
                done.append(step)
                continue
            return {"step": step, "kind": kind, "cursor": None}

        name = parents.get(side)
        if not name:
            continue  # not known yet; may become known, so not marked done
        found = await _resolve_named_subject(update, context, name)
        if found is None:
            continue
        if kind == "flow" and len(
            await _recorded_parents(context, who, found)
        ) >= 2:
            done.append(step)
            continue
        return {
            "step": step,
            "kind": kind,
            "cursor": found,
            "name": name,
            "sex": "M" if side == "father" else "F",
        }
    return None


def _tour_prompt(step: dict[str, Any]) -> str:
    return {
        "own_parents": lambda: texts.TOUR_OWN_PARENTS,
        "own_siblings": lambda: texts.TOUR_OWN_SIBLINGS,
        "own_family": lambda: texts.TOUR_OWN_FAMILY,
        "father_parents": lambda: texts.tour_grandparents(step["name"]),
        "mother_parents": lambda: texts.tour_grandparents(step["name"]),
        "father_siblings": lambda: texts.tour_parent_siblings(
            step["name"], step["sex"]
        ),
        "mother_siblings": lambda: texts.tour_parent_siblings(
            step["name"], step["sex"]
        ),
    }[step["step"]]()


async def _offer_tour(
    update: Update, context: ContextTypes.DEFAULT_TYPE, lead: str | None = None
):
    """Show the next tour step — or the menu, for anyone past the tour."""
    if not context.user_data.get("tour_on"):
        return await _show_menu(update, context, lead)

    step = await _tour_next(update, context)
    if step is None:
        context.user_data["tour_on"] = False
        _set_cursor(context, None)
        count = await store.count_contributions(update.effective_user.id) + sum(
            len(p.get("people") or []) for p in _basket(context)
        )
        closing = texts.TOUR_DONE.format(count=count)
        return await _show_menu(
            update, context, f"{lead}\n\n{closing}" if lead else closing
        )

    context.user_data["tour_step"] = step["step"]
    context.user_data["tour_kind"] = step["kind"]
    _set_cursor(context, step.get("cursor"))

    if step["kind"] == "flow":
        first = [_button(texts.TOUR_LETS_GO, f"{CB_TOUR}:go")]
    else:
        none_label = (
            texts.TOUR_NONE_FAMILY
            if step["step"] == "own_family"
            else texts.TOUR_NONE_SIBLINGS
        )
        first = [_button(none_label, f"{CB_TOUR}:none")]
    rows = [
        first,
        [_button(texts.TOUR_SKIP, f"{CB_TOUR}:skip")],
        [_button(texts.TOUR_MENU, f"{CB_TOUR}:menu")],
    ]
    prompt = _tour_prompt(step)
    await _say(update, f"{lead}\n\n{prompt}" if lead else prompt, _kb(rows))
    return TOUR


def _tour_mark_done(context: ContextTypes.DEFAULT_TYPE) -> None:
    step = context.user_data.pop("tour_step", None)
    if step is not None:
        done = context.user_data.setdefault("tour_done", [])
        if step not in done:
            done.append(step)


async def on_tour_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    choice = update.callback_query.data.split(":", 1)[1]

    if choice == "menu":
        # They want to drive. Fine — the tree grows in any direction.
        context.user_data["tour_on"] = False
        return await _show_menu(update, context)

    if choice in ("none", "skip"):
        _tour_mark_done(context)
        return await _offer_tour(update, context)

    # "Let's do it" — the step's flow, pointed at the right person.
    _tour_mark_done(context)
    _begin(context, flows.ADD_PARENTS)
    await _prefill_own_father(update, context, flows.ADD_PARENTS)
    return await _ask(update, context)


async def on_tour_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.effective_message.text or ""
    step_id = context.user_data.get("tour_step") or ""
    kind = context.user_data.get("tour_kind")

    answer = understand.yes_no(typed)
    if answer is False or understand.is_skip(typed):
        _tour_mark_done(context)
        return await _offer_tour(update, context)
    if answer is True:
        if kind == "flow":
            _tour_mark_done(context)
            _begin(context, flows.ADD_PARENTS)
            await _prefill_own_father(update, context, flows.ADD_PARENTS)
            return await _ask(update, context)
        await _say(update, texts.TOUR_GO_ON)
        return TOUR

    # Not a yes or a no — hopefully names. Read them like any dictation,
    # hung off whoever this step is about.
    reading = dictation.parse(
        typed,
        default_role=_TOUR_DEFAULT_ROLE.get(step_id),
        subject_name=_subject_name_or_none(context),
        known_names=await _known_names(update, context),
    )
    if reading:
        _tour_mark_done(context)
        return await _absorb_dictation(update, context, reading)

    await _say(update, texts.NOT_UNDERSTOOD)
    return await _offer_tour(update, context)


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
    await _prefill_own_father(update, context, flow)
    return await _ask(update, context)


async def _prefill_own_father(update, context, flow) -> None:
    if flow.kind == submissions.ADD_PARENTS and _cursor(context) is None:
        # They told us their father's name when they signed up. Asking again
        # two minutes later reads as if the bot was not listening.
        who = await store.contributor_state(update.effective_user.id)
        known = who.get("father_given_name")
        if known:
            state = _state(context)
            state["answers"]["father_given"] = known
            state["index"] = 1


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
        return await _offer_tour(update, context)

    target = context.user_data.get("climb_to") or {}
    name = target.get("label", "them").split()[0]
    await _say(
        update,
        _climb_prompt(name, None, target.get("mode", "parents")),
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
        return await _offer_tour(update, context)

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
    if target.get("mode") == "family":
        # Their household: the menu, pointed at them, says exactly what can
        # be added — their spouse, their children.
        return await _show_menu(update, context)
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
        label = candidate["label"] or ""
        first = label.split()
        if first:
            names.add(first[0])
        if "(" in label:
            names.add(label.split("(", 1)[1].rstrip(")").strip())
        if candidate.get("person_id"):
            names.add(str(candidate["person_id"]))
    who = await store.contributor_state(update.effective_user.id)
    if who.get("father_given_name"):
        names.add(who["father_given_name"])
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
    target = name.casefold().lstrip("#")

    for payload in _basket(context):
        for entry in payload.get("people") or []:
            if target in (
                entry.get("given_name", "").casefold(),
                (entry.get("also_known_as") or "").casefold(),
            ):
                return {
                    "person_id": None,
                    "submission_id": None,
                    "draft_id": payload.get("_draft_id"),
                    "label": submissions.person_label(entry),
                }

    for candidate in await store.subject_candidates(update.effective_user.id):
        label = candidate["label"] or ""
        aka = (
            label.split("(", 1)[1].rstrip(")").strip().casefold()
            if "(" in label
            else ""
        )
        first = label.split()
        matches = (
            (first and first[0].casefold() == target)
            or (aka and aka == target)
            or (
                target.isdigit()
                and candidate.get("person_id") == int(target)
            )
        )
        if matches:
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

    def resolves_to_self(found: dict[str, Any]) -> bool:
        """"My name is Steven" — that's the contributor, not a stranger who
        happens to share their name. Telling somebody their own family
        "belongs to Steven Sukar rather than to you" reads as broken."""
        if who["person_id"] and found.get("person_id") == who["person_id"]:
            return True
        return bool(
            who["identify_submission_id"]
            and found.get("submission_id") == who["identify_submission_id"]
        )

    lead_extra = ""
    if reading.subject:
        # Best effort: the subject may be in the tree, in the basket — or
        # introduced by this very message, in which case the per-line anchors
        # place her relatives and there is nothing to bail out over.
        found = await _resolve_named_subject(update, context, reading.subject)
        if found is not None and resolves_to_self(found):
            _set_cursor(context, None)
        elif found is not None:
            _set_cursor(context, found)
            lead_extra = "\n\n" + texts.DICTATED_SUBJECT.format(name=found["label"])

    # Lines can name people other than whoever the bot was asking about:
    # "Hanna married Therese, kids are ...". Those hang off Hanna — who may
    # be in the tree, in the basket, or introduced two lines up in this very
    # message, so resolution happens as groups are stashed, not before.
    anchors: dict[str, dict[str, Any] | None] = {}
    for name in dict.fromkeys(m.about for m in reading.people if m.about):
        resolved = await _resolve_named_subject(update, context, name)
        if resolved is not None and resolves_to_self(resolved):
            resolved = dict(resolved)
            resolved["is_self"] = True
        anchors[name] = resolved
    unplaced: list[str] = []
    placed = sorted(
        found["label"]
        for found in anchors.values()
        if found is not None and not found.get("is_self")
    )
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
        """Whoever this mention hangs off: this message, the tree, or the cursor."""
        if not mention.about:
            return dict(about)
        found = anchors.get(mention.about)
        if found is not None and found.get("is_self"):
            # Their own name: these are the contributor's relatives, whatever
            # the cursor was doing.
            return submissions.subject(
                person_id=who["person_id"],
                submission_id=who["identify_submission_id"],
                label=who["label"] or "themselves",
            )
        # Introduced earlier in this same message?
        anchor = drafts_by_label.get(mention.about)
        if anchor is not None:
            subject = submissions.subject(label=mention.about)
            subject["draft_id"] = anchor
            return subject
        if found is None:
            if mention.about not in unplaced:
                unplaced.append(mention.about)
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

    async def shared_father(subject: dict[str, Any]) -> str | None:
        """What the subject's own record calls their father.

        A sibling shares it, which is what lets two contributors' unapproved
        claims about the same person find each other: a brother enters "my
        sibling Nawal", later Nawal enters "my siblings are..." — different
        subjects, same father."""
        if subject.get("draft_id"):
            return None
        if subject.get("person_id"):
            found = await store.person_father_given(subject["person_id"])
            if found:
                return found
        if _is_self(subject, who):
            return who.get("father_given_name")
        return None

    async def subject_as_father(subject: dict[str, Any]) -> str | None:
        """The subject's own name, when a man's children are being listed."""
        if subject.get("person_id"):
            return await store.person_given_if_male(subject["person_id"])
        return None

    stashed: list[dict[str, Any]] = []

    def stash(payload) -> str:
        draft_id = _draft_id(context)
        payload["_draft_id"] = draft_id
        _basket(context).append(payload)
        stashed.append(payload)
        # Everyone in this payload can now anchor later lines and later
        # messages: "Kalim's parents are..." right after naming Kalim.
        for entry in payload.get("people") or []:
            drafts_by_label.setdefault(entry["given_name"], draft_id)
            drafts_by_label.setdefault(submissions.person_label(entry), draft_id)
        return draft_id

    #: Payloads that only exist because a message used the contributor's own
    #: name in the third person. Usually that IS them — but half the family
    #: shares a handful of names, so it gets asked, not assumed.
    self_drafts: list[str] = []
    self_name: str | None = None

    def note_self_anchor(about_name: str | None, draft: str) -> None:
        nonlocal self_name
        if about_name and (anchors.get(about_name) or {}).get("is_self"):
            self_drafts.append(draft)
            self_name = self_name or about_name

    try:
        for owner, parents in parent_groups.items():
            subject = subject_for(parents[0])
            if subject is None:
                continue  # nobody to hang them off; already reported above
            parents_draft = stash(
                submissions.build(
                    submissions.ADD_PARENTS,
                    submitted_by=submitted_by,
                    about=subject,
                    people=[entry_of(m) for m in parents],
                    note=note,
                )
            )
            note_self_anchor(owner, parents_draft)

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
            entry = entry_of(mention)
            if not entry.get("father_given_name"):
                if kind == submissions.ADD_SIBLING:
                    entry["father_given_name"] = await shared_father(subject)
                elif kind == submissions.ADD_CHILD:
                    entry["father_given_name"] = await subject_as_father(subject)
            draft_id = stash(
                submissions.build(
                    kind,
                    submitted_by=submitted_by,
                    about=subject,
                    people=[entry],
                    note=note,
                )
            )
            drafts_by_label[mention.label()] = draft_id
            note_self_anchor(mention.about, draft_id)
    except ValueError as problem:
        log.warning("dictation rejected for %s: %s", user_id, problem)
        return await _show_menu(update, context, texts.ERROR)

    if unplaced:
        lead_extra += texts.DICTATED_UNKNOWN_PEOPLE.format(names=_join(unplaced))

    lead = (
        texts.DICTATED_ONE
        if len(reading) == 1
        else texts.DICTATED.format(count=len(reading))
    ) + lead_extra
    guesses = list(
        dict.fromkeys(reason for m in reading.people for reason in m.uncertain)
    )
    if guesses:
        template = (
            texts.DICTATED_UNSURE_ONE if len(guesses) == 1 else texts.DICTATED_UNSURE
        )
        lead += template.format(reasons=", and ".join(guesses))

    # "My siblings are Toufic and Nawal" — the word "siblings" never said who
    # is a brother and who is a sister. Silence here draws the tree wrong
    # quietly, so each nameless-sex person earns exactly one question.
    queue: list[dict[str, Any]] = []
    for payload in stashed:
        kind_of = {
            submissions.SIBLING: "sibling",
            submissions.CHILD: "child",
            submissions.SPOUSE: "partner",
        }
        about = payload.get("about") or {}
        owner = (
            None
            if _is_self(about, who)
            else (about.get("label") or "").split()[0] or None
        )
        for position, entry in enumerate(payload.get("people") or []):
            kind = kind_of.get(entry.get("role") or "")
            if kind and not entry.get("sex"):
                queue.append(
                    {
                        "type": "sex",
                        "draft_id": payload["_draft_id"],
                        "position": position,
                        "name": entry["given_name"],
                        "kind": kind,
                        "owner": owner,
                        # The family's own records often settle it — every
                        # Toufic on this tree is a man. Lead with the guess,
                        # but the tap decides; Hanna is a man here whatever
                        # an outside name list thinks.
                        "guess": await store.name_sex_hint(entry["given_name"]),
                    }
                )

    # And when a name looks like somebody already recorded — by an admin, or
    # by a cousin whose claim is still in the queue — the person who would
    # know is the one typing. Ask now, keep the answer as evidence; the merge
    # itself stays an admin's decision.
    asked = context.user_data.setdefault("asked_links", [])
    link_questions = 0
    for payload in stashed:
        if link_questions >= 3:
            break  # a party guest is not here for an interrogation
        for position, entry in enumerate(payload.get("people") or []):
            match = await store.find_link(user_id, entry, payload.get("about") or {})
            if match is None:
                continue
            key = f"{entry['given_name']}:{match['kind']}:{match['id']}"
            if key in asked:
                continue
            asked.append(key)
            label = match["label"]
            if match["kind"] == "person":
                label = f"{label} (#{match['id']})"
            else:
                label = f"{label}{texts.MATCH_PENDING_SUFFIX}"
            queue.append(
                {
                    "type": "link",
                    "draft_id": payload["_draft_id"],
                    "position": position,
                    "name": entry["given_name"],
                    "match_label": label,
                    "person_id": match["person_id"],
                    "submission_id": (
                        match["id"] if match["kind"] == "submission" else None
                    ),
                }
            )
            link_questions += 1
            break  # one question per payload is plenty

    if self_drafts and self_name:
        queue.insert(
            0, {"type": "self", "name": self_name, "drafts": self_drafts}
        )

    if queue:
        context.user_data["clarify_queue"] = queue
        context.user_data["clarify_lead"] = lead
        return await _ask_next_clarify(update, context)

    return await _show_review(update, context, lead)


# ===========================================================================
# Clarifying as it goes
# ===========================================================================
#
# Two kinds of question the bot asks in the middle of a conversation, both
# because the person typing is the one who knows:
#
#   * brother or sister — for people dictated without a sex. The flows
#     already ask; free text is the one door somebody can walk through
#     without ever saying.
#   * same person? — when a name looks like somebody already recorded. The
#     answer is kept as evidence on the submission; merging stays an
#     admin's decision.
# ===========================================================================

_SEX_BUTTONS = {
    "sibling": (texts.SIBLING_BROTHER, texts.SIBLING_SISTER),
    "child": (texts.CHILD_SON, texts.CHILD_DAUGHTER),
    "partner": (texts.SPOUSE_HUSBAND, texts.SPOUSE_WIFE),
}


async def _ask_next_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("clarify_queue") or []
    if not queue:
        context.user_data.pop("clarify_queue", None)
        lead = context.user_data.pop("clarify_lead", None)
        return await _show_review(update, context, lead)

    item = queue[0]
    if item["type"] == "self":
        await _say(
            update,
            texts.ask_meant_yourself(item["name"]),
            _kb(
                [
                    [_button(texts.MEANT_MYSELF, f"{CB_SELF}:yes")],
                    [
                        _button(
                            texts.MEANT_SOMEONE_ELSE.format(name=item["name"]),
                            f"{CB_SELF}:no",
                        )
                    ],
                ]
            ),
        )
        return CLARIFY

    if item["type"] == "link":
        await _say(
            update,
            texts.ask_same_person(item["name"], item["match_label"]),
            _kb(
                [
                    [_button(texts.SAME_PERSON, f"{CB_LINK}:yes")],
                    [_button(texts.DIFFERENT_PERSON, f"{CB_LINK}:no")],
                    [_button(texts.NOT_SURE, f"{CB_LINK}:skip")],
                ]
            ),
        )
        return CLARIFY

    male_label, female_label = _SEX_BUTTONS[item["kind"]]
    guess = item.get("guess")
    if guess in ("M", "F"):
        other = "F" if guess == "M" else "M"
        labels = {"M": male_label, "F": female_label}
        question = texts.ask_person_sex_guessed(
            item["name"], item["owner"], item["kind"], guess
        )
        rows = [
            [_button(texts.GUESS_YES.format(word=labels[guess].lower()), f"{CB_SEX}:{guess}")],
            [_button(texts.GUESS_NO.format(word=labels[other].lower()), f"{CB_SEX}:{other}")],
            [_button(texts.SKIP, f"{CB_SEX}:skip")],
        ]
    else:
        question = texts.ask_person_sex(item["name"], item["owner"], item["kind"])
        rows = [
            [_button(male_label, f"{CB_SEX}:M")],
            [_button(female_label, f"{CB_SEX}:F")],
            [_button(texts.SKIP, f"{CB_SEX}:skip")],
        ]
    await _say(update, question, _kb(rows))
    return CLARIFY


def _clarify_entry(
    context: ContextTypes.DEFAULT_TYPE, item: dict[str, Any]
) -> dict[str, Any] | None:
    for payload in _basket(context):
        if payload.get("_draft_id") == item["draft_id"]:
            people = payload.get("people") or []
            if item["position"] < len(people):
                return people[item["position"]]
    return None


def _record_sex(context: ContextTypes.DEFAULT_TYPE, sex: str | None) -> None:
    queue = context.user_data.get("clarify_queue") or []
    if not queue:
        return
    item = queue.pop(0)
    if sex is None:
        return
    entry = _clarify_entry(context, item)
    if entry is not None:
        entry["sex"] = sex


def _record_link(context: ContextTypes.DEFAULT_TYPE, answer: str) -> None:
    queue = context.user_data.get("clarify_queue") or []
    if not queue:
        return
    item = queue.pop(0)
    if answer == "skip":
        return
    entry = _clarify_entry(context, item)
    if entry is None:
        return
    if answer == "yes":
        if item.get("person_id"):
            entry["same_person_id"] = item["person_id"]
        elif item.get("submission_id"):
            entry["same_submission_id"] = item["submission_id"]
    elif answer == "no" and item.get("person_id"):
        # A denial is evidence too — it stops the admin merging on a hunch.
        entry["not_person_id"] = item["person_id"]


def _remove_drafts(context: ContextTypes.DEFAULT_TYPE, draft_ids: list[str]) -> None:
    """Drop these drafts and everything anchored on them, however deep."""
    basket = _basket(context)
    doomed = set(draft_ids)
    changed = True
    while changed:
        changed = False
        for payload in list(basket):
            anchored_on = (payload.get("about") or {}).get("draft_id")
            if payload.get("_draft_id") in doomed or anchored_on in doomed:
                doomed.add(payload.get("_draft_id"))
                basket.remove(payload)
                changed = True


async def on_self_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    answer = update.callback_query.data.split(":", 1)[1]
    queue = context.user_data.get("clarify_queue") or []
    if not queue or queue[0].get("type") != "self":
        return await _ask_next_clarify(update, context)
    item = queue.pop(0)
    if answer == "no":
        _remove_drafts(context, item["drafts"])
        await _say(update, texts.SELF_MISREAD.format(name=item["name"]))
    return await _ask_next_clarify(update, context)


async def on_sex_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    answer = update.callback_query.data.split(":", 1)[1]
    _record_sex(context, answer if answer in ("M", "F") else None)
    return await _ask_next_clarify(update, context)


async def on_link_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _record_link(context, update.callback_query.data.split(":", 1)[1])
    return await _ask_next_clarify(update, context)


async def on_clarify_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.effective_message.text or ""
    queue = context.user_data.get("clarify_queue") or []
    current = queue[0] if queue else {"type": "sex"}

    if current["type"] == "self":
        answer = understand.yes_no(typed)
        if answer is None:
            await _say(update, texts.SEX_NOT_UNDERSTOOD)
            return CLARIFY
        queue.pop(0)
        if answer is False:
            _remove_drafts(context, current["drafts"])
            await _say(update, texts.SELF_MISREAD.format(name=current["name"]))
        return await _ask_next_clarify(update, context)

    if current["type"] == "link":
        answer = understand.yes_no(typed)
        if answer is not None:
            _record_link(context, "yes" if answer else "no")
            return await _ask_next_clarify(update, context)
        if understand.is_skip(typed):
            _record_link(context, "skip")
            return await _ask_next_clarify(update, context)
        await _say(update, texts.SEX_NOT_UNDERSTOOD)
        return CLARIFY

    sex = understand.sex_word(typed)
    if sex is not None:
        _record_sex(context, sex)
        return await _ask_next_clarify(update, context)
    if understand.is_skip(typed):
        _record_sex(context, None)
        return await _ask_next_clarify(update, context)
    await _say(update, texts.SEX_NOT_UNDERSTOOD)
    return CLARIFY


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
    drawing = await _sketch_of(update, context)
    parts = [
        html_escape_module.escape(part) if part else ""
        for part in (lead, texts.REVIEW_HEADING, body)
        if part
    ]
    if drawing:
        parts.insert(1 if lead else 0, drawing)
    await _say(update, "\n\n".join(parts), _kb(rows), html=True)
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
    return await _offer_tour(update, context, texts.SAVED)


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
    parts = query.data.split(":")

    if parts[1] == "back":
        return await _start_correction(update, context)

    fixing = parts[1] == "go"
    submission_id = int(parts[-1])

    chosen = None
    for item in await store.recent_submissions(update.effective_user.id):
        if item["id"] == submission_id:
            chosen = item
            break
    if chosen is None:
        return await _show_menu(update, context, texts.ERROR)

    if not fixing:
        # The list truncates on a phone; a tap means "let me read it", not
        # "it's wrong". Show the whole story first — fixing is a second,
        # deliberate tap.
        status = texts.FIX_STATUS.get(chosen["status"], chosen["status"])
        body = "\n".join(chosen["details"]) + f"\n\nStatus: {status}"
        await _say(
            update,
            body,
            _kb(
                [
                    [_button(texts.FIX_THIS, f"{CB_FIX}:go:{submission_id}")],
                    [_button(texts.FIX_BACK, f"{CB_FIX}:back")],
                    [_button(texts.BACK_TO_MENU, CB_CANCEL)],
                ]
            ),
        )
        return PICK_SUBMISSION

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


def _asks_for_sketch(typed: str) -> bool:
    import re as _re
    return bool(
        _re.search(r"\b(sketch|tree|drawing|picture|chart|so far)\b", typed, _re.I)
    )


async def _show_sketch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    drawing = await _sketch_of(update, context)
    if not drawing:
        return await _show_menu(update, context, texts.SKETCH_EMPTY)
    await _say(
        update,
        html_escape_module.escape(texts.SKETCH_HEADING) + "\n" + drawing,
        _menu_keyboard(_subject_name_or_none(context), len(_basket(context))),
        html=True,
    )
    return MENU


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The menu is where people rest, so it is where they start typing."""
    typed = update.effective_message.text or ""
    if _asks_for_sketch(typed) and not dictation.looks_like_dictation(typed):
        return await _show_sketch(update, context)
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
