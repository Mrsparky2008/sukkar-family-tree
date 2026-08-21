"""
Reading a whole family out of one message.

The bot asks one question at a time, which suits someone who needs coaxing.
It is exactly wrong for the person who already knows — the aunt who can name
four generations and wants to get it out in one go. Told "that's longer than a
first name", she stops, and she was the one worth listening to.

So a message like

    Kalim's parents are Toufic and Cilene
    Kalim's sisters: Dibeh, Sonia and Saide, married to Jamil Tarabay

is read into people and relationships, and shown back as a list to correct.
The parsing is deliberately literal — no cleverness, no guessing at names it
cannot see. Anything it is unsure of is marked, and the contributor fixes it
on the review screen before a single row is written.

Nothing here talks to Telegram or the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import submissions
from bot import understand

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Words that announce whose relatives come next, and what they are. Order
#: matters: longer phrases are matched first so "grandfather" does not get
#: read as "father".
ROLE_WORDS: list[tuple[str, str, str | None]] = [
    # phrase, role, sex
    ("parents", submissions.FATHER, None),      # expanded below into two
    ("mother and father", submissions.FATHER, None),
    ("father and mother", submissions.FATHER, None),
    ("mum and dad", submissions.FATHER, None),
    ("mom and dad", submissions.FATHER, None),
    ("father", submissions.FATHER, "M"),
    ("dad", submissions.FATHER, "M"),
    ("baba", submissions.FATHER, "M"),
    ("mother", submissions.MOTHER, "F"),
    ("mum", submissions.MOTHER, "F"),
    ("mom", submissions.MOTHER, "F"),
    ("brothers", submissions.SIBLING, "M"),
    ("brother", submissions.SIBLING, "M"),
    ("sisters", submissions.SIBLING, "F"),
    ("sister", submissions.SIBLING, "F"),
    ("siblings", submissions.SIBLING, None),
    ("sons", submissions.CHILD, "M"),
    ("son", submissions.CHILD, "M"),
    ("daughters", submissions.CHILD, "F"),
    ("daughter", submissions.CHILD, "F"),
    ("children", submissions.CHILD, None),
    ("kids", submissions.CHILD, None),
    ("child", submissions.CHILD, None),
    ("wife", submissions.SPOUSE, "F"),
    ("husband", submissions.SPOUSE, "M"),
    ("spouse", submissions.SPOUSE, None),
]

#: Phrases meaning "this pair is a father and a mother", which is the only
#: place the parser infers sex from position rather than from a word.
PAIR_WORDS = {"parents", "mother and father", "father and mother", "mum and dad", "mom and dad"}

#: "Kalim's parents" names whose relatives these are — not a person to add.
POSSESSIVE = re.compile(r"[’']s$|s[’']$", re.IGNORECASE)

MARRIAGE = re.compile(
    r"\b(?:married\s+to|married|wed\s+to|wife\s+of|husband\s+of)\b", re.IGNORECASE
)

#: Words that are never part of a name.
FILLER = {
    "my", "our", "his", "her", "their", "its", "the", "a", "an", "of", "is",
    "are", "was", "were", "and", "then", "also", "plus", "with", "to", "who",
    "that", "this", "these", "those", "other", "others", "rest", "all", "both",
    "single", "unmarried", "married", "deceased", "late", "passed", "away",
    "girls", "boys", "men", "women", "guy", "guys", "one", "two", "three",
    "four", "five", "six", "seven", "eight", "nine", "ten", "name", "names",
    "called", "grandparents", "grandparent", "grandfather", "grandmother",
    "grandad", "granddad", "grandma", "nan", "nanna", "jiddo", "teta",
    "i", "we", "you", "he", "she", "they", "them", "me", "him", "us",
    "know", "think", "remember", "believe", "sure", "yes", "no", "ok",
    "sorry", "please", "thanks", "there", "here", "still", "already",
}

#: Notes worth keeping verbatim rather than discarding as filler.
NOTE_PATTERNS = [
    (re.compile(r"\b(single|unmarried|never married)\b", re.I), "said to be single"),
    (re.compile(r"\b(passed away|deceased|late|died)\b", re.I), "said to have passed away"),
]


@dataclass
class Reading:
    """What one message turned out to say."""

    people: list["Mention"] = field(default_factory=list)
    #: Remarks that could not be pinned to one person — "the other girls are
    #: single". Kept verbatim on the submission rather than guessed at, because
    #: attributing "the others" by rule gets it wrong and nobody notices.
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.people)

    def __len__(self) -> int:
        return len(self.people)


@dataclass
class Mention:
    """One person the parser found, and how sure it is."""

    role: str
    given_name: str
    family_name: str | None = None
    sex: str | None = None
    note: str | None = None
    spouse_of: str | None = None
    uncertain: list[str] = field(default_factory=list)

    def label(self) -> str:
        return (
            f"{self.given_name} {self.family_name}"
            if self.family_name
            else self.given_name
        )


# ---------------------------------------------------------------------------
# Deciding whether to even try
# ---------------------------------------------------------------------------


def looks_like_dictation(text: str) -> bool:
    """Whether this message is a list of relatives rather than one name.

    Kept deliberately narrow. A single name, even a long one, goes down the
    ordinary path; only text that names a relationship or clearly lists
    several people gets parsed.
    """
    cleaned = understand.tidy(text)
    if not cleaned:
        return False
    lowered = cleaned.casefold()

    if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word, _, _ in ROLE_WORDS):
        return True
    if MARRIAGE.search(lowered):
        return True
    # "Toufic, Cilene and Dibeh" — a list, without ever saying what they are.
    if "," in cleaned and re.search(r"\band\b", lowered):
        return True
    return len(cleaned.split()) > 6


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _is_subject(given: str, subject_name: str | None) -> bool:
    """Whether this name is just the person whose relatives are being listed.

    "Kalims sisters" has no apostrophe to go on, so the subject's own name is
    matched directly, with or without a trailing s.
    """
    if not subject_name:
        return False
    target = re.sub(r"[^a-z]", "", given.casefold())
    for candidate in subject_name.split():
        base = re.sub(r"[^a-z]", "", candidate.casefold())
        if base and target in (base, base + "s"):
            return True
    return False


def _strip_notes(chunk: str) -> tuple[str, str | None]:
    note = None
    for pattern, description in NOTE_PATTERNS:
        if pattern.search(chunk):
            note = description
            chunk = pattern.sub(" ", chunk)
    return chunk, note


def _names_in(
    chunk: str, subject_name: str | None = None
) -> list[tuple[str, str | None]]:
    """Pull (given, family) pairs out of a fragment like 'Dibeh Haddad, Sonia'."""
    found: list[tuple[str, str | None]] = []
    for piece in re.split(r",|\band\b|&|/|;|\+", chunk, flags=re.IGNORECASE):
        words = [
            word.strip(understand.EDGE_PUNCTUATION)
            for word in piece.split()
        ]
        words = [
            word
            for word in words
            if word
            and word.casefold() not in FILLER
            and not word.isdigit()
            and not POSSESSIVE.search(word)
            and not _is_subject(word, subject_name)
        ]
        if not words:
            continue
        given = words[0]
        family = " ".join(words[1:]) if len(words) > 1 else None
        found.append((_titled(given), _titled(family) if family else None))
    return found


def _titled(word: str) -> str:
    """Capitalise as a name would be, leaving mixed case alone.

    'saide' becomes 'Saide'; 'McKay' and 'AbouKhalil' are left as typed.
    """
    return word if any(c.isupper() for c in word[1:]) else word.capitalize()


def _role_at(lowered: str) -> tuple[str, str | None, bool, int, int] | None:
    """The first role word in this text: role, sex, is_pair, start, end."""
    best = None
    for phrase, role, sex in ROLE_WORDS:
        match = re.search(rf"\b{re.escape(phrase)}\b", lowered)
        if match and (best is None or match.start() < best[3]):
            best = (role, sex, phrase in PAIR_WORDS, match.start(), match.end())
    return best


def parse(
    text: str,
    default_role: str | None = None,
    subject_name: str | None = None,
) -> Reading:
    """Read a message into people. Never raises; returns an empty Reading."""
    mentions: list[Mention] = []
    loose_notes: list[str] = []
    current_role = default_role
    current_sex = None
    pair_expected = False

    for raw_line in re.split(r"[\n;]+", text):
        line = understand.tidy(raw_line)
        if not line:
            continue

        # A line can announce a role and then list people: "his sisters: A, B".
        found = _role_at(line.casefold())
        if found is not None:
            current_role, current_sex, pair_expected, start, end = found
            remainder = (line[:start] + " " + line[end:]).strip(" :,-—")
        else:
            remainder = line

        if current_role is None:
            continue

        # "Saide married to Jamil Tarabay" — two people, one relationship.
        spouse_names: list[tuple[str, str | None]] = []
        spouse_note = None
        if MARRIAGE.search(remainder):
            left, right = MARRIAGE.split(remainder, maxsplit=1)[0], MARRIAGE.split(
                remainder, maxsplit=1
            )[1]
            remainder = left
            right, spouse_note = _strip_notes(right)
            spouse_names = _names_in(right, subject_name)

        remainder, note = _strip_notes(remainder)
        if spouse_note:
            loose_notes.append(spouse_note)
        people = _names_in(remainder, subject_name)

        # BUG 2 was here: a remark like "the others are single" was pinned to
        # whoever happened to be on the line. It can only be trusted when the
        # line named exactly one person; otherwise it is kept loose and a human
        # decides who it meant.
        if note and len(people) != 1:
            loose_notes.append(note)
            note = None

        for index, (given, family) in enumerate(people):
            sex = current_sex
            uncertain = []
            role = current_role
            if pair_expected:
                # "parents" names two people and never says which is which.
                role = submissions.FATHER if index == 0 else submissions.MOTHER
                sex = "M" if index == 0 else "F"
                uncertain.append("guessed from the order they were listed")
            mentions.append(
                Mention(
                    role=role,
                    given_name=given,
                    family_name=family,
                    sex=sex,
                    note=note,
                    uncertain=uncertain,
                )
            )

        # The spouse attaches to the last person named before "married to".
        if spouse_names and mentions:
            partner_of = mentions[-1].label()
            for given, family in spouse_names:
                mentions.append(
                    Mention(
                        role=submissions.SPOUSE,
                        given_name=given,
                        family_name=family,
                        sex=_opposite(mentions[-1].sex),
                        spouse_of=partner_of,
                        uncertain=["sex assumed from their spouse"]
                        if _opposite(mentions[-1].sex)
                        else [],
                    )
                )

    return Reading(people=mentions, notes=[n for n in loose_notes if n])


def _opposite(sex: str | None) -> str | None:
    return {"M": "F", "F": "M"}.get(sex or "")
