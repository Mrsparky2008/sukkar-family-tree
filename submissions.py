"""
The submission payload contract.

A submission is a *proposal*: someone's claim about who is related to whom. It
is built by the bot, stored as JSON in `submissions.payload_json`, read by the
admin interface, and only becomes a person if an admin says so.

This module sits above both the bot and the admin interface so that neither
owns the format. The bot builds payloads with `build_*`; the admin renders them
with `describe()`; both validate with `validate()`.

Constraint 4: nothing in here writes anything. It builds dictionaries.
Constraint 2: there are no date fields, and `validate()` rejects any that
appear — a payload from an older or hand-edited source cannot smuggle one in.
"""

from __future__ import annotations

from typing import Any

#: Bumped if the payload shape ever changes incompatibly. Stored on every
#: payload so a queue that has been sitting for a month is still readable.
SCHEMA_VERSION = 1

# --- kinds -----------------------------------------------------------------

IDENTIFY = "identify"
ADD_PARENTS = "add_parents"
ADD_SIBLING = "add_sibling"
ADD_SPOUSE = "add_spouse"
ADD_CHILD = "add_child"
CORRECTION = "correction"
NAME_FIX = "name_fix"

KINDS = frozenset(
    {IDENTIFY, ADD_PARENTS, ADD_SIBLING, ADD_SPOUSE, ADD_CHILD, CORRECTION,
     NAME_FIX}
)

#: The three name fields anyone may correct. Deliberately only names: a
#: correction can change how somebody is written, never who they are attached
#: to. Moving a person between parents is a decision with consequences for
#: everyone below them, and it stays with the review desk.
NAME_FIELDS: dict[str, str] = {
    "given_name": "first name",
    "family_name": "family name",
    "also_known_as": "the name they go by",
}

# --- roles: how a named person relates to the subject -----------------------

SELF = "self"
FATHER = "father"
MOTHER = "mother"
SIBLING = "sibling"
SPOUSE = "spouse"
CHILD = "child"

ROLES = frozenset({SELF, FATHER, MOTHER, SIBLING, SPOUSE, CHILD})

#: Which roles each kind is allowed to carry. Keeps an "add a sibling" payload
#: from arriving with a spouse in it.
ROLES_BY_KIND: dict[str, frozenset[str]] = {
    IDENTIFY: frozenset({SELF}),
    ADD_PARENTS: frozenset({FATHER, MOTHER}),
    ADD_SIBLING: frozenset({SIBLING}),
    ADD_SPOUSE: frozenset({SPOUSE}),
    ADD_CHILD: frozenset({CHILD}),
    CORRECTION: frozenset(),
    NAME_FIX: frozenset(),
}

#: Field names that must never appear on a person in a payload. Constraint 2 is
#: easiest to hold if the bot literally cannot express a date.
FORBIDDEN_FIELDS = frozenset(
    {"birth", "death", "born", "died", "dob", "age", "year", "date", "birthday"}
)

