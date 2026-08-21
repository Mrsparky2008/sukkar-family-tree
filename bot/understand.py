"""
Reading what people actually type.

The bot asks with buttons, but people answer with words — "ok", "yes mate",
"Su K ar", "dunno". Every time it replies "use the buttons above" it costs
trust, and for the older relatives who hold most of the knowledge it is the
point where they put the phone down.

None of this guesses at names. It only recognises answers the bot already
offered, and only when the match is unambiguous.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

#: Trailing punctuation people add without thinking: "Steven." — which would
#: otherwise become part of the name and show up on the tree forever.
EDGE_PUNCTUATION = " \t\r\n.,;:!?\"'`´’‘“”()[]{}<>«»…"

#: What `tidy` strips from a whole message. Brackets are NOT in here: a
#: message ending "...Youssef (Joe)" would lose its closing bracket, and the
#: other-name would silently become part of the surname.
_MESSAGE_EDGES = " \t\r\n.,;:!?\"'`´’‘“”<>«»…"

_AFFIRMATIVE = {
    "y", "ye", "yes", "yep", "yeah", "yea", "yup", "ok", "okay", "okey",
    "sure", "please", "go", "go on", "goahead", "correct", "right", "true",
    "of course", "certainly", "definitely", "absolutely", "aye", "na3am",
    "naam", "eh", "mm", "yes please", "do it", "lets do it", "let's do it",
    "carry on", "continue", "next",
}

_NEGATIVE = {
    "n", "no", "nope", "nah", "naw", "not", "never", "no thanks", "no thank you",
    "stop", "done", "finished", "that's all", "thats all", "nothing", "la",
    "laa", "no more", "im done", "i'm done", "enough",
}

_SKIP = {
    "dunno", "dont know", "don't know", "do not know", "no idea", "not sure",
    "unsure", "unknown", "skip", "pass", "cant remember", "can't remember",
    "cannot remember", "dont remember", "don't remember", "no clue", "?",
    "not known", "unclear", "forgot", "i forget",
}


def tidy(text: str) -> str:
    """Collapse whitespace and drop punctuation clinging to either end.

    Inner punctuation survives, because plenty of real names carry it —
    Abou-Khalil, N'Diaye, O'Brien.
    """
    return " ".join(text.split()).strip(_MESSAGE_EDGES).strip()


def _key(text: str) -> str:
    """Letters and digits only, lowercased — 'Su K ar' and 'sukar' agree."""
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _key(a), _key(b)).ratio()


def yes_no(text: str) -> bool | None:
    """True for yes, False for no, None when it is neither."""
    cleaned = tidy(text).casefold()
    if not cleaned:
        return None
    if cleaned in _AFFIRMATIVE:
        return True
    if cleaned in _NEGATIVE:
        return False
    # "yes please do that" — take the lead word rather than demanding exactness.
    first = cleaned.split()[0]
    if first in _AFFIRMATIVE:
        return True
    if first in _NEGATIVE:
        return False
    return None


def is_skip(text: str) -> bool:
    """Whether this reads as "I don't know"."""
    cleaned = tidy(text).casefold()
    return bool(cleaned) and (
        cleaned in _SKIP or any(cleaned.startswith(phrase) for phrase in _SKIP)
    )


def match_choice(
    choices: Iterable[tuple[str, Any]], text: str, threshold: float = 0.85
) -> Any | None:
    """The value of the button this text was clearly aiming at, or None.

    Exact first, ignoring case, spacing and punctuation — so someone spelling
    their surname out loud, "Su K ar", lands on the Sukar button. Then a near
    match, but only when one option stands clearly ahead of the rest;
    ambiguity gets re-asked rather than guessed.
    """
    cleaned = tidy(text)
    if not cleaned:
        return None

    options = list(choices)
    target = _key(cleaned)
    if not target:
        return None

    for label, value in options:
        if target in (_key(label), _key(str(value))):
            return value

    scored = sorted(
        (
            (max(_similar(cleaned, label), _similar(cleaned, str(value))), value)
            for label, value in options
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not scored or scored[0][0] < threshold:
        return None
    # Two options equally close is not an answer, it is a coin toss.
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
        return None
    return scored[0][1]
