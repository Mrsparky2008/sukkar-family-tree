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

import config
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

#: "Wadiha is the daughter of Najib and Saide" — the message names its own
#: subject, and the two people after "of" are her parents, not more children.
#: Read the other way round this sentence turns a woman into her own mother's
#: sibling, which nobody would ever spot in a list of forty names.
DESCENT = re.compile(
    r"^(?P<who>.{1,60}?)\s+(?:is|was|were)\s+(?:the\s+)?"
    r"(?P<what>daughter|son|child|wife|husband)\s+of\s+(?P<parents>.+)$",
    re.IGNORECASE,
)

#: "(John)" is what everyone actually calls him; "(she became a nun)" is a
#: story. Both arrive in brackets and they are not the same thing.
BRACKETED = re.compile(r"\(([^)]*)\)")

#: Words that make a bracketed phrase a remark rather than a nickname.
SENTENCE_WORDS = {
    "he", "she", "they", "it", "was", "were", "is", "are", "became", "become",
    "died", "passed", "away", "born", "lives", "lived", "went", "married",
    "never", "not", "also", "known", "still", "the", "a", "an", "his", "her",
}

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

#: "Sister Clemence" is what the family calls her, not a second woman. A title
#: followed by a name already in the list is another way of saying the same
#: person.
TITLES = {
    "sister", "sr", "brother", "br", "father", "fr", "mother", "saint", "st",
    "abouna", "sayedna", "monsignor", "bishop", "sheikh", "hajj", "hajji",
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
    #: A subject the message named for itself, when it did — "Wadiha is the
    #: daughter of...". Overrides whoever the bot was asking about.
    subject: str | None = None
    subject_sex: str | None = None
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
    nickname: str | None = None
    spouse_of: str | None = None
    uncertain: list[str] = field(default_factory=list)

    def label(self) -> str:
        name = (
            f"{self.given_name} {self.family_name}"
            if self.family_name
            else self.given_name
        )
        return f"{name} ({self.nickname})" if self.nickname else name

    def same_person_as(self, other: "Mention") -> bool:
        """Whether these are obviously the same name written twice."""
        if self.given_name.casefold() != other.given_name.casefold():
            return False
        if not self.family_name or not other.family_name:
            return True
        return self.family_name.casefold() == other.family_name.casefold()


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


def _take_brackets(chunk: str) -> tuple[str, list[str], list[str]]:
    """Pull bracketed text out, sorted into nicknames and remarks."""
    nicknames: list[str] = []
    remarks: list[str] = []

    for inner in BRACKETED.findall(chunk):
        content = inner.strip()
        if not content:
            continue
        words = content.split()
        reads_like_a_sentence = len(words) > 2 or any(
            word.casefold() in SENTENCE_WORDS for word in words
        )
        if reads_like_a_sentence:
            remarks.append(content)
        else:
            nicknames.append(content)

    return BRACKETED.sub(" ", chunk), nicknames, remarks


def _split_run_on(chunk: str, family_names: set[str]) -> list[str]:
    """Split "Khalil FAMILYNAME Hanna FAMILYNAME" where a comma was forgotten.

    A family name in the middle of a run of words is the end of one person
    and the start of the next. Only known family spellings count, so ordinary
    two-part given names are left alone.
    """
    words = chunk.split()
    if len(words) < 3:
        return [chunk]

    bare_words = [word.strip(understand.EDGE_PUNCTUATION).casefold() for word in words]
    # A surname repeated inside one run is the same signal as a known one:
    # "Khalil Haddad Hanna Haddad" is two men, whatever the family is called.
    repeated = {word for word in bare_words if bare_words.count(word) > 1 and word}

    boundaries = (family_names or set()) | repeated
    if not boundaries:
        return [chunk]

    pieces: list[str] = []
    current: list[str] = []
    for word, bare in zip(words, bare_words):
        current.append(word)
        if bare in boundaries and len(current) > 1:
            pieces.append(" ".join(current))
            current = []
    if current:
        pieces.append(" ".join(current))
    return pieces or [chunk]


def _strip_notes(chunk: str) -> tuple[str, str | None]:
    note = None
    for pattern, description in NOTE_PATTERNS:
        if pattern.search(chunk):
            note = description
            chunk = pattern.sub(" ", chunk)
    return chunk, note


@dataclass
class _Name:
    given: str
    family: str | None
    nickname: str | None
    remarks: list[str]


def _names_in(
    chunk: str,
    subject_name: str | None = None,
    family_names: set[str] | None = None,
) -> list[_Name]:
    """Pull people out of a fragment like 'Dibeh Haddad, Hanna (John) Haddad'."""
    found: list[_Name] = []
    for piece in re.split(r",|\band\b|&|/|;|\+", chunk, flags=re.IGNORECASE):
        for run in _split_run_on(piece, family_names or set()):
            run, nicknames, remarks = _take_brackets(run)
            words = [
                word.strip(understand.EDGE_PUNCTUATION) for word in run.split()
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
            found.append(
                _Name(
                    given=_titled(words[0]),
                    family=_titled(" ".join(words[1:])) if len(words) > 1 else None,
                    nickname=nicknames[0] if nicknames else None,
                    remarks=remarks,
                )
            )
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

    family_names = {
        variant.casefold() for variant in config.FAMILY_NAME_VARIANTS
    } | {config.FAMILY_NAME.casefold()}
    subject_override = None
    subject_sex = None

    for raw_line in re.split(r"[\n;]+", text):
        line = understand.tidy(raw_line)
        if not line:
            continue

        # "Wadiha is the daughter of Najib and Saide" names its own subject.
        descent = DESCENT.match(line)
        if descent is not None:
            named = understand.tidy(descent.group("who"))
            # "my mother Wadiha is the daughter of..." — the relationship word
            # tells us how the speaker knows her, not what she is called.
            for phrase, _role, _sex in ROLE_WORDS:
                named = re.sub(
                    rf"\b{re.escape(phrase)}\b", " ", named, flags=re.IGNORECASE
                )
            what = descent.group("what").casefold()
            people = _names_in(named, None, family_names)
            if people:
                subject_override = people[0].given
                subject_name = subject_override
                subject_sex = {"daughter": "F", "son": "M"}.get(what)
            if what in ("wife", "husband"):
                current_role, current_sex, pair_expected = (
                    submissions.SPOUSE,
                    "M" if what == "wife" else "F",
                    False,
                )
            else:
                current_role, current_sex, pair_expected = (
                    submissions.FATHER,
                    None,
                    True,
                )
            line = descent.group("parents")

        found = _role_at(line.casefold())
        if found is not None:
            current_role, current_sex, pair_expected, start, end = found
            remainder = (line[:start] + " " + line[end:]).strip(" :,-—")
        else:
            remainder = line

        if current_role is None:
            continue

        spouse_names: list[_Name] = []
        spouse_note = None
        if MARRIAGE.search(remainder):
            left, right = MARRIAGE.split(remainder, maxsplit=1)[0], MARRIAGE.split(
                remainder, maxsplit=1
            )[1]
            remainder = left
            right, spouse_note = _strip_notes(right)
            spouse_names = _names_in(right, subject_name, family_names)

        remainder, note = _strip_notes(remainder)
        if spouse_note:
            loose_notes.append(spouse_note)
        people = _names_in(remainder, subject_name, family_names)

        if note and len(people) != 1:
            loose_notes.append(note)
            note = None

        for index, person in enumerate(people):
            sex = current_sex
            uncertain = []
            role = current_role
            if pair_expected:
                role = submissions.FATHER if index == 0 else submissions.MOTHER
                sex = "M" if index == 0 else "F"
                uncertain.append(
                    "which of the two parents is the father, from the order "
                    "you listed them"
                )
            mention = Mention(
                role=role,
                given_name=person.given,
                family_name=person.family,
                sex=sex,
                nickname=person.nickname,
                note="; ".join(person.remarks) or note,
                uncertain=uncertain,
            )
            if not any(mention.same_person_as(seen) for seen in mentions):
                mentions.append(mention)

        if spouse_names and mentions:
            partner_of = mentions[-1].label()
            partner_sex = mentions[-1].sex
            for person in spouse_names:
                mention = Mention(
                    role=submissions.SPOUSE,
                    given_name=person.given,
                    family_name=person.family,
                    sex=_opposite(partner_sex),
                    nickname=person.nickname,
                    spouse_of=partner_of,
                    uncertain=["whether a husband or a wife was meant"]
                    if _opposite(partner_sex)
                    else [],
                )
                if not any(mention.same_person_as(seen) for seen in mentions):
                    mentions.append(mention)

    return Reading(
        people=_resolve_titles(mentions),
        notes=[n for n in loose_notes if n],
        subject=subject_override,
        subject_sex=subject_sex,
    )


def _resolve_titles(mentions: list[Mention]) -> list[Mention]:
    """Fold "Sister Clemence" back into the Clemence already on the list."""
    kept: list[Mention] = []
    for mention in mentions:
        if mention.given_name.casefold() not in TITLES:
            kept.append(mention)
            continue

        title, name = mention.given_name, mention.family_name
        if not name:
            continue  # a bare title names nobody

        existing = next(
            (m for m in kept if m.given_name.casefold() == name.casefold()), None
        )
        if existing is not None:
            remark = f"also called {title} {name}"
            existing.note = f"{existing.note}; {remark}" if existing.note else remark
            continue

        # A title in front of somebody we have not met: keep the person.
        mention.given_name, mention.family_name = name, None
        mention.note = f"called {title} {name}"
        kept.append(mention)
    return kept


def _opposite(sex: str | None) -> str | None:
    return {"M": "F", "F": "M"}.get(sex or "")
