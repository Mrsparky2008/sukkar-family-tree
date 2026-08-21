# Step 2 — Telegram capture

Run it:

    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=...        # or put it in .env
    python -m bot

Long polling, so there is no webhook, no public hostname, and no certificate to
renew. It runs anywhere Python runs.

## Files

| file | holds |
|---|---|
| `texts.py` | Every string the bot says. One file, so the tone stays consistent and nothing family-specific gets hardcoded. |
| `flows.py` | The conversations, as data: a list of questions and a function turning answers into a payload. No Telegram, no database — testable on its own. |
| `handlers.py` | One generic ask → confirm → advance loop over those flows. |
| `understand.py` | Reading what people actually type when the bot asked with buttons. |
| `dictation.py` | Reading a whole family out of one message. |
| `store.py` | The only file that touches the database. |
| `main.py` | Wiring and entry point. |

## Why the flows are data

The spec asks for one question per message and for every typed answer to be
confirmed before use. Written out per flow, that is five near-identical state
machines and about twenty-five conversation states. Written as data, it is five
states and a list of questions per flow. Adding a question is one line.

## Reading what people type

The bot asks with buttons; people answer with words. Every "use the buttons
above" costs trust, and for the older relatives who hold most of the knowledge
it is where they put the phone down.

`understand.py` handles three things found by sitting someone in front of it:

- **Trailing punctuation.** "Steven." became a person called `Steven.`, on the
  display name, forever. Edge punctuation is stripped; inner punctuation
  survives, because Abou-Khalil and O'Brien are real names.
- **Typed answers to button questions.** "Su K ar" is somebody spelling their
  own surname out loud. It now lands on the Sukar button. So does "daughter",
  and "dunno" for "I don't know".
- **Yes and no.** "Ok", "yeah", "nah", "that's all" all work where the bot
  offered two buttons.

It never guesses at a name — only at answers the bot already offered, and only
when one option is clearly ahead. Genuine ambiguity re-asks, showing the
question *with* its buttons rather than scolding and leaving nothing to tap.

## A whole family in one message

One question at a time suits someone who needs coaxing. It is exactly wrong
for the person who already knows — the aunt who can name four generations and
wants to get it out in one go. Told "that's longer than a first name", she
stops, and she was the one worth listening to.

So a message like

```
Kalim's parents are Toufic and Cilene
Kalim's sisters: Dibeh, Sonia and Saide, married to Jamil Tarabay
                                          — the other girls are single
```

is read into six people, with Jamil hanging off Saide rather than off Kalim,
and shown back as an editable list before anything is sent.

The parsing is deliberately literal: relationship words set the role, and a
role gives the sex. It never invents a name it cannot see. Two things it does
have to guess — which of an unlabelled pair of parents is the father, and a
spouse's sex from their partner's — are said out loud in the reply.

A remark that fits nobody in particular ("the other girls are single") is
kept verbatim on the submission rather than pinned to whoever happened to be
on the same line. Attributing "the others" by rule gets it wrong, and nobody
notices.

Ordinary single answers still go down the ordinary path. Only a message that
names a relationship, or clearly lists several people, gets parsed.

## Constraint 4

`store.py` is the boundary. It wraps exactly what the bot may do: read people,
read its own submissions, write to `submissions`, and record who a Telegram
user is in `contributors`.

There is deliberately no wrapper for `create_person`, `update_person`, or
`create_union` — and a test fails the build if anything under `bot/` calls one.
If a flow seems to need one, it needs `store.queue()` instead.

Writing to `contributors` is not a violation: that table records who is holding
the phone, not who exists in the family.

## Duplicate flagging

Every queued submission is fuzzy-matched against existing people — on given
name plus the father's given name, scoped to the submitter's branch — and the
best match is stored in `submissions.matched_person_id`.

That is a hint for whoever reviews it. Nothing is auto-merged and nothing is
auto-rejected.

## Testing

`tests/harness.py` fakes Telegram's transport but drives the real handlers
through the real routing table from `build_conversation()`, so the callback
patterns are covered rather than reimplemented.

    python -m unittest discover -s tests -t .

## Not done, deliberately

Conversation state lives in memory. If the bot restarts mid-conversation, the
relative sends `/start` and picks up again. Persisting it would mean a pickle
file to manage and corrupt; the failure it prevents is one extra tap.
