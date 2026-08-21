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
ROLE_WORDS: list[tuple[str, str, str | None, bool]] = [
    # phrase, role, sex, plural
    ("parents", submissions.FATHER, None, True),
    ("mother and father", submissions.FATHER, None, False),
    ("father and mother", submissions.FATHER, None, False),
    ("mum and dad", submissions.FATHER, None, False),
    ("mom and dad", submissions.FATHER, None, False),
    ("father", submissions.FATHER, "M", False),
    ("dad", submissions.FATHER, "M", False),
    ("baba", submissions.FATHER, "M", False),
    ("mother", submissions.MOTHER, "F", False),
    ("mum", submissions.MOTHER, "F", False),
    ("mom", submissions.MOTHER, "F", False),
    ("brothers", submissions.SIBLING, "M", True),
    ("brother", submissions.SIBLING, "M", False),
    ("sisters", submissions.SIBLING, "F", True),
    ("sister", submissions.SIBLING, "F", False),
    ("siblings", submissions.SIBLING, None, True),
    ("sons", submissions.CHILD, "M", True),
    ("son", submissions.CHILD, "M", False),
    ("daughters", submissions.CHILD, "F", True),
    ("daughter", submissions.CHILD, "F", False),
    ("children", submissions.CHILD, None, True),
    ("kids", submissions.CHILD, None, True),
    ("child", submissions.CHILD, None, False),
    ("wife", submissions.SPOUSE, "F", False),
    ("husband", submissions.SPOUSE, "M", False),
    ("spouse", submissions.SPOUSE, None, False),
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

#: Words that make a bracketed phrase a remark rather than a also_known_as.
SENTENCE_WORDS = {
    "he", "she", "they", "it", "was", "were", "is", "are", "became", "become",
    "died", "passed", "away", "born", "lives", "lived", "went", "married",
    "never", "not", "also", "known", "still", "the", "a", "an", "his", "her",
}

MARRIAGE = re.compile(
    r"\b(?:married\s+to|married|wed\s+to|wife\s+of|husband\s+of)\b", re.IGNORECASE
)

#: "Khalil never married" is a fact about Khalil, not a marriage. Checked
#: before MARRIAGE, which would otherwise read it as one.
NEVER_MARRIED = re.compile(
    r"^(?P<who>.{1,60}?)\s+(?:never|did\s*n[o']?t|didn't|not)\s+"
    r"(?:get\s+)?marri(?:ed|age)?\b",
    re.IGNORECASE,
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
    #: Facts about people already named — "Khalil never married". Not new
    #: relatives; notes to attach to whoever they are about.
    remarks: list[tuple[str | None, str]] = field(default_factory=list)
    #: Other names for people already named — "Hanna (John) married to...".
    #: Hanna is not a new relative here, but John is new information about him
    #: and would otherwise fall on the floor.
    aliases: list[tuple[str, str]] = field(default_factory=list)
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
    also_known_as: str | None = None
    spouse_of: str | None = None
    #: Who this person hangs off, when the line named somebody other than
    #: whoever the bot was asking about.
    about: str | None = None
    uncertain: list[str] = field(default_factory=list)

    def label(self) -> str:
        name = (
            f"{self.given_name} {self.family_name}"
            if self.family_name
            else self.given_name
        )
        return f"{name} ({self.also_known_as})" if self.also_known_as else name

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

    if any(
        re.search(rf"\b{re.escape(word)}\b", lowered) for word, _, _, _ in ROLE_WORDS
    ):
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
    """Pull bracketed text out, sorted into also_known_ass and remarks."""
    also_known_ass: list[str] = []
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
            also_known_ass.append(content)

    return BRACKETED.sub(" ", chunk), also_known_ass, remarks


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
    also_known_as: str | None
    remarks: list[str]
    maybe_two: bool = False


def _titled(word: str) -> str:
    """Capitalise as a name would be, leaving mixed case alone.

    'saide' becomes 'Saide'; 'McKay' and 'AbouKhalil' are left as typed.
    """
    return word if any(c.isupper() for c in word[1:]) else word.capitalize()


def _role_at(
    lowered: str, start_from: int = 0
) -> tuple[str, str | None, bool, bool, int, int] | None:
    """The first role word at or after `start_from`.

    Returns role, sex, whether it names a pair of parents, whether it was
    plural, and where it sat.
    """
    best = None
    for phrase, role, sex, plural in ROLE_WORDS:
        match = re.search(rf"\b{re.escape(phrase)}\b", lowered[start_from:])
        if match is None:
            continue
        start, end = match.start() + start_from, match.end() + start_from
        if best is None or start < best[4]:
            best = (role, sex, phrase in PAIR_WORDS, plural, start, end)
    return best


def _names_in(
    chunk: str,
    subject_name: str | None = None,
    family_names: set[str] | None = None,
    plural: bool = False,
) -> list[_Name]:
    """Pull people out of a fragment like 'Dibeh Haddad, Hanna (John) Haddad'."""
    found: list[_Name] = []
    had_separators = bool(re.search(r",|\band\b|&|/|;|\+", chunk, re.IGNORECASE))

    for piece in re.split(r",|\band\b|&|/|;|\+", chunk, flags=re.IGNORECASE):
        for run in _split_run_on(piece, family_names or set()):
            run, others, remarks = _take_brackets(run)
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

            # "kids are Rohnda Jason Ronnie Jocelyn" — a plural role, no commas,
            # no surname anywhere. That is four children, not one child with a
            # very long name.
            if plural and not had_separators and len(words) > 2:
                for word in words:
                    found.append(
                        _Name(
                            given=_titled(word),
                            family=None,
                            also_known_as=None,
                            remarks=remarks,
                            maybe_two=False,
                        )
                    )
                    remarks = []
                continue

            found.append(
                _Name(
                    given=_titled(words[0]),
                    family=_titled(" ".join(words[1:])) if len(words) > 1 else None,
                    also_known_as=others[0] if others else None,
                    remarks=remarks,
                    # In a plural list where the others are single names, two
                    # words is as likely to be a missing comma as a surname.
                    maybe_two=plural and len(words) == 2,
                )
            )
            remarks = []
    return found


def _known_owner(prefix: str, known: set[str]) -> str | None:
    """Whether this fragment is just a possessive naming somebody we know."""
    words = [
        word.strip(understand.EDGE_PUNCTUATION)
        for word in prefix.split()
        if word.strip(understand.EDGE_PUNCTUATION).casefold() not in FILLER
    ]
    if len(words) != 1:
        return None

    bare = re.sub(r"[^a-z]", "", words[0].casefold())
    for candidate in (bare, bare.rstrip("s")):
        if candidate and candidate in known:
            return _titled(candidate)
    return None


def _first_name_in(
    text: str, subject_name: str | None, family_names: set[str]
) -> tuple[str, str | None] | None:
    """The first person named in a fragment, and any other name they go by.

    Returns (given name, also-known-as). Relationship words are stripped first
    so "my mother Wadiha" answers Wadiha.
    """
    cleaned = text
    for phrase, _role, _sex, _plural in ROLE_WORDS:
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.IGNORECASE)
    people = _names_in(cleaned, subject_name, family_names)
    if not people:
        return None
    return people[0].given, people[0].also_known_as


def parse(
    text: str,
    default_role: str | None = None,
    subject_name: str | None = None,
    known_names: set[str] | None = None,
) -> Reading:
    """Read a message into people. Never raises; returns an empty Reading.

    `known_names` are given names already in the tree or in the contributor's
    basket. A line beginning with one of them is naming whose relatives come
    next — "Kalims sisters are..." — rather than introducing somebody called
    Kalims.
    """
    known = {name.casefold() for name in (known_names or set())}
    mentions: list[Mention] = []
    loose_notes: list[str] = []
    remarks: list[tuple[str | None, str]] = []

    family_names = {
        variant.casefold() for variant in config.FAMILY_NAME_VARIANTS
    } | {config.FAMILY_NAME.casefold()}

    declared_subject: str | None = None
    declared_sex: str | None = None
    aliases: list[tuple[str, str]] = []
    #: Who the current line's people hang off. None means whoever the bot was
    #: already asking about.
    about: str | None = None

    current_role = default_role
    current_sex: str | None = None
    pair_expected = False
    plural = False

    def add(mention: Mention) -> None:
        mention.about = about
        if not any(mention.same_person_as(seen) for seen in mentions):
            mentions.append(mention)

    for raw_line in re.split(r"[\n;]+", text):
        line = understand.tidy(raw_line)
        if not line:
            continue

        # "Khalil never married" — a fact about a man already named,
        # not a new relative. Recorded as a remark against him.
        unmarried = NEVER_MARRIED.match(line)
        if unmarried is not None:
            named = _first_name_in(unmarried.group("who"), subject_name, family_names)
            if named:
                who, also = named
                about = who
                if also:
                    aliases.append((who, also))
                remarks.append((who, "never married"))
                continue

        # "Wadiha is the daughter of Najib and Saide" names its own subject.
        descent = DESCENT.match(line)
        if descent is not None:
            named = descent.group("who")
            what = descent.group("what").casefold()
            resolved = _first_name_in(named, None, family_names)
            if resolved:
                who, also = resolved
                if also:
                    aliases.append((who, also))
                declared_subject = declared_subject or who
                declared_sex = {"daughter": "F", "son": "M"}.get(what) or declared_sex
                subject_name = who
                about = None if who == declared_subject else who
            if what in ("wife", "husband"):
                current_role, current_sex, pair_expected, plural = (
                    submissions.SPOUSE,
                    "M" if what == "wife" else "F",
                    False,
                    False,
                )
            else:
                current_role, current_sex, pair_expected, plural = (
                    submissions.FATHER,
                    None,
                    True,
                    True,
                )
            line = descent.group("parents")

        # "Hanna (John) married to Therese Taouk kids are A B C"
        # "Hanna married to Therese" names a new subject. "Dibeh, Sonia and
        # Rima married to Jamil" is a list carrying on from an earlier role
        # word, and only the last of them married anyone — so the deciding
        # question is whether the left side names exactly one person.
        marriage_names_a_subject = False
        if MARRIAGE.search(line):
            before = MARRIAGE.split(line, maxsplit=1)[0]
            marriage_names_a_subject = (
                _role_at(before.casefold()) is None
                and len(_names_in(before, subject_name, family_names)) == 1
            )

        if marriage_names_a_subject:
            left, right = MARRIAGE.split(line, maxsplit=1)[:2]

            resolved = _first_name_in(left, subject_name, family_names)
            if resolved:
                about, also = resolved
                if also:
                    aliases.append((about, also))

            # The spouse's name ends where the next relationship word starts.
            following = _role_at(right.casefold())
            if following is not None:
                spouse_text, line = right[: following[4]], right[following[4] :]
            else:
                spouse_text, line = right, ""

            spouse_text, spouse_note = _strip_notes(spouse_text)
            if spouse_note:
                loose_notes.append(spouse_note)
            for person in _names_in(spouse_text, subject_name, family_names):
                add(
                    Mention(
                        role=submissions.SPOUSE,
                        given_name=person.given,
                        family_name=person.family,
                        also_known_as=person.also_known_as,
                        spouse_of=about,
                        note="; ".join(person.remarks) or None,
                    )
                )

            if not understand.tidy(line):
                continue

        found = _role_at(line.casefold())
        if found is not None:
            current_role, current_sex, pair_expected, plural, start, end = found
            prefix = line[:start].strip(" :,-—")
            # "Kalims sisters are Dibeh and Sonia" — the word in front of the
            # relationship is a possessive naming somebody we already know,
            # not a relative called Kalims.
            owner = _known_owner(prefix, known)
            if owner is not None:
                about = owner
                prefix = ""
            remainder = (prefix + " " + line[end:]).strip(" :,-—")
        else:
            remainder = line

        if current_role is None:
            continue

        inline_spouses: list[_Name] = []
        if MARRIAGE.search(remainder):
            left, right = MARRIAGE.split(remainder, maxsplit=1)[:2]
            remainder = left
            right, spouse_note = _strip_notes(right)
            if spouse_note:
                loose_notes.append(spouse_note)
            inline_spouses = _names_in(right, subject_name, family_names)

        remainder, note = _strip_notes(remainder)
        people = _names_in(remainder, subject_name, family_names, plural=plural)

        if note and len(people) != 1:
            loose_notes.append(note)
            note = None

        for index, person in enumerate(people):
            role, sex = current_role, current_sex
            uncertain = []
            if pair_expected:
                role = submissions.FATHER if index == 0 else submissions.MOTHER
                sex = "M" if index == 0 else "F"
                uncertain.append(
                    "which of the two parents is the father, from the order "
                    "you listed them"
                )
            if person.maybe_two:
                uncertain.append(
                    f"whether {person.given} {person.family} is one person or two"
                )
            add(
                Mention(
                    role=role,
                    given_name=person.given,
                    family_name=person.family,
                    sex=sex,
                    also_known_as=person.also_known_as,
                    note="; ".join(person.remarks) or note,
                    uncertain=uncertain,
                )
            )

        if inline_spouses and mentions:
            partner = mentions[-1]
            for person in inline_spouses:
                add(
                    Mention(
                        role=submissions.SPOUSE,
                        given_name=person.given,
                        family_name=person.family,
                        sex=_opposite(partner.sex),
                        also_known_as=person.also_known_as,
                        spouse_of=partner.label(),
                        uncertain=["whether a husband or a wife was meant"]
                        if _opposite(partner.sex)
                        else [],
                    )
                )

    return Reading(
        people=_resolve_titles(mentions),
        notes=[n for n in loose_notes if n],
        remarks=remarks,
        aliases=aliases,
        subject=declared_subject,
        subject_sex=declared_sex,
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
            continue

        existing = next(
            (m for m in kept if m.given_name.casefold() == name.casefold()), None
        )
        if existing is not None:
            remark = f"also called {title} {name}"
            existing.note = f"{existing.note}; {remark}" if existing.note else remark
            continue

        mention.given_name, mention.family_name = name, None
        mention.note = f"called {title} {name}"
        kept.append(mention)
    return kept


def _opposite(sex: str | None) -> str | None:
    return {"M": "F", "F": "M"}.get(sex or "")