PERSON_FIELDS = frozenset(
    {
        "role",
        "given_name",
        "given_name_ar",
        "also_known_as",
        "sex",
        "family_name",
        "notes",
        # The submitter's claim about this person's father, kept as context for
        # the duplicate matcher and the reviewing admin. It is NOT a link — the
        # real father_id is decided at approval time, by a human.
        "father_given_name",
        # The contributor's own answer when the bot spotted a likely match and
        # asked "is this the same person?". Evidence for the admin, never a
        # decision: the merge still happens at approval time, by a human.
        "same_person_id",
        "same_submission_id",
        "not_person_id",
        # Which house (sub-lineage) of the family they say they belong to.
        # A key from the configured list, or whatever they typed when none
        # of them fitted — kept verbatim either way, because an unrecognised
        # house is information, not an error. It is deliberately NOT part of
        # any name: names are built from given + father's + family, and the
        # spellings machinery would fracture if a house name leaked in.
        "house",
    }
)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def person(
    role: str,
    given_name: str,
    *,
    sex: str | None = None,
    given_name_ar: str | None = None,
    also_known_as: str | None = None,
    family_name: str | None = None,
    father_given_name: str | None = None,
    notes: str | None = None,
    house: str | None = None,
) -> dict[str, Any]:
    """One named person inside a payload.

    `given_name` is the first name only, as everywhere else. `family_name` is
    set only when it differs from the family default — a woman who married in.
    `father_given_name` is context for the matcher, not a relationship: who the
    father actually is remains an admin's decision.
    """
    return {
        "role": role,
        "given_name": given_name.strip(),
        "given_name_ar": (given_name_ar or "").strip() or None,
        "also_known_as": (also_known_as or "").strip() or None,
        "sex": sex,
        "family_name": (family_name or "").strip() or None,
        "father_given_name": (father_given_name or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "house": (house or "").strip() or None,
    }


def subject(
    person_id: int | None = None,
    submission_id: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Who the new people are being attached to.

    Usually the contributor themselves. `person_id` when they are already in
    the tree; `submission_id` when their own identity is still in the queue, so
    an admin approving both in one sitting can see how they chain together.
    """
    return {
        "person_id": person_id,
        "submission_id": submission_id,
        "label": label,
    }


def submitter(
    telegram_user_id: int,
    person_id: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    return {
        "telegram_user_id": telegram_user_id,
        "person_id": person_id,
        "label": label,
    }


def build(
    kind: str,
    *,
    submitted_by: dict[str, Any],
    about: dict[str, Any] | None = None,
    people: list[dict[str, Any]] | None = None,
    note: str | None = None,
    source: str | None = None,
    target_submission_id: int | None = None,
    target_person_id: int | None = None,
    field: str | None = None,
    was: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Assemble a payload. Raises ValueError if it would be invalid.

    Validating on the way in means a malformed payload never reaches the queue,
    where an admin would have to puzzle over it.
    """
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "kind": kind,
        "about": about or subject(),
        "people": people or [],
        "note": (note or "").strip() or None,
        # Who the contributor got this from. Elders mostly will not use
        # Telegram, so their knowledge arrives second-hand through a son or a
        # niece. Recording that is not recoverable later.
        "source": (source or "").strip() or None,
        "submitted_by": submitted_by,
    }
    if kind == CORRECTION:
        payload["target_submission_id"] = target_submission_id
        payload["target_person_id"] = target_person_id
    if kind == NAME_FIX:
        payload["target_person_id"] = target_person_id
        payload["field"] = field
        # What it said when the correction was written. Kept so a fix that
        # has been overtaken by another can be spotted rather than silently
        # undoing somebody else's later word.
        payload["was"] = (was or "").strip() or None
        payload["now"] = (now or "").strip() or None

    problems = validate(payload)
    if problems:
        raise ValueError("; ".join(problems))
    return payload


# ---------------------------------------------------------------------------
# Validating
# ---------------------------------------------------------------------------


def validate(payload: dict[str, Any]) -> list[str]:
    """Everything wrong with a payload, as a list of readable problems."""
    problems: list[str] = []

    kind = payload.get("kind")
    if kind not in KINDS:
        return [f"unknown kind: {kind!r}"]

    if payload.get("version") != SCHEMA_VERSION:
        problems.append(
            f"payload version {payload.get('version')!r}, expected {SCHEMA_VERSION}"
        )

    submitted_by = payload.get("submitted_by") or {}
    if not isinstance(submitted_by.get("telegram_user_id"), int):
        problems.append("submitted_by.telegram_user_id is required")

    allowed_roles = ROLES_BY_KIND[kind]
    entries = payload.get("people") or []

    if kind == CORRECTION:
        if not payload.get("note"):
            problems.append("a correction must say what is wrong")
        if not payload.get("target_submission_id") and not payload.get(
            "target_person_id"
        ):
            problems.append("a correction must name what it is correcting")
    elif kind == NAME_FIX:
        if not payload.get("target_person_id"):
            problems.append("a name fix must say whose name it is")
        if payload.get("field") not in NAME_FIELDS:
            problems.append(f"not a name that can be fixed: {payload.get('field')!r}")
        if not payload.get("now"):
            problems.append("a name fix must say what it should say")
        # `now == was` is deliberately allowed through. It is not a malformed
        # payload, it is somebody typing the spelling that is already there —
        # usually because the line below them disagrees and this is the only
        # tool that says so. Refusing it here turns that into a dead end;
        # answering it needs the tree, so it is answered where the tree is.
    elif not entries:
        problems.append(f"{kind} needs at least one person")

    seen_roles: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"people[{index}]"

        role = entry.get("role")
        if role not in ROLES:
            problems.append(f"{where}: unknown role {role!r}")
        elif role not in allowed_roles:
            problems.append(f"{where}: role {role!r} is not valid for {kind}")
        elif role in (FATHER, MOTHER, SELF, SPOUSE) and role in seen_roles:
            problems.append(f"{where}: more than one {role} in one submission")
        if role is not None:
            seen_roles.add(role)

        given = (entry.get("given_name") or "").strip()
        if not given:
            problems.append(f"{where}: given_name is required")
        elif " " in given:
            problems.append(
                f"{where}: given_name is the first name only — got {given!r}. "
                f"Full names are computed from the father link."
            )

        if entry.get("sex") not in (None, "M", "F"):
            problems.append(f"{where}: sex must be 'M', 'F', or absent")

        for link_field in ("same_person_id", "same_submission_id", "not_person_id"):
            if entry.get(link_field) is not None and not isinstance(
                entry[link_field], int
            ):
                problems.append(f"{where}: {link_field} must be a number")

        for field in entry:
            if field.lower() in FORBIDDEN_FIELDS:
                problems.append(
                    f"{where}: {field!r} is not a field — this project stores "
                    f"no dates at all"
                )
        unknown = set(entry) - PERSON_FIELDS
        if unknown:
            problems.append(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    return problems


# ---------------------------------------------------------------------------
# Describing
# ---------------------------------------------------------------------------

#: How each role reads in a sentence, by the sex of the person named.
_ROLE_WORDS = {
    (FATHER, None): "father",
    (MOTHER, None): "mother",
    (SIBLING, "M"): "brother",
    (SIBLING, "F"): "sister",
    (SIBLING, None): "sibling",
    (SPOUSE, "M"): "husband",
    (SPOUSE, "F"): "wife",
    (SPOUSE, None): "spouse",
    (CHILD, "M"): "son",
    (CHILD, "F"): "daughter",
    (CHILD, None): "child",
    (SELF, None): "themselves",
}

_KIND_TITLES = {
    IDENTIFY: "Who I am",
    ADD_PARENTS: "Parents",
    ADD_SIBLING: "Sibling",
    ADD_SPOUSE: "Spouse",
    ADD_CHILD: "Child",
    CORRECTION: "Correction",
    NAME_FIX: "Name fix",
}


def role_word(role: str, sex: str | None = None) -> str:
    """The natural word for a role, given the sex if known."""
    return _ROLE_WORDS.get((role, sex)) or _ROLE_WORDS.get((role, None), role)


def person_label(entry: dict[str, Any]) -> str:
    """A person inside a payload, as text.

    Deliberately not a full display name: the father link that
    `db.display_name` needs does not exist until an admin approves this.
    """
    name = entry.get("given_name", "?")
    if entry.get("family_name"):
        name = f"{name} {entry['family_name']}"
    extras = [
        value
        for value in (entry.get("also_known_as"), entry.get("given_name_ar"))
        if value
    ]
    if extras:
        name = f"{name} ({', '.join(extras)})"
    return name


def kind_title(kind: str) -> str:
    return _KIND_TITLES.get(kind, kind)


def describe(payload: dict[str, Any]) -> str:
    """One line summarising a submission, for the bot and the admin queue."""
    kind = payload.get("kind", "?")
    about = payload.get("about") or {}
    who = about.get("label") or "an unknown person"

    if kind == CORRECTION:
        return f"Correction to {who}: {payload.get('note') or '(no detail)'}"

    if kind == NAME_FIX:
        what = NAME_FIELDS.get(payload.get("field"), "name")
        was = payload.get("was") or "(blank)"
        return f"{who} — {what}: {was} -> {payload.get('now')}"

    if kind == IDENTIFY:
        entries = payload.get("people") or []
        name = person_label(entries[0]) if entries else "?"
        return f"Says they are {name}"

    parts = []
    for entry in payload.get("people") or []:
        parts.append(f"{person_label(entry)} as {role_word(entry['role'], entry.get('sex'))}")

    if not parts:
        return kind_title(kind)
    return f"{', '.join(parts)} of {who}"


def detail_lines(payload: dict[str, Any]) -> list[str]:
    """A fuller breakdown, for the admin queue and the bot's confirm step.

    The summary already names everyone, so a person only earns their own line
    when they carry something it does not show. Otherwise the confirmation
    reads as the same sentence twice, which trains people to stop reading it.
    """
    lines = [describe(payload)]
    for entry in payload.get("people") or []:
        extras = []
        if entry.get("father_given_name"):
            extras.append(f"father said to be {entry['father_given_name']}")
        if entry.get("given_name_ar"):
            extras.append(entry["given_name_ar"])
        if entry.get("house"):
            extras.append(f"house: {entry['house']}")
        if entry.get("notes"):
            extras.append(entry["notes"])
        if entry.get("same_person_id"):
            extras.append(
                f"contributor confirmed: same as person #{entry['same_person_id']}"
            )
        if entry.get("same_submission_id"):
            extras.append(
                "contributor confirmed: same as pending submission "
                f"#{entry['same_submission_id']}"
            )
        if entry.get("not_person_id"):
            extras.append(
                f"contributor says NOT the same as person #{entry['not_person_id']}"
            )
        if extras:
            label = role_word(entry["role"], entry.get("sex")).title()
            lines.append(f"  {label}: {entry['given_name']} — {' — '.join(extras)}")
    if payload.get("note") and payload.get("kind") != CORRECTION:
        lines.append(f"  Note: {payload['note']}")
    if payload.get("source"):
        lines.append(f"  Told to them by: {payload['source']}")
    return lines


# ---------------------------------------------------------------------------
# Reading, for the admin interface
# ---------------------------------------------------------------------------


def people_of(payload: dict[str, Any], role: str | None = None) -> list[dict[str, Any]]:
    entries = payload.get("people") or []
    if role is None:
        return list(entries)
    return [entry for entry in entries if entry.get("role") == role]


def primary_person(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The person a duplicate check should run against.

    For everything except "add my parents" there is exactly one. For parents,
    the father is the one that matters: he is the patriline, and he is what the
    fuzzy matcher keys on.
    """
    entries = payload.get("people") or []
    if not entries:
        return None
    for role in (SELF, FATHER, SIBLING, SPOUSE, CHILD, MOTHER):
        for entry in entries:
            if entry.get("role") == role:
                return entry
    return entries[0]
