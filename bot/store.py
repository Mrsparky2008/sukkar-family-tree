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
        "sex": None,
        "telegram_user_id": telegram_user_id,
    }

    if state["person_id"] is not None:
        person = db.get_person(conn, state["person_id"])
        if person is not None:
            state["label"] = db.row_display_name(person)
            state["branch_id"] = person["branch_id"]
            state["father_given_name"] = person["father_given_name"]
            state["sex"] = person["sex"]
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
                state["sex"] = entries[0].get("sex")
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

        # The contributor was asked in the chat and answered. Their yes beats
        # a similarity score; their no unsets a guess that they rejected.
        if entry.get("same_person_id"):
            matched_person_id = entry["same_person_id"]
        elif entry.get("not_person_id") and matched_person_id == entry["not_person_id"]:
            matched_person_id = None

    submission_id = db.add_submission(
        conn, telegram_user_id, payload, matched_person_id=matched_person_id
    )
    return {"submission_id": submission_id, "matched": best}


async def queue(telegram_user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Put a submission in the review queue. Nothing else reaches the family data."""
    return await _run(_queue, telegram_user_id, payload)


def _find_link(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    entry: dict[str, Any],
    about: dict[str, Any],
) -> dict[str, Any] | None:
    """The one existing record this new mention most looks like, if any.

    This powers the bot asking "is this the same person as ...?" in the
    middle of the conversation — the person who typed the name is standing
    right there, and they know. Their answer becomes evidence; the merge
    itself stays an admin's decision."""
    contributor = db.get_contributor(conn, telegram_user_id)
    matches = db.corroborate(
        conn,
        entry["given_name"],
        role=entry.get("role"),
        family_name=entry.get("family_name"),
        subject_person_id=about.get("person_id"),
        subject_submission_id=about.get("submission_id"),
        father_given_name=entry.get("father_given_name"),
        branch_id=contributor["branch_id"] if contributor else None,
        threshold=config.FUZZY_MATCH_THRESHOLD,
    )
    for match in matches:
        # Their own earlier claims corroborating themselves is not news worth
        # a question; another person's claim, or an approved person, is.
        if match["kind"] == "submission" and match.get("submitted_by") == telegram_user_id:
            continue
        # A bare name in common is not a link — half the family answers to
        # the same given names. Only ask when something relational agrees:
        # the same father, the same subject, a shared relative.
        if not match.get("reasons"):
            continue
        return match
    return None


async def find_link(
    telegram_user_id: int, entry: dict[str, Any], about: dict[str, Any]
) -> dict[str, Any] | None:
    return await _run(_find_link, telegram_user_id, entry, about)


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


# ---------------------------------------------------------------------------
# The system reviewer — green flows, yellow talks, people referee
# ---------------------------------------------------------------------------
#
# Nothing here weakens the queue: every write still goes through the same
# review functions, and every decision is recorded with its reviewer. The
# system reviewer (id 0) approves exactly one shape of claim — an admitted
# contributor telling their own story, resembling nobody, contradicting
# nothing. Everything else waits for a person; and when a claim disagrees
# with something already recorded, the person behind the standing record is
# asked how confident they are, so the family sorts most of it out before
# any admin has to.

SYSTEM_REVIEWER = 0

_FIRST_HAND_KINDS = {
    submissions.ADD_PARENTS,
    submissions.ADD_SIBLING,
    submissions.ADD_SPOUSE,
    submissions.ADD_CHILD,
}


def _standing_conflict(conn: sqlite3.Connection, me: int, kind: str) -> str | None:
    """A single-slot fact already filled: the one shape of first-hand claim
    that can contradict the record outright rather than merely overlap."""
    person = db.get_person(conn, me)
    if person is None:
        return None
    if kind == submissions.ADD_PARENTS and (
        person["father_id"] or person["mother_id"]
    ):
        return "parents already recorded"
    if kind == submissions.ADD_SPOUSE and db.get_partners(conn, me):
        return "a spouse already recorded"
    return None


def _standing_author(conn: sqlite3.Connection, person_id: int) -> int | None:
    """Who told us about this person — the contributor, not the reviewer."""
    person = db.get_person(conn, person_id)
    if person is None or not person["from_submission_id"]:
        return None
    origin = db.get_submission(conn, person["from_submission_id"])
    return origin["telegram_user_id"] if origin else None


def _auto_review(
    conn: sqlite3.Connection, telegram_user_id: int, submission_id: int
) -> dict[str, Any]:
    import review

    row = db.get_submission(conn, submission_id)
    if row is None or row["status"] != "pending":
        return {"tier": "manual"}
    payload = db.submission_payload(row)
    kind = payload.get("kind")

    contributor = db.get_contributor(conn, telegram_user_id)
    me = contributor["linked_person_id"] if contributor else None
    about = payload.get("about") or {}
    # First-hand means: an admitted contributor (a person approved their
    # sign-up) speaking about their own immediate circle, from their own
    # account. Anything else is the ordinary queue.
    if kind not in _FIRST_HAND_KINDS or not me or about.get("person_id") != me:
        return {"tier": "manual"}

    overlaps = [
        match
        for match in review.evidence(conn, payload, submission_id)
        if match["score"] >= 0.5
    ]
    conflict = _standing_conflict(conn, me, kind)

    if not overlaps and not conflict:
        try:
            review.approve(conn, submission_id, SYSTEM_REVIEWER)
        except review.Blocked as blocked:
            return {"tier": "yellow", "why": str(blocked), "outreach": None}
        conn.execute(
            "UPDATE submissions SET review_note = ? WHERE id = ?",
            ("auto-approved: first-hand and uncontested", submission_id),
        )
        created = [
            {
                "person_id": person["id"],
                "label": db.row_display_name(person),
            }
            for person in db.people_from_submission(conn, submission_id)
        ]
        return {"tier": "green", "created": created}

    # Yellow: find the person behind the standing record and ask them.
    why = conflict or "resembles someone already recorded"
    outreach = None
    strongest = next(
        (match for match in overlaps if match["kind"] == "person"), None
    )
    author = None
    standing_label = None
    if strongest:
        author = _standing_author(conn, strongest["person_id"])
        standing_label = texts.tagged(
            strongest["label"], strongest["person_id"]
        )
    elif conflict:
        person = db.get_person(conn, me)
        for column in ("father_id", "mother_id"):
            if person[column]:
                author = _standing_author(conn, person[column])
                standing = db.get_person(conn, person[column])
                standing_label = texts.tagged(
                    db.row_display_name(standing), standing["id"]
                )
                break
    if author and author != telegram_user_id:
        question = texts.peer_check_question(
            asker=contributor["display_label"] or "A relative",
            claim=submissions.describe(payload),
            standing=standing_label or "what the tree shows",
        )
        check_id = db.add_peer_check(conn, submission_id, author, question)
        outreach = {
            "chat_id": author,
            "check_id": check_id,
            "question": question,
        }
    return {"tier": "yellow", "why": why, "outreach": outreach}


async def auto_review(telegram_user_id: int, submission_id: int) -> dict[str, Any]:
    """Triage a freshly queued submission. Explicitly called by the bot
    after queueing — queuing alone still never changes the family."""
    return await _run(_auto_review, telegram_user_id, submission_id)


def _answer_peer_check(
    conn: sqlite3.Connection, telegram_user_id: int, check_id: int, verdict: str
) -> bool:
    """Record an answer — only from the person who was actually asked."""
    check = db.get_peer_check(conn, check_id)
    if check is None or check["telegram_user_id"] != telegram_user_id:
        return False
    if verdict not in ("stands", "concedes", "unsure"):
        return False
    db.answer_peer_check(conn, check_id, verdict)
    return True


async def answer_peer_check(
    telegram_user_id: int, check_id: int, verdict: str
) -> bool:
    return await _run(_answer_peer_check, telegram_user_id, check_id, verdict)


# ---------------------------------------------------------------------------
# The review desk, from a phone
# ---------------------------------------------------------------------------
#
# The one sanctioned way the bot touches `people`: a super admin approving
# through the same review functions the CLI and the web interface use. The
# admin's Telegram ID is the credential, checked here at the boundary —
# every function below answers None/refusal for anyone else.


def _is_super_admin(telegram_user_id: int) -> bool:
    return telegram_user_id in config.SUPER_ADMIN_TELEGRAM_IDS


def _next_pending(
    conn: sqlite3.Connection, telegram_user_id: int, skip: list[int]
) -> dict[str, Any] | None:
    if not _is_super_admin(telegram_user_id):
        return None
    import review

    rows = [
        row
        for row in db.list_submissions(conn, status="pending")
        if row["id"] not in set(skip)
    ]
    if not rows:
        return {"remaining": 0}
    row = rows[0]
    payload = db.submission_payload(row)
    match = None
    for item in review.evidence(conn, payload, row["id"]):
        if item["kind"] == "person":
            match = {
                "person_id": item["person_id"],
                "label": item["label"],
                "score": item["score"],
                "reasons": list(item.get("reasons") or []),
            }
            break
    checks = []
    for check in db.peer_checks_for(conn, row["id"]):
        who = db.get_contributor(conn, check["telegram_user_id"])
        checks.append(
            {
                "who": (who["display_label"] if who else None)
                or f"user {check['telegram_user_id']}",
                "verdict": check["verdict"],
            }
        )
    return {
        "remaining": len(rows),
        "id": row["id"],
        "summary": submissions.describe(payload),
        "details": submissions.detail_lines(payload),
        "match": match,
        "checks": checks,
    }


async def next_pending(
    telegram_user_id: int, skip: list[int] | None = None
) -> dict[str, Any] | None:
    """The oldest pending submission, with its strongest person match."""
    return await _run(_next_pending, telegram_user_id, list(skip or []))


def _admin_resolve(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    submission_id: int,
    action: str,
    person_id: int | None,
) -> str | None:
    """Apply one review decision. Returns a problem sentence, or None."""
    if not _is_super_admin(telegram_user_id):
        return texts.REVIEW_NOT_ADMIN
    import review

    try:
        if action == "approve":
            review.approve(conn, submission_id, telegram_user_id)
        elif action == "force":
            review.approve(conn, submission_id, telegram_user_id, force=True)
        elif action == "merge":
            review.approve(
                conn, submission_id, telegram_user_id,
                use_person_id=int(person_id),
            )
        elif action == "reject":
            review.reject(
                conn, submission_id, telegram_user_id,
                note="rejected in the phone review",
            )
        else:
            return f"unknown review action {action!r}"
    except review.Blocked as blocked:
        return str(blocked)
    return None


async def admin_resolve(
    telegram_user_id: int,
    submission_id: int,
    action: str,
    person_id: int | None = None,
) -> str | None:
    return await _run(
        _admin_resolve, telegram_user_id, submission_id, action, person_id
    )


def _person_display(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = db.get_person(conn, person_id)
    return db.display_name_with_also_known_as(row) if row else None


async def person_display(person_id: int) -> str | None:
    """The full computed name, or None when no such number exists."""
    return await _run(_person_display, person_id)


def _people_named(conn: sqlite3.Connection, given: str) -> list[dict[str, Any]]:
    wanted = given.casefold()
    return [
        {
            "person_id": row["id"],
            "label": db.display_name_with_also_known_as(row),
        }
        for row in db.get_people(conn)
        if (row["given_name"] or "").casefold() == wanted
    ]


async def people_named(given: str) -> list[dict[str, Any]]:
    """Everyone on the tree answering to this first name — the same-name
    problem, measured. Two or more means only a number settles it."""
    return await _run(_people_named, given)


def _resolved_person_id(conn: sqlite3.Connection, ref: dict[str, Any]) -> int | None:
    import review

    try:
        return review.resolve_subject(
            conn,
            {"about": {
                "submission_id": ref.get("submission_id"),
                "label": ref.get("label"),
            }},
        )
    except review.Blocked:
        return None


async def resolved_person_id(ref: dict[str, Any]) -> int | None:
    """The person a cursor ref means, once its submission was approved.

    A contributor's saved session can point at a submission from before a
    round of approvals. The person now stands in the tree with parents and
    all — answering questions from the stale ref alone re-asks what the
    tree already knows."""
    if ref.get("person_id"):
        return int(ref["person_id"])
    if not ref.get("submission_id"):
        return None
    return await _run(_resolved_person_id, ref)


def _person_father_given(conn: sqlite3.Connection, person_id: int) -> str | None:
    for row in db.get_parents(conn, person_id):
        if row["sex"] == "M":
            return row["given_name"]
    return None


async def person_father_given(person_id: int) -> str | None:
    """The recorded father's given name, if the tree has one."""
    return await _run(_person_father_given, person_id)


def _person_sex(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = db.get_person(conn, person_id)
    return row["sex"] if row is not None else None


async def person_sex(person_id: int) -> str | None:
    return await _run(_person_sex, person_id)


def _person_given_if_male(conn: sqlite3.Connection, person_id: int) -> str | None:
    row = db.get_person(conn, person_id)
    return row["given_name"] if row is not None and row["sex"] == "M" else None


async def person_given_if_male(person_id: int) -> str | None:
    return await _run(_person_given_if_male, person_id)


def _own_parent_names(
    conn: sqlite3.Connection, telegram_user_id: int
) -> dict[str, str]:
    """The contributor's parents' given names, from wherever they were said.

    The tree if they are linked; otherwise their own queued claims — the
    guided tour runs before any admin has approved anything, so waiting for
    the tree would mean never knowing."""
    state = _contributor_state(conn, telegram_user_id)
    names: dict[str, str] = {}
    if state["father_given_name"]:
        names["father"] = state["father_given_name"]

    if state["person_id"] is not None:
        for row in db.get_parents(conn, state["person_id"]):
            key = "father" if row["sex"] == "M" else "mother"
            names.setdefault(key, row["given_name"])

    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=50):
        payload = db.submission_payload(row)
        if payload.get("kind") != submissions.ADD_PARENTS:
            continue
        about = payload.get("about") or {}
        is_self = (
            about.get("person_id") == state["person_id"]
            if state["person_id"] is not None
            else about.get("submission_id") == state["identify_submission_id"]
        )
        if not is_self:
            continue
        for entry in payload.get("people") or []:
            if entry.get("role") == submissions.FATHER:
                names.setdefault("father", entry["given_name"])
            elif entry.get("role") == submissions.MOTHER:
                names.setdefault("mother", entry["given_name"])
    return names


async def own_parent_names(telegram_user_id: int) -> dict[str, str]:
    return await _run(_own_parent_names, telegram_user_id)


def _name_sex_hint(conn: sqlite3.Connection, given_name: str) -> str | None:
    return db.name_sex_hint(conn, given_name)


async def name_sex_hint(given_name: str) -> str | None:
    """The family's own verdict on a name's sex, if it is unanimous."""
    return await _run(_name_sex_hint, given_name)


def _own_submission_exists(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    kind: str,
    about_person_id: int | None = None,
    about_submission_id: int | None = None,
    about_self: bool = False,
    about_label: str | None = None,
) -> bool:
    state = _contributor_state(conn, telegram_user_id)
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=100):
        payload = db.submission_payload(row)
        if payload.get("kind") != kind:
            continue
        about = payload.get("about") or {}
        if about_self:
            if state["person_id"] is not None:
                if about.get("person_id") == state["person_id"]:
                    return True
            elif (
                state["identify_submission_id"]
                and about.get("submission_id") == state["identify_submission_id"]
            ):
                return True
        else:
            if about_person_id and about.get("person_id") == about_person_id:
                return True
            if (
                about_submission_id
                and about.get("submission_id") == about_submission_id
                and _same_label(about.get("label"), about_label)
            ):
                return True
    return False


async def own_submission_exists(
    telegram_user_id: int,
    kind: str,
    about_person_id: int | None = None,
    about_submission_id: int | None = None,
    about_self: bool = False,
    about_label: str | None = None,
) -> bool:
    """Whether this contributor already sent a claim of this kind about this
    person — what lets the tour skip a step somebody has already done."""
    return await _run(
        _own_submission_exists,
        telegram_user_id,
        kind,
        about_person_id,
        about_submission_id,
        about_self,
        about_label,
    )


def _own_submission_payload(
    conn: sqlite3.Connection, telegram_user_id: int, submission_id: int
) -> dict | None:
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=200):
        if row["id"] == submission_id:
            return db.submission_payload(row)
    return None


