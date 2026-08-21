#!/usr/bin/env python3
"""
The review queue, from the command line.

Enough to run a pilot with a handful of volunteers before the Flask interface
(step 3) exists. Same rules as the real thing will have: nothing reaches the
family data without a person deciding, and nothing is ever merged or rejected
automatically.

    python review.py                     # the pending queue, with evidence
    python review.py --all               # every submission, any status
    python review.py --show 4            # one submission in full
    python review.py --approve 4         # accept it and create the people
    python review.py --merge 4 --into 12 # it is person 12, already in the tree
    python review.py --reject 4 --note "not a relative"
    python review.py --tree              # what the family looks like now
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

import config
import db
import submissions


class Blocked(Exception):
    """The submission cannot be applied yet, with a reason a human can act on."""


# ---------------------------------------------------------------------------
# Resolving what a submission hangs off
# ---------------------------------------------------------------------------


def resolve_subject(conn: sqlite3.Connection, payload: dict[str, Any]) -> int | None:
    """The person id this submission attaches to.

    A contributor can add their grandfather straight after their father,
    without waiting for anyone — so a submission may point at another
    submission. That one has to be approved first, and saying so plainly beats
    a foreign key error.
    """
    about = payload.get("about") or {}

    if about.get("person_id"):
        return int(about["person_id"])

    if about.get("submission_id"):
        parent = db.get_submission(conn, int(about["submission_id"]))
        if parent is None:
            raise Blocked(f"it hangs off submission #{about['submission_id']}, which is gone")
        if parent["resulting_person_id"]:
            return int(parent["resulting_person_id"])
        raise Blocked(
            f"approve #{parent['id']} first — this one hangs off it "
            f"({submissions.describe(db.submission_payload(parent))})"
        )

    return None


def _create(conn, entry: dict[str, Any], reviewed_by: int, **links) -> int:
    return db.create_person(
        conn,
        entry["given_name"],
        given_name_ar=entry.get("given_name_ar"),
        family_name=entry.get("family_name"),
        sex=entry.get("sex"),
        notes=entry.get("notes"),
        created_by_telegram_id=reviewed_by,
        **links,
    )


# ---------------------------------------------------------------------------
# Applying a decision
# ---------------------------------------------------------------------------


def approve(
    conn: sqlite3.Connection,
    submission_id: int,
    reviewed_by: int,
    use_person_id: int | None = None,
    force: bool = False,
) -> list[int]:
    """Turn a submission into people and links. Returns the ids created.

    `use_person_id` means "this is someone already in the tree" — the
    relationship still gets applied, but no new person is made. That is what
    merging is, and it is why merging cannot just mark a row and walk away:
    the claim ("Youssef is Khalil's father") is the part worth keeping.
    """
    row = db.get_submission(conn, submission_id)
    if row is None:
        raise Blocked(f"no submission #{submission_id}")
    if row["status"] != "pending":
        raise Blocked(f"#{submission_id} was already {row['status']}")

    payload = db.submission_payload(row)
    kind = payload.get("kind")

    if kind == submissions.CORRECTION:
        raise Blocked(
            "a correction is a note, not a change. Read it, edit the person "
            "yourself, then reject it with a note saying what you did."
        )

    # A near-certain match means this is probably a duplicate. Approving would
    # create a second Youssef and quietly move his son onto the copy. Refuse
    # and make the reviewer say which they meant.
    if use_person_id is None and not force:
        for match in evidence(conn, payload, submission_id):
            if match["kind"] == "person" and match["score"] >= 0.9:
                raise Blocked(
                    f"this looks like {match['label']} (#{match['person_id']}), "
                    f"already in the tree — {', '.join(match['reasons']) or 'same name'}.\n"
                    f"    If it is them:      --merge {submission_id} --into {match['person_id']}\n"
                    f"    If it is not:       --approve {submission_id} --anyway"
                )

    subject_id = resolve_subject(conn, payload)
    created: list[int] = []
    primary: int | None = None
    entries = payload.get("people") or []

    def place(entry, is_primary: bool) -> int:
        """Reuse the matched person for the primary role; create the rest."""
        if is_primary and use_person_id is not None:
            return use_person_id
        new_id = _create(conn, entry, reviewed_by)
        created.append(new_id)
        return new_id

    with db.transaction(conn):
        if kind == submissions.IDENTIFY:
            primary = place(entries[0], True)
            db.upsert_contributor(
                conn,
                payload["submitted_by"]["telegram_user_id"],
                linked_person_id=primary,
            )

        elif kind == submissions.ADD_PARENTS:
            if subject_id is None:
                raise Blocked("no subject — cannot tell whose parents these are")
            main = submissions.primary_person(payload)
            father_id = mother_id = None
            for entry in entries:
                person_id = place(entry, entry is main)
                if entry["role"] == submissions.FATHER:
                    father_id = person_id
                else:
                    mother_id = person_id
            links = {}
            if father_id:
                links["father_id"] = father_id
            if mother_id:
                links["mother_id"] = mother_id
            db.update_person(conn, subject_id, **links)
            if father_id and mother_id:
                db.create_union(conn, father_id, mother_id)
            primary = father_id or mother_id

        elif kind == submissions.ADD_SIBLING:
            if subject_id is None:
                raise Blocked("no subject — cannot tell whose sibling this is")
            subject = db.get_person(conn, subject_id)
            primary = place(entries[0], True)
            # A sibling shares parents. When merging, only fill gaps — never
            # overwrite what an admin already recorded.
            existing = db.get_person(conn, primary)
            links = {}
            if subject["father_id"] and not existing["father_id"]:
                links["father_id"] = subject["father_id"]
            if subject["mother_id"] and not existing["mother_id"]:
                links["mother_id"] = subject["mother_id"]
            if links:
                db.update_person(conn, primary, **links)

        elif kind == submissions.ADD_SPOUSE:
            if subject_id is None:
                raise Blocked("no subject — cannot tell whose spouse this is")
            primary = place(entries[0], True)
            db.create_union(conn, subject_id, primary)

        elif kind == submissions.ADD_CHILD:
            if subject_id is None:
                raise Blocked("no subject — cannot tell whose child this is")
            subject = db.get_person(conn, subject_id)
            primary = place(entries[0], True)
            column = "father_id" if subject["sex"] == "M" else "mother_id"
            existing = db.get_person(conn, primary)
            if not existing[column]:
                db.update_person(conn, primary, **{column: subject_id})

        else:
            raise Blocked(f"do not know how to apply {kind!r}")

        db.resolve_submission(
            conn,
            submission_id,
            "merged" if use_person_id is not None else "approved",
            reviewed_by,
            resulting_person_id=primary,
            review_note=(
                f"same as #{use_person_id}" if use_person_id is not None else None
            ),
        )
        db.assign_branches(conn)

    return created


def merge(conn, submission_id: int, person_id: int, reviewed_by: int) -> list[int]:
    """This submission describes someone already in the tree.

    The relationship it claims still gets applied — merging is not discarding.
    """
    if db.get_person(conn, person_id) is None:
        raise Blocked(f"no person #{person_id}")
    return approve(conn, submission_id, reviewed_by, use_person_id=person_id)


def reject(conn, submission_id: int, reviewed_by: int, note: str | None) -> None:
    if db.get_submission(conn, submission_id) is None:
        raise Blocked(f"no submission #{submission_id}")
    with db.transaction(conn):
        db.resolve_submission(conn, submission_id, "rejected", reviewed_by, review_note=note)


# ---------------------------------------------------------------------------
# Showing the queue
# ---------------------------------------------------------------------------


def evidence(
    conn: sqlite3.Connection, payload: dict[str, Any], submission_id: int | None = None
) -> list[dict[str, Any]]:
    """Who this might already be, and why.

    `submission_id` excludes the row being examined, so a submission is not
    offered as evidence for itself.
    """
    entry = submissions.primary_person(payload)
    if entry is None:
        return []
    about = payload.get("about") or {}
    return db.corroborate(
        conn,
        entry["given_name"],
        role=entry.get("role"),
        family_name=entry.get("family_name"),
        subject_person_id=about.get("person_id"),
        subject_submission_id=about.get("submission_id"),
        father_given_name=entry.get("father_given_name"),
        exclude_submission_id=submission_id,
    )


def show_queue(conn: sqlite3.Connection, status: str | None) -> None:
    rows = db.list_submissions(conn, status=status)
    if not rows:
        print("\nNothing waiting.\n")
        return

    print(f"\n{len(rows)} submission(s)\n")
    for row in rows:
        payload = db.submission_payload(row)
        who = payload.get("submitted_by") or {}
        label = who.get("label") or f"telegram {row['telegram_user_id']}"

        print(f"  #{row['id']}  {submissions.describe(payload)}")
        print(f"      from {label}   [{row['status']}]")
        if payload.get("source"):
            print(f"      told to them by {payload['source']}")

        for match in evidence(conn, payload, row['id'])[:3]:
            where = "in the tree" if match["kind"] == "person" else "also submitted"
            why = ", ".join(match["reasons"]) or "name only"
            marker = "  <-- likely" if match["score"] >= 0.9 else ""
            print(
                f"      ? {match['label']} ({where}, {match['score']}) — {why}{marker}"
            )
        print()


def show_one(conn: sqlite3.Connection, submission_id: int) -> None:
    row = db.get_submission(conn, submission_id)
    if row is None:
        print(f"no submission #{submission_id}", file=sys.stderr)
        return
    payload = db.submission_payload(row)
    print(f"\n#{row['id']}  [{row['status']}]  submitted {row['created_at']}")
    for line in submissions.detail_lines(payload):
        print(f"  {line}")
    print()
    for match in evidence(conn, payload, row['id']):
        why = ", ".join(match["reasons"]) or "name only"
        print(f"  ? {match['label']} ({match['kind']}, {match['score']}) — {why}")
    print()


def show_spellings(conn: sqlite3.Connection) -> None:
    """Where each spelling of the family name starts, and who inherited it.

    A spelling is not a branch. It is usually one clerk at one border on one
    day, and everything below that person carries it. Showing the divergence
    point makes that visible: a line spelled differently in Australia and in
    Lebanon is still one man's descendants.
    """
    people = {row["id"]: row for row in db.get_people(conn)}
    ours = {
        person_id: row
        for person_id, row in people.items()
        if db.canonical_family_name(row["family_name"], conn) == config.FAMILY_NAME
    }
    if not ours:
        print("\nNobody in the family yet.\n")
        return

    children: dict[int, list[int]] = {}
    for person_id, row in people.items():
        for parent in (row["father_id"], row["mother_id"]):
            if parent is not None:
                children.setdefault(parent, []).append(person_id)

    def descendants(person_id: int) -> int:
        seen: set[int] = set()
        frontier = [person_id]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, ()):
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
        return len(seen)

    spellings: dict[str, list] = {}
    for person_id, row in ours.items():
        spellings.setdefault(row["family_name"], []).append(person_id)

    print(f"\n{len(spellings)} spelling(s) of {config.FAMILY_NAME} in the tree\n")

    for spelling, members in sorted(
        spellings.items(), key=lambda item: -len(item[1])
    ):
        print(f"  {spelling}  —  {len(members)} people")

        for person_id in sorted(members):
            row = ours[person_id]
            father = people.get(row["father_id"]) if row["father_id"] else None
            if father is None:
                if row["father_id"] is None:
                    print(
                        f"      starts at {db.row_display_name(row)} "
                        f"(no father recorded)"
                    )
                continue
            if father["family_name"] != spelling:
                print(
                    f"      splits from {father['family_name']} at "
                    f"{db.row_display_name(row)} "
                    f"— {descendants(person_id)} descendant(s) carry it"
                )
                print(
                    f"        (father {db.row_display_name(father)} "
                    f"spells it {father['family_name']})"
                )
        print()

    if len(spellings) > 1:
        print(
            "  All of the above are one family. Spelling differences are\n"
            "  transliteration, not descent — matching folds them together.\n"
        )


def show_tree(conn: sqlite3.Connection) -> None:
    people = db.get_people(conn)
    print(f"\n{len(people)} people, {len(db.get_unions(conn))} unions\n")
    for person in people:
        parents = " & ".join(
            db.row_display_name(p) for p in db.get_parents(conn, person["id"])
        )
        line = f"  {db.row_display_name(person)}"
        if parents:
            line += f"   <- {parents}"
        print(line)
    print()


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review the submission queue.")
    parser.add_argument("--all", action="store_true", help="every status, not just pending")
    parser.add_argument("--show", type=int, metavar="ID")
    parser.add_argument("--approve", type=int, metavar="ID")
    parser.add_argument("--merge", type=int, metavar="ID")
    parser.add_argument("--into", type=int, metavar="PERSON_ID")
    parser.add_argument("--reject", type=int, metavar="ID")
    parser.add_argument("--note")
    parser.add_argument("--anyway", action="store_true",
                        help="approve even though it looks like a duplicate")
    parser.add_argument("--tree", action="store_true")
    parser.add_argument("--spellings", action="store_true",
                        help="where each spelling of the family name split off")
    parser.add_argument("--as", dest="reviewer", type=int, default=0,
                        help="your Telegram id, for the audit trail")
    parser.add_argument("--db")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.init_db(conn)

    try:
        if args.spellings:
            show_spellings(conn)
        elif args.tree:
            show_tree(conn)
        elif args.show is not None:
            show_one(conn, args.show)
        elif args.approve is not None:
            created = approve(conn, args.approve, args.reviewer, force=args.anyway)
            print(f"approved #{args.approve}; created {len(created)} person(s)")
            show_tree(conn)
        elif args.merge is not None:
            if args.into is None:
                print("--merge needs --into PERSON_ID", file=sys.stderr)
                return 1
            merge(conn, args.merge, args.into, args.reviewer)
            print(f"#{args.merge} recorded as person #{args.into}, relationship applied")
        elif args.reject is not None:
            reject(conn, args.reject, args.reviewer, args.note)
            print(f"rejected #{args.reject}")
        else:
            show_queue(conn, None if args.all else "pending")
    except Blocked as problem:
        print(f"\n  cannot do that: {problem}\n", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.stdout = None
        raise SystemExit(0)
