"""
The bot's database access.

Two jobs beyond wrapping `db`:

  * Keep SQLite off the event loop. Every call runs in a worker thread with its
    own short-lived connection, because a SQLite connection cannot be shared
    across threads and the bot is async throughout.

  * Enforce constraint 4 at the boundary. The functions here cover exactly what
    the bot may do: read people, read its own submissions, write to
    `submissions`, and record who a Telegram user is in `contributors`. There is
    deliberately no wrapper for `create_person`, `update_person`, or
    `create_union`. If a flow seems to need one, it needs `queue()` instead.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Callable

import config
import db
import submissions

from bot import texts


def _in_connection(work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    conn = db.connect()
    try:
        result = work(conn, *args, **kwargs)
        conn.commit()
        return result
    finally:
        conn.close()


async def _run(work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(_in_connection, work, *args, **kwargs)


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Who is this?
# ---------------------------------------------------------------------------


def _contributor_state(conn: sqlite3.Connection, telegram_user_id: int) -> dict[str, Any]:
    """Everything the bot needs to know about the person it is talking to."""
    row = db.get_contributor(conn, telegram_user_id)
    state: dict[str, Any] = {
        "known": row is not None,
        "person_id": row["linked_person_id"] if row else None,
        "branch_id": row["branch_id"] if row else None,
        "label": row["display_label"] if row else None,
        "identify_submission_id": None,
        "father_given_name": None,
    }

    if state["person_id"] is not None:
        person = db.get_person(conn, state["person_id"])
        if person is not None:
            state["label"] = db.row_display_name(person)
            state["branch_id"] = person["branch_id"]
            state["father_given_name"] = person["father_given_name"]
        return state

    # Not linked to anyone yet. Their own identity may still be in the queue,
    # which is what later submissions hang off.
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=50):
        payload = db.submission_payload(row)
        if payload.get("kind") == submissions.IDENTIFY:
            state["identify_submission_id"] = row["id"]
            entries = payload.get("people") or []
            if entries:
                if not state["label"]:
                    state["label"] = submissions.person_label(entries[0])
                state["father_given_name"] = entries[0].get("father_given_name")
            break

    return state


async def contributor_state(telegram_user_id: int) -> dict[str, Any]:
    return await _run(_contributor_state, telegram_user_id)


def _identity_candidates(
    conn: sqlite3.Connection, given_name: str, father_given_name: str | None
) -> list[dict[str, Any]]:
    matches = db.find_probable_matches(conn, given_name, father_given_name)
    return [
        {
            "person_id": row["id"],
            "label": db.row_display_name(row),
            "score": round(score, 3),
        }
        for row, score in matches[:5]
    ]


async def identity_candidates(
    given_name: str, father_given_name: str | None = None
) -> list[dict[str, Any]]:
    """People who might already be this contributor, best first."""
    return await _run(_identity_candidates, given_name, father_given_name)


def _link_contributor(
    conn: sqlite3.Connection, telegram_user_id: int, person_id: int
) -> dict[str, Any]:
    person = db.get_person(conn, person_id)
    label = db.row_display_name(person) if person else None
    db.upsert_contributor(
        conn,
        telegram_user_id,
        linked_person_id=person_id,
        branch_id=person["branch_id"] if person else None,
        display_label=label,
    )
    return {"person_id": person_id, "label": label}


async def link_contributor(telegram_user_id: int, person_id: int) -> dict[str, Any]:
    """Record that this Telegram user is this person.

    `contributors` is not `people`: this says who is holding the phone, not who
    exists in the family. Constraint 4 is untouched.
    """
    return await _run(_link_contributor, telegram_user_id, person_id)


def _remember_label(
    conn: sqlite3.Connection, telegram_user_id: int, label: str
) -> None:
    db.upsert_contributor(conn, telegram_user_id, display_label=label)


async def remember_label(telegram_user_id: int, label: str) -> None:
    """Note what an unlinked contributor calls themselves, so we can greet them."""
    await _run(_remember_label, telegram_user_id, label)


# ---------------------------------------------------------------------------
# The queue — the bot's only write path into the family data
# ---------------------------------------------------------------------------


def _queue(
    conn: sqlite3.Connection, telegram_user_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    problems = submissions.validate(payload)
    if problems:
        # A malformed payload reaching the queue would land on an admin's desk
        # as a puzzle. Refuse it here instead.
        raise ValueError("; ".join(problems))

    contributor = db.get_contributor(conn, telegram_user_id)
    branch_id = contributor["branch_id"] if contributor else None

    if payload.get("kind") == submissions.IDENTIFY:
        # They were answering "how do you spell your family name", so whatever
        # they said is a spelling of this family — including one nobody has
        # listed. Learning it now means the next relative who spells it that
        # way corroborates instead of looking like a stranger.
        for entry in payload.get("people") or []:
            db.record_family_variant(conn, entry.get("family_name"))

    matched_person_id = None
    best = None
    entry = submissions.primary_person(payload)
    about = payload.get("about") or {}
    if entry is not None:
        # Two brothers submitting the same third brother is the common case.
        # Corroboration weighs shared relatives, not just spelling, because
        # half the men in a branch answer to the same given name.
        matches = db.corroborate(
            conn,
            entry["given_name"],
            role=entry.get("role"),
            family_name=entry.get("family_name"),
            subject_person_id=about.get("person_id"),
            subject_submission_id=about.get("submission_id"),
            father_given_name=entry.get("father_given_name"),
            branch_id=branch_id,
        )
        if matches:
            best = matches[0]
            # Only a real person can be linked; a match against another
            # pending claim is evidence for the admin, not a foreign key.
            for match in matches:
                if match["person_id"] is not None:
                    matched_person_id = match["person_id"]
                    break

    submission_id = db.add_submission(
        conn, telegram_user_id, payload, matched_person_id=matched_person_id
    )
    return {"submission_id": submission_id, "matched": best}


async def queue(telegram_user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Put a submission in the review queue. Nothing else reaches the family data."""
    return await _run(_queue, telegram_user_id, payload)