async def own_submission_payload(
    telegram_user_id: int, submission_id: int
) -> dict | None:
    """The payload of one of this contributor's own submissions — what lets
    the menu keep knowing "Toufic — your brother" after his row is sent."""
    return await _run(_own_submission_payload, telegram_user_id, submission_id)


def _pending_payloads(conn: sqlite3.Connection, telegram_user_id: int) -> list[dict]:
    """The contributor's own still-pending claims, as sketch food — so their
    corner of the tree survives the moment their basket is sent."""
    out = []
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=200):
        if row["status"] != "pending":
            continue
        payload = db.submission_payload(row)
        if payload.get("kind") in (submissions.CORRECTION, submissions.IDENTIFY):
            continue
        out.append(payload)
    out.reverse()  # oldest first, the order they were told
    return out


async def pending_payloads(telegram_user_id: int) -> list[dict]:
    return await _run(_pending_payloads, telegram_user_id)


def _same_label(a: str | None, b: str | None) -> bool:
    """Whether two subject labels name the same person, by first name.

    A submission can hold two people — a father AND a mother — so matching
    on submission id alone once handed Wadiha's parents to Kalim."""
    if not a or not b:
        return True  # nothing to compare; the id match stands alone
    return a.split()[0].casefold() == b.split()[0].casefold()


