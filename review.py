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
    python review.py --house 96 --is atrash  # and everyone below him
    python review.py --fold 108 --into 105   # one person, entered twice
"""

from __future__ import annotations

import argparse
import pathlib
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

    # "Themselves", with nothing to point at. That is what a submission
    # looks like when somebody reached a flow before introducing themselves,
    # and it is recoverable: whoever sent it said it was about them, and the
    # contributor record says who they turned out to be. Repairing it here
    # rather than by hand means a whole chain hanging off one such entry
    # becomes approvable the moment they sign in.
    if not about.get("submission_id"):
        submitter = (payload.get("submitted_by") or {}).get("telegram_user_id")
        if submitter and (about.get("label") or "").strip().lower() == "themselves":
            contributor = db.get_contributor(conn, int(submitter))
            if contributor and contributor["linked_person_id"]:
                return int(contributor["linked_person_id"])

    if about.get("submission_id"):
        parent = db.get_submission(conn, int(about["submission_id"]))
        if parent is None:
            raise Blocked(f"it hangs off submission #{about['submission_id']}, which is gone")

        # "Add my parents" creates two people. An anchor meaning the mother
        # must not quietly resolve to the father, or her parents become his
        # and her siblings inherit the wrong pair.
        wanted = (about.get("label") or "").split()
        if wanted and parent["status"] in ("approved", "merged"):
            first = wanted[0].casefold()
            for person in db.people_from_submission(conn, parent["id"]):
                if (person["given_name"] or "").casefold() == first:
                    return int(person["id"])

            # A parents-pair merged into people already on the tree creates
            # nobody, so the label cannot match a created row — but the pair
            # still stands. Find the one the label means through the parent
            # submission's own subject: the mother is that subject's mother.
            # Without this, "Wadiha's parents" quietly resolves to her
            # husband, and her parents become his.
            parent_payload = db.submission_payload(parent)
            if parent_payload.get("kind") == submissions.ADD_PARENTS:
                for entry in parent_payload.get("people") or []:
                    if (entry.get("given_name") or "").casefold() != first:
                        continue
                    grand_subject = resolve_subject(conn, parent_payload)
                    if grand_subject is None:
                        break
                    subject_row = db.get_person(conn, grand_subject)
                    column = (
                        "father_id"
                        if entry.get("role") == submissions.FATHER
                        else "mother_id"
                    )
                    if subject_row and subject_row[column]:
                        return int(subject_row[column])
                    break

        if parent["resulting_person_id"]:
            return int(parent["resulting_person_id"])
        raise Blocked(
            f"approve #{parent['id']} first — this one hangs off it "
            f"({submissions.describe(db.submission_payload(parent))})"
        )

    return None


def _create(
    conn, entry: dict[str, Any], reviewed_by: int, submission_id: int | None = None,
    **links,
) -> int:
    return db.create_person(
        conn,
        entry["given_name"],
        from_submission_id=submission_id,
        given_name_ar=entry.get("given_name_ar"),
        also_known_as=entry.get("also_known_as"),
        family_name=entry.get("family_name"),
        sex=entry.get("sex"),
        notes=entry.get("notes"),
        created_by_telegram_id=reviewed_by,
        **links,
    )


# ---------------------------------------------------------------------------
# Applying a decision
# ---------------------------------------------------------------------------


def _would_loop(conn: sqlite3.Connection, person_id: int, parent_id: int) -> bool:
    """Whether making `parent_id` a parent of `person_id` closes a loop.

    Merging a man into his own father is the easy way to ask for one: two
    relatives of the same name, one generation apart, is the commonest shape
    in this family. The schema refuses a person who is their own father, but
    it cannot see a longer ring, and a rejected write reaches the reviewer as
    a crash rather than a reason.
    """
    seen: set[int] = set()
    frontier = [parent_id]
    while frontier:
        current = frontier.pop()
        if current == person_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        row = db.get_person(conn, current)
        if row is None:
            continue
        frontier += [p for p in (row["father_id"], row["mother_id"]) if p]
    return False


def _check_links(conn, person_id: int, links: dict[str, Any]) -> None:
    """Refuse a parent link that would make somebody their own ancestor."""
    for column, parent_id in links.items():
        if parent_id and _would_loop(conn, person_id, parent_id):
            who = db.get_person(conn, person_id)
            side = "father" if column == "father_id" else "mother"
            name = db.row_display_name(who) if who else f"#{person_id}"
            raise Blocked(
                f"that would make {name} their own {side} — or their own "
                f"ancestor further up. They cannot be the same person."
            )


def _fathers_name(conn: sqlite3.Connection, father: sqlite3.Row | None,
                  entry: dict[str, Any]) -> dict[str, Any]:
    """A person born into the family carries their father's family name.

    Not the tree's canonical spelling — his. He may be the one who spells it
    the way the Australian paperwork does, and his children's papers follow
    his, not the village's. And when a daughter's husband married in from
    another family entirely, their children are his family, not this one.

    Only ever fills a blank: whatever the contributor actually typed wins,
    and the stored payload is never touched — this is a copy.
    """
    if entry.get("family_name") or father is None or not father["family_name"]:
        return entry
    return {**entry, "family_name": father["family_name"]}


def _approval_note(edits, unknown_house: str | None) -> str | None:
    parts = []
    if edits:
        parts.append("approved with edits")
    if unknown_house:
        parts.append(f"house not configured: {unknown_house!r}")
    return "; ".join(parts) or None


def approve(
    conn: sqlite3.Connection,
    submission_id: int,
    reviewed_by: int,
    use_person_id: int | None = None,
    force: bool = False,
    edits: dict[int, dict[str, Any]] | None = None,
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

    # Edit-then-approve. The admin's fix is applied to what gets created,
    # never to the stored submission — the original stays exactly as sent,
    # which is the whole provenance model. `edits` maps a person's index in
    # the payload to replacement fields.
    if edits:
        editable = {"given_name", "family_name", "given_name_ar",
                    "also_known_as", "sex", "notes"}
        for index, fields in edits.items():
            if 0 <= index < len(payload.get("people") or []):
                for name, value in fields.items():
                    if name in editable:
                        payload["people"][index][name] = (
                            value.strip() if isinstance(value, str) else value
                        ) or None

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
    unknown_house: str | None = None
    entries = payload.get("people") or []

    def place(entry, is_primary: bool) -> int:
        """Reuse the matched person for the primary role; create the rest."""
        if is_primary and use_person_id is not None:
            return use_person_id
        new_id = _create(conn, entry, reviewed_by, submission_id)
        created.append(new_id)
        return new_id

    with db.transaction(conn):
        if kind == submissions.IDENTIFY:
            primary = place(entries[0], True)
            spelling = entries[0].get("family_name")
            if spelling:
                # They answered for themselves, which outranks any relative's
                # guess already on the record.
                db.set_family_name(conn, primary, spelling, self_reported=True)
            house = entries[0].get("house")
            if house and not db.declare_house(conn, primary, house):
                # A house nobody has configured. The claim stays on the
                # record either way; the admin either adds the house or
                # decides it is a spelling of one already listed.
                unknown_house = house
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
            # A parent link already on the record was put there by a reviewed
            # decision. Replacing it must be said out loud, not slipped in.
            subject = db.get_person(conn, subject_id)
            for column in list(links):
                standing = subject[column]
                if standing and standing != links[column] and not force:
                    side = "father" if column == "father_id" else "mother"
                    raise Blocked(
                        f"{db.row_display_name(subject)} already has a "
                        f"{side} on the tree (#{standing}) — approving this "
                        f"would replace them. If that is really the fix: "
                        f"--approve {submission_id} --anyway"
                    )
            _check_links(conn, subject_id, links)
            db.update_person(conn, subject_id, **links)
            if father_id and mother_id:
                db.create_union(conn, father_id, mother_id)
            primary = father_id or mother_id

        elif kind == submissions.ADD_SIBLING:
            if subject_id is None:
                raise Blocked("no subject — cannot tell whose sibling this is")
            subject = db.get_person(conn, subject_id)
            father = (
                db.get_person(conn, subject["father_id"])
                if subject["father_id"] else None
            )
            primary = place(_fathers_name(conn, father, entries[0]), True)
            # A sibling shares parents. When merging, only fill gaps — never
            # overwrite what an admin already recorded.
            existing = db.get_person(conn, primary)
            links = {}
            if subject["father_id"] and not existing["father_id"]:
                links["father_id"] = subject["father_id"]
            if subject["mother_id"] and not existing["mother_id"]:
                links["mother_id"] = subject["mother_id"]
            _check_links(conn, primary, links)
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
            # A child is told to us under one parent, but they have two, and
            # the tree already knows who the other is when the couple is on
            # the record. Linking only the parent we were told about left a
            # child with no father — and so no family name of his own, and no
            # house to inherit.
            partners = db.get_partners(conn, subject_id)
            other = partners[0] if len(partners) == 1 else None
            if subject["sex"] == "M":
                father = subject
            elif other is not None and other["sex"] == "M":
                father = other
            else:
                father = None

            primary = place(_fathers_name(conn, father, entries[0]), True)
            existing = db.get_person(conn, primary)
            links = {}
            column = "father_id" if subject["sex"] == "M" else "mother_id"
            if not existing[column]:
                links[column] = subject_id
            if other is not None:
                other_column = (
                    "father_id" if other["sex"] == "M" else "mother_id"
                )
                if other_column != column and not existing[other_column]:
                    links[other_column] = other["id"]
            _check_links(conn, primary, links)
            if links:
                db.update_person(conn, primary, **links)

        else:
            raise Blocked(f"do not know how to apply {kind!r}")

        db.resolve_submission(
            conn,
            submission_id,
            "merged" if use_person_id is not None else "approved",
            reviewed_by,
            resulting_person_id=primary,
            review_note=(
                f"same as #{use_person_id}" if use_person_id is not None
                else _approval_note(edits, unknown_house)
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


def show_spelling_claims(conn: sqlite3.Connection, person_id: int) -> None:
    claims = db.spelling_claims(conn, person_id)
    if len(claims) < 2:
        return
    print("  recorded as:")
    for claim in claims:
        mark = "  <- their own answer" if claim["self_reported"] else ""
        print(f"      {claim['spelling']} — by {claim['who']}{mark}")


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
    if row["resulting_person_id"]:
        show_spelling_claims(conn, row["resulting_person_id"])
    print()


def declare_house(
    conn: sqlite3.Connection, person_id: int, house_key: str
) -> int:
    """Pin somebody's house, and let everyone below on the father chain take it.

    A house is not something the system can work out on its own — the men it
    is named after are further back than anything recorded here. So it is
    stated once, as high up a line as somebody can vouch for, and inherited
    downward from there. Stating it on a grandfather rather than a grandson
    is the whole point: his brothers and their children get it too.

    Returns how many people changed house as a result.
    """
    person = db.get_person(conn, person_id)
    if person is None:
        raise Blocked(f"there is no person #{person_id}")
    if not db.declare_house(conn, person_id, house_key):
        known = ", ".join(row["key"] for row in db.get_branches(conn))
        raise Blocked(f"no house called {house_key!r} — there is: {known}")
    return db.assign_branches(conn)


def fold(conn: sqlite3.Connection, person_id: int, into_id: int) -> None:
    """One person entered twice: keep one number, retire the other.

    Only ever for a genuine double entry — the same person reaching the tree
    down two paths — and only while the spare copy is still bare. If anything
    has been hung off it since, that has to be moved first, deliberately, by
    somebody looking at both. Refusing here is cheaper than silently
    reparenting a child onto the wrong number.

    The retired number is not reused. It stays spent, so a reference to it
    written down anywhere never quietly means somebody else.
    """
    if person_id == into_id:
        raise Blocked("those are the same number")
    spare = db.get_person(conn, person_id)
    keeper = db.get_person(conn, into_id)
    if spare is None:
        raise Blocked(f"there is no person #{person_id}")
    if keeper is None:
        raise Blocked(f"there is no person #{into_id}")

    children = conn.execute(
        "SELECT id FROM people WHERE father_id = ?1 OR mother_id = ?1",
        (person_id,),
    ).fetchall()
    if children:
        listed = ", ".join(f"#{row['id']}" for row in children)
        raise Blocked(
            f"#{person_id} has children on it ({listed}) — move them to"
            f" #{into_id} first, then fold"
        )
    partners = conn.execute(
        "SELECT id FROM unions WHERE partner_a_id = ?1 OR partner_b_id = ?1",
        (person_id,),
    ).fetchall()
    if partners:
        raise Blocked(
            f"#{person_id} has a marriage recorded on it — move it to"
            f" #{into_id} first, then fold"
        )
    signed_in = conn.execute(
        "SELECT telegram_user_id FROM contributors WHERE linked_person_id = ?",
        (person_id,),
    ).fetchall()
    if signed_in:
        raise Blocked(
            f"somebody signed in as #{person_id} — point them at #{into_id}"
            " first, then fold"
        )
    if conn.execute(
        "SELECT 1 FROM branches WHERE founding_ancestor_id = ?", (person_id,)
    ).fetchone():
        raise Blocked(f"#{person_id} is a founding ancestor of a branch")

    note = f"same as #{into_id} — entered twice"
    conn.execute(
        "UPDATE submissions SET status = 'merged', resulting_person_id = ?1,"
        " review_note = COALESCE(review_note, ?2)"
        " WHERE resulting_person_id = ?3 OR matched_person_id = ?3",
        (into_id, note, person_id),
    )
    conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    conn.commit()


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


def find_people(conn: sqlite3.Connection, needle: str) -> None:
    """Look somebody up by any name they answer to, and get their number."""
    target = needle.strip().casefold()
    hits = []
    for row in db.get_people(conn):
        haystack = " ".join(
            part.casefold()
            for part in (
                row["given_name"], row["family_name"], row["also_known_as"],
                row["given_name_ar"], db.row_display_name(row),
            )
            if part
        )
        if target in haystack:
            hits.append(row)

    if not hits:
        print(f"\nNobody matching {needle!r}.\n")
        return

    print(f"\n{len(hits)} match(es) for {needle!r}\n")
    for row in hits:
        parents = db.get_parents(conn, row["id"])
        line = f"  #{row['id']:<4} {db.display_name_with_also_known_as(row)}"
        if parents:
            line += "   child of " + " & ".join(
                db.row_display_name(p) for p in parents
            )
        print(line)
    print()


def show_person(conn: sqlite3.Connection, person_id: int) -> None:
    """Drill down: who this person is, and who says so."""
    person = db.get_person(conn, person_id)
    if person is None:
        print(f"no person #{person_id}", file=sys.stderr)
        return

    print(f"\n#{person_id}  {db.display_name_with_spellings(conn, person)}")
    if person["also_known_as"]:
        print(f"  also known as {person['also_known_as']}")

    parents = db.get_parents(conn, person_id)
    if parents:
        print(f"  parents:  {', '.join(db.row_display_name(p) for p in parents)}")
    partners = db.get_partners(conn, person_id)
    if partners:
        print(f"  married:  {', '.join(db.row_display_name(p) for p in partners)}")
    children = db.get_children(conn, person_id)
    if children:
        print(f"  children: {', '.join(db.row_display_name(c) for c in children)}")
    if person["notes"]:
        print(f"  notes:    {person['notes']}")

    show_spelling_claims(conn, person_id)

    claims = db.provenance(conn, person_id)
    if not claims:
        print("\n  Nobody has submitted anything about them — seeded by hand.\n")
        return

    print("\n  Where this came from, closest teller first:")
    for claim in claims:
        print(
            f"      #{claim['submission_id']}  {submissions.describe(claim['claim'])}"
        )
        line = f"          told by {claim['told_by']} — {claim['closeness']}"
        if claim["heard_from"]:
            line += f", who heard it from {claim['heard_from']}"
        print(f"{line}  [{claim['status']}]")

    closest = claims[0]
    if len(claims) > 1 and closest["distance"] is not None:
        print(
            f"\n  If these disagree, {closest['told_by']} is the closest to them"
            f" — but that is a hint, not a ruling."
        )
    print()


def export_everything(conn: sqlite3.Connection, path: str) -> int:
    """Write the whole database out as plain JSON.

    Insurance against this project, not against the disk. If the code is
    rewritten, replaced, or abandoned, what relatives typed in survives in a
    format anything can read — including the submissions, which are the
    original words rather than the interpretation of them.
    """
    import json

    tables = ("branches", "people", "unions", "submissions", "contributors",
              "family_variants")
    dump = {
        "exported_from": str(config.DATABASE_PATH),
        "family": config.FAMILY_NAME,
        "village": config.VILLAGE,
    }
    total = 0
    for table in tables:
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        dump[table] = rows
        total += len(rows)

    pathlib.Path(path).write_text(
        json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return total


def show_tree(conn: sqlite3.Connection) -> None:
    people = db.get_people(conn)
    print(f"\n{len(people)} people, {len(db.get_unions(conn))} unions\n")
    for person in people:
        parents = " & ".join(
            db.row_display_name(p) for p in db.get_parents(conn, person["id"])
        )
        line = f"  {db.display_name_with_spellings(conn, person)}"
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
    parser.add_argument("--who", type=int, metavar="PERSON_ID",
                        help="drill down: who they are and who says so")
    parser.add_argument("--find", metavar="NAME",
                        help="look somebody up and get their number")
    parser.add_argument("--export", metavar="FILE",
                        help="write everything out as plain JSON")
    parser.add_argument("--spellings", action="store_true",
                        help="where each spelling of the family name split off")
    parser.add_argument("--house", type=int, metavar="PERSON_ID",
                        help="say which house somebody belongs to")
    parser.add_argument("--is", dest="house_key", metavar="HOUSE",
                        help="the house, for --house")
    parser.add_argument("--fold", type=int, metavar="PERSON_ID",
                        help="retire a double entry; needs --into")
    parser.add_argument("--as", dest="reviewer", type=int, default=0,
                        help="your Telegram id, for the audit trail")
    parser.add_argument("--db")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.init_db(conn)

    try:
        if args.find:
            find_people(conn, args.find)
        elif args.export:
            count = export_everything(conn, args.export)
            print(f"wrote {count} rows to {args.export}")
        elif args.who is not None:
            show_person(conn, args.who)
        elif args.spellings:
            show_spellings(conn)
        elif args.house is not None:
            if not args.house_key:
                print("--house needs --is HOUSE", file=sys.stderr)
                return 1
            moved = declare_house(conn, args.house, args.house_key)
            conn.commit()
            who = db.row_display_name(db.get_person(conn, args.house))
            print(f"#{args.house} {who} is {args.house_key}; {moved} follow")
        elif args.fold is not None:
            if args.into is None:
                print("--fold needs --into PERSON_ID", file=sys.stderr)
                return 1
            fold(conn, args.fold, args.into)
            print(f"#{args.fold} retired; they are #{args.into}")
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
