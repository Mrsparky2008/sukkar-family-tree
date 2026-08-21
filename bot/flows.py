"""
The conversation flows, as data.

Each menu option is a `Flow`: a list of questions, and a function turning the
answers into a submission payload. Keeping them declarative means the handlers
in `handlers.py` stay one generic ask-confirm-advance loop instead of twenty
near-identical states, and it means the flows can be tested without Telegram.

Two rules from the spec live here:

  * One question per message.
  * Every typed answer is confirmed before it is used, and "no" re-asks.

Nothing in this module touches the database or Telegram. It is answers in,
payload out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import config
import submissions
from bot import texts, understand

#: Step types.
NAME = "name"  # a single given name: validated, then confirmed
TEXT = "text"  # free text: confirmed, but spaces allowed
CHOICE = "choice"  # inline buttons: no confirmation needed

MAX_NAME_LENGTH = 40
MAX_TEXT_LENGTH = 400


class FlowError(Exception):
    """A problem worth showing the contributor, in words they can act on."""


@dataclass(frozen=True)
class Step:
    """One question."""

    id: str
    type: str
    prompt: str | Callable[[dict[str, Any]], str]
    choices: list[tuple[str, str]] = field(default_factory=list)
    #: Offer an "I don't know" button, and accept no answer.
    optional: bool = False
    #: Only ask this if the named earlier answer was given.
    only_if: str | None = None
    #: ...and, when set, only if that answer equals this value.
    only_if_value: Any = None

    def text(self, answers: dict[str, Any]) -> str:
        return self.prompt(answers) if callable(self.prompt) else self.prompt

    def applies(self, answers: dict[str, Any]) -> bool:
        if self.only_if is None:
            return True
        answer = answers.get(self.only_if)
        if self.only_if_value is not None:
            return answer == self.only_if_value
        return answer is not None


@dataclass(frozen=True)
class Flow:
    kind: str
    steps: list[Step]
    build: Callable[..., dict[str, Any]]


# ---------------------------------------------------------------------------
# Answer cleaning
# ---------------------------------------------------------------------------


def clean_name(raw: str) -> str:
    """Validate a typed given name, or raise FlowError explaining why not.

    The single-word rule is the one that matters. If someone types their whole
    name into a first-name box, the computed-name rule appends the father and
    family name to a string that already contains them, and the duplicate goes
    unnoticed for months.
    """
    value = understand.tidy(raw)
    if not value:
        raise FlowError(texts.NAME_EMPTY)
    if len(value) > MAX_NAME_LENGTH:
        raise FlowError(texts.NAME_TOO_LONG)
    if " " in value:
        raise FlowError(
            texts.FIRST_NAME_ONLY.format(first=value.split()[0], whole=value)
        )
    return value


def clean_text(raw: str) -> str:
    value = understand.tidy(raw)
    if not value:
        raise FlowError(texts.NAME_EMPTY)
    if len(value) > MAX_TEXT_LENGTH:
        raise FlowError(texts.NAME_TOO_LONG)
    return value


def clean(step: Step, raw: str) -> str:
    return clean_name(raw) if step.type == NAME else clean_text(raw)


def next_step(steps: list[Step], answers: dict[str, Any], index: int) -> tuple[Step | None, int]:
    """The next applicable step at or after `index`, and its position."""
    while index < len(steps):
        step = steps[index]
        if step.applies(answers):
            return step, index
        index += 1
    return None, index


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def family_name_from(answers: dict[str, Any]) -> str | None:
    """Whichever spelling they picked, or typed when none of them fitted."""
    chosen = answers.get("family")
    if chosen == FAMILY_OTHER:
        return answers.get("family_other")
    return chosen


def _build_identify(answers, submitted_by, about, **_):
    return submissions.build(
        submissions.IDENTIFY,
        submitted_by=submitted_by,
        about=about,
        people=[
            submissions.person(
                submissions.SELF,
                answers["given"],
                family_name=family_name_from(answers),
                father_given_name=answers.get("father_given"),
            )
        ],
    )


def _build_parents(answers, submitted_by, about, **_):
    people = []
    if answers.get("father_given"):
        people.append(
            submissions.person(submissions.FATHER, answers["father_given"], sex="M")
        )
    if answers.get("mother_given"):
        people.append(
            submissions.person(
                submissions.MOTHER,
                answers["mother_given"],
                sex="F",
                family_name=answers.get("mother_family"),
            )
        )
    if not people:
        raise FlowError(
            "You didn't give me either name, so there's nothing to send. "
            "Try again when you know one of them."
        )
    return submissions.build(
        submissions.ADD_PARENTS,
        submitted_by=submitted_by,
        about=about,
        people=people,
    )


def _build_simple(kind: str, role: str):
    """Builder for the flows that add exactly one person."""

    def build(answers, submitted_by, about, **_):
        return submissions.build(
            kind,
            submitted_by=submitted_by,
            about=about,
            people=[
                submissions.person(
                    role,
                    answers["given"],
                    sex=answers.get("sex"),
                    family_name=answers.get("family_name"),
                )
            ],
        )

    return build


def _build_correction(
    answers,
    submitted_by,
    about,
    *,
    target_submission_id=None,
    target_person_id=None,
    target_label=None,
    **_,
):
    # `about` is the contributor; a correction is about the thing being
    # corrected, so the target replaces it. Otherwise the admin queue reads
    # "Correction to Steven" when the correction is to a name Steven sent.
    return submissions.build(
        submissions.CORRECTION,
        submitted_by=submitted_by,
        about=submissions.subject(
            person_id=target_person_id,
            submission_id=target_submission_id,
            label=target_label or about.get("label"),
        ),
        note=answers["note"],
        target_submission_id=target_submission_id,
        target_person_id=target_person_id,
    )


# ---------------------------------------------------------------------------
# The flows
# ---------------------------------------------------------------------------


#: Key the handlers inject into `answers` so prompts can name the person the
#: cursor is on. Prefixed so it cannot collide with a real answer id.
SUBJECT_KEY = "_subject_name"


def _subject(answers: dict[str, Any]) -> str | None:
    return answers.get(SUBJECT_KEY)


def _his_her(answers: dict[str, Any]) -> str:
    return texts.his_her(answers.get("sex"))


FAMILY_OTHER = "__other__"


def _family_choices() -> list[tuple[str, str]]:
    """The known spellings, plus an escape hatch for one we have not seen."""
    return [(variant, variant) for variant in config.FAMILY_NAME_VARIANTS] + [
        (texts.FAMILY_OTHER, FAMILY_OTHER)
    ]


IDENTIFY = Flow(
    kind=submissions.IDENTIFY,
    steps=[
        Step("given", NAME, texts.ASK_SELF_GIVEN),
        Step("family", CHOICE, texts.ASK_SELF_FAMILY, choices=_family_choices()),
        Step(
            "family_other",
            TEXT,
            texts.ASK_FAMILY_OTHER,
            only_if="family",
            only_if_value=FAMILY_OTHER,
        ),
        Step(
            "father_given",
            NAME,
            f"{texts.ASK_SELF_FATHER}\n\n{texts.ASK_SELF_FATHER_WHY}",
            optional=True,
        ),
    ],
    build=_build_identify,
)


ADD_PARENTS = Flow(
    kind=submissions.ADD_PARENTS,
    steps=[
        Step("father_given", NAME, lambda a: texts.ask_father(_subject(a)), optional=True),
        Step("mother_given", NAME, lambda a: texts.ask_mother(_subject(a)), optional=True),
        Step(
            "mother_family",
            TEXT,
            lambda a: texts.ask_mother_family(_subject(a)),
            optional=True,
            only_if="mother_given",
        ),
    ],
    build=_build_parents,
)


ADD_SIBLING = Flow(
    kind=submissions.ADD_SIBLING,
    steps=[
        Step(
            "sex",
            CHOICE,
            lambda a: texts.ask_sibling_sex(_subject(a)),
            choices=[(texts.SIBLING_BROTHER, "M"), (texts.SIBLING_SISTER, "F")],
        ),
        Step(
            "given",
            NAME,
            lambda a: texts.ASK_SIBLING_GIVEN.format(his_her=_his_her(a)),
        ),
    ],
    build=_build_simple(submissions.ADD_SIBLING, submissions.SIBLING),
)


ADD_SPOUSE = Flow(
    kind=submissions.ADD_SPOUSE,
    steps=[
        Step(
            "sex",
            CHOICE,
            lambda a: texts.ask_spouse_sex(_subject(a)),
            choices=[(texts.SPOUSE_HUSBAND, "M"), (texts.SPOUSE_WIFE, "F")],
        ),
        Step(
            "given",
            NAME,
            lambda a: texts.ASK_SPOUSE_GIVEN.format(his_her=_his_her(a)),
        ),
        Step(
            "family_name",
            TEXT,
            lambda a: texts.ASK_SPOUSE_FAMILY.format(his_her=_his_her(a)),
            optional=True,
        ),
    ],
    build=_build_simple(submissions.ADD_SPOUSE, submissions.SPOUSE),
)


ADD_CHILD = Flow(
    kind=submissions.ADD_CHILD,
    steps=[
        Step(
            "sex",
            CHOICE,
            lambda a: texts.ask_child_sex(_subject(a)),
            choices=[(texts.CHILD_SON, "M"), (texts.CHILD_DAUGHTER, "F")],
        ),
        Step(
            "given",
            NAME,
            lambda a: texts.ASK_CHILD_GIVEN.format(his_her=_his_her(a)),
        ),
    ],
    build=_build_simple(submissions.ADD_CHILD, submissions.CHILD),
)


CORRECTION = Flow(
    kind=submissions.CORRECTION,
    steps=[Step("note", TEXT, texts.FIX_ASK_NOTE)],
    build=_build_correction,
)


#: Menu key -> flow, in the order the spec lists them. The visible label comes
#: from texts.menu_labels() so it can name whoever the cursor is on.
MENU: list[tuple[str, Flow]] = [
    ("parents", ADD_PARENTS),
    ("sibling", ADD_SIBLING),
    ("spouse", ADD_SPOUSE),
    ("child", ADD_CHILD),
]

#: When somebody dictates a list under a question, the question itself says
#: what they are talking about — "his father's name?" answered with three
#: names means three parents, not three strangers.
_DEFAULT_ROLES = {
    (submissions.ADD_PARENTS, "father_given"): submissions.FATHER,
    (submissions.ADD_PARENTS, "mother_given"): submissions.MOTHER,
    (submissions.ADD_SIBLING, "given"): submissions.SIBLING,
    (submissions.ADD_SPOUSE, "given"): submissions.SPOUSE,
    (submissions.ADD_CHILD, "given"): submissions.CHILD,
}


def default_role(kind: str, step_id: str) -> str | None:
    return _DEFAULT_ROLES.get((kind, step_id))


BY_KIND: dict[str, Flow] = {
    flow.kind: flow
    for flow in (IDENTIFY, ADD_PARENTS, ADD_SIBLING, ADD_SPOUSE, ADD_CHILD, CORRECTION)
}