def _queued_parent_names(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    about_person_id: int | None,
    about_submission_id: int | None,
    about_label: str | None,
) -> list[str]:
    names: list[str] = []
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=200):
        payload = db.submission_payload(row)
        if payload.get("kind") != submissions.ADD_PARENTS:
            continue
        about = payload.get("about") or {}
        matched = (
            about_person_id and about.get("person_id") == about_person_id
        ) or (
            about_submission_id
            and about.get("submission_id") == about_submission_id
            and _same_label(about.get("label"), about_label)
        )
        if matched:
            names += [e["given_name"] for e in payload.get("people") or []]
    return names


async def queued_parent_names(
    telegram_user_id: int,
    about_person_id: int | None = None,
    about_submission_id: int | None = None,
    about_label: str | None = None,
) -> list[str]:
    return await _run(
        _queued_parent_names,
        telegram_user_id,
        about_person_id,
        about_submission_id,
        about_label,
    )


def _count_contributions(conn: sqlite3.Connection, telegram_user_id: int) -> int:
    total = 0
    for row in db.list_submissions_by_user(conn, telegram_user_id, limit=200):
        payload = db.submission_payload(row)
        if payload.get("kind") == submissions.CORRECTION:
            continue
        total += len(payload.get("people") or [])
    return total


async def count_contributions(telegram_user_id: int) -> int:
    """How many people this contributor has named so far, queued or approved."""
    return await _run(_count_contributions, telegram_user_id)


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
                "details": submissions.detail_lines(payload),
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