def _subject_candidates(
    conn: sqlite3.Connection, telegram_user_id: int, limit: int = 60
) -> list[dict[str, Any]]:
    """Everyone this contributor could reasonably be asked about.

    Themselves, their immediate relatives, and everyone they have already
    named — including people still sitting in the queue, because a contributor
    should be able to add their grandfather straight after adding their father,
    without waiting days for an admin.
    """
    candidates: list[dict[str, Any]] = []
    seen_people: set[int] = set()
    seen_submissions: set[int] = set()

    def add_person(row, note=None):
        if row is None or row["id"] in seen_people:
            return
        seen_people.add(row["id"])
        candidates.append(
            {
                "person_id": row["id"],
                "submission_id": None,
                "label": db.display_name_with_also_known_as(row),
                "note": note,
            }
        )

    contributor = db.get_contributor(conn, telegram_user_id)
    own_id = contributor["linked_person_id"] if contributor else None

    def kin(row, kind: str) -> str:
        # The note doubles as the menu heading ("Toufic — your brother"),
        # which is what tells a contributor the bot knows who it is on.
        return "your " + texts.kin_word(kind, row["sex"])

    if own_id is not None:
        add_person(db.get_person(conn, own_id), note="you")
        parents = db.get_parents(conn, own_id)
        for row in parents:
            add_person(row, note=kin(row, "parent"))
        for row in db.get_siblings(conn, own_id):
            add_person(row, note=kin(row, "sibling"))
        for row in db.get_partners(conn, own_id):
            add_person(row, note=kin(row, "partner"))
        for row in db.get_children(conn, own_id):
            add_person(row, note=kin(row, "child"))
        # The wider circle the sketch shows must also be addressable:
        # "add kids to #19" points at an uncle, not a sibling.
        for parent in parents:
            for row in db.get_parents(conn, parent["id"]):
                add_person(row, note=kin(row, "grandparent"))
            for sibling in db.get_siblings(conn, parent["id"]):
                add_person(sibling, note=kin(sibling, "parent_sibling"))
                for partner in db.get_partners(conn, sibling["id"]):
                    add_person(partner, note=kin(partner, "parent_sibling"))

    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=50):
        payload = db.submission_payload(row)
        if payload.get("kind") == submissions.CORRECTION:
            continue
        if row["resulting_person_id"]:
            add_person(db.get_person(conn, row["resulting_person_id"]))
            continue
        if row["status"] != "pending" or row["id"] in seen_submissions:
            continue
        seen_submissions.add(row["id"])
        for entry in payload.get("people") or []:
            candidates.append(
                {
                    "person_id": None,
                    "submission_id": row["id"],
                    "label": submissions.person_label(entry),
                    "note": "waiting for review",
                }
            )

    return candidates[:limit]


async def subject_candidates(telegram_user_id: int) -> list[dict[str, Any]]:
    return await _run(_subject_candidates, telegram_user_id)


