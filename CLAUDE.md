# Working on the family tree

## Deliberating is not instructing

**Default to talking, not building.** When a message is exploratory, answer it
and stop. Do not write code, do not change files, do not deploy — propose, and
wait for a decision.

Treat as deliberation unless told otherwise:

- "maybe we could…", "is it worth…", "how hard would it be…", "what do you think"
- any question about whether to do something, or how something might work
- thinking out loud about a feature that does not exist yet

Treat as an instruction when it says so: "build it", "do it", "go ahead",
"fix that", "add it", "approve them", or an explicit description of work to
carry out.

When it is genuinely ambiguous, say what you would build in a sentence or two
and ask. A wrong guess in this direction is expensive: it spends a deploy, it
puts code in the repository nobody asked for, and it moves the conversation on
before the idea was finished.

The reverse also holds — once a decision is made, carry it out fully rather
than checking back at every step.

## The shape of the thing

Constraints that predate any particular session and are not up for casual
revision. If a change would break one, say so before writing it.

- **A graph, not a tree.** Cousins marry cousins here. Anything that assumes a
  single line of descent is wrong.
- **No dates. Ever.** Not birth, not death, not age. The bot cannot express one.
- **Display names are computed** from given name + father's given name + family
  name. Never typed, never stored whole.
- **Nothing writes to `people` except through the submissions queue.** Not the
  bot, not a helper, not a quick fix. Every person on the tree answers "who
  says so" with a submission.
- **Provenance is permanent.** Claims are never rewritten, only superseded. A
  correction adds a record; it does not erase one.
- **Permanent meaningless IDs.** Shown as #N after approval, never reused, never
  changed. Names are for humans; IDs are how the system identifies people.
- **White-label.** Family-specific names live in `config.py` only, and a test
  enforces it.
- **It must still run in five years.** Python, SQLite, python-telegram-bot,
  Flask, static HTML. No React, no build step, no framework that needs feeding.
- **No AI at runtime.** The bot is deterministic rules. The reasoning happens at
  the writing desk, not in the bot.

## Working habits that have earned their place

- **Rehearse on a copy before touching live data.** Back up to S3, download,
  run it, check the result, then run it for real, then verify, then republish.
- **Every live change is verified against the real database**, not asserted from
  memory or from what the code appears to do.
- **Tests describe behaviour a person would notice**, in the words a person
  would use. The test name is the sentence; the docstring says why it matters.
- **Prompts that need explaining have already failed.** If wording confuses the
  person who built it, rewrite it rather than defending it.