def _person_parents(conn: sqlite3.Connection, person_id: int) -> list[str]:
    return [row["given_name"] for row in db.get_parents(conn, person_id)]


async def person_parents(person_id: int) -> list[str]:
    """Given names of the recorded parents, for the menu to reason about."""
    return await _run(_person_parents, person_id)


def _corroboration(
    conn: sqlite3.Connection,
    given_name: str,
    role: str | None,
    subject_person_id: int | None,
    subject_submission_id: int | None,
    telegram_user_id: int,
) -> list[dict[str, Any]]:
    contributor = db.get_contributor(conn, telegram_user_id)
    return db.corroborate(
        conn,
        given_name,
        role=role,
        subject_person_id=subject_person_id,
        subject_submission_id=subject_submission_id,
        branch_id=contributor["branch_id"] if contributor else None,
    )


def _sketchable(row) -> dict:
    """A person from the tree, shaped like a payload entry for the sketch.

    The display name is split so the sketch shows the full computed name — the
    father middle name doing its disambiguation work — rather than a brother
    and a grandfather who share a given name collapsing into one string.
    """
    parts = db.row_display_name(row).split(" ", 1)
    return {
        "given_name": parts[0],
        "family_name": parts[1] if len(parts) > 1 else None,
        "also_known_as": row["also_known_as"],
        "role": None,
    }


def _approved_payloads(conn, telegram_user_id: int) -> list[dict]:
    """The contributor's approved corner of the tree, as sketch food.

    Basket entries disappear once an admin approves them; without this the
    sketch would go blank at exactly the moment the data became real.
    """
    contributor = db.get_contributor(conn, telegram_user_id)
    if contributor is None or contributor["linked_person_id"] is None:
        return []
    me = db.get_person(conn, contributor["linked_person_id"])
    if me is None:
        return []

    payloads: list[dict] = []

    def about_label(row) -> str:
        return db.row_display_name(row)

    def family_of(row) -> None:
        """Parents of `row`, and `row`'s siblings, spouse and children."""
        label = about_label(row)
        parents = db.get_parents(conn, row["id"])
        if parents:
            people = []
            for parent in parents:
                entry = _sketchable(parent)
                entry["role"] = (
                    submissions.FATHER if parent["sex"] == "M" else submissions.MOTHER
                )
                people.append(entry)
            payloads.append(
                {"kind": submissions.ADD_PARENTS,
                 "about": {"label": label}, "people": people}
            )
        for sibling in db.get_siblings(conn, row["id"]):
            entry = _sketchable(sibling)
            entry["role"] = submissions.SIBLING
            payloads.append(
                {"kind": submissions.ADD_SIBLING,
                 "about": {"label": label}, "people": [entry]}
            )
            for partner in db.get_partners(conn, sibling["id"]):
                spouse = _sketchable(partner)
                spouse["role"] = submissions.SPOUSE
                payloads.append(
                    {"kind": submissions.ADD_SPOUSE,
                     "about": {"label": about_label(sibling)},
                     "people": [spouse]}
                )
        for partner in db.get_partners(conn, row["id"]):
            entry = _sketchable(partner)
            entry["role"] = submissions.SPOUSE
            payloads.append(
                {"kind": submissions.ADD_SPOUSE,
                 "about": {"label": label}, "people": [entry]}
            )
        for child in db.get_children(conn, row["id"]):
            entry = _sketchable(child)
            entry["role"] = submissions.CHILD
            payloads.append(
                {"kind": submissions.ADD_CHILD,
                 "about": {"label": label}, "people": [entry]}
            )

    family_of(me)
    for parent in db.get_parents(conn, me["id"]):
        family_of(parent)
    return payloads


async def approved_payloads(telegram_user_id: int) -> list[dict]:
    return await _run(_approved_payloads, telegram_user_id)


def _recent_submissions(
    conn: sqlite3.Connection, telegram_user_id: int, limit: int
) -> list[dict[str, Any]]:
    out = []
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=limit):
        payload = db.submission_payload(row)
        out.append(
            {
                "id": row["id"],
                "status": row["status"],
                "summary": submissions.describe(payload),
                "kind": payload.get("kind"),
                "person_id": row["resulting_person_id"],
            }
        )
    return out


async def recent_submissions(
    telegram_user_id: int, limit: int | None = None
) -> list[dict[str, Any]]:
    """A contributor's own submissions, for the "fix something" flow."""
    return await _run(
        _recent_submissions,
        telegram_user_id,
        limit or config.FIXABLE_SUBMISSION_LIMIT,
    )
