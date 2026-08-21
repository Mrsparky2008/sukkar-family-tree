# Step 3 — the review interface

```bash
python -m admin        # http://localhost:8080
```

On the server it runs as a service bound to localhost, reached over an SSH
tunnel — no public port, no certificate. See `deploy/README.md`.

## Signing in

One shared password (`ADMIN_PASSWORD`) plus your Telegram ID. The password
opens the door; the ID decides your scope and goes on the audit trail with
every decision. Branch admins see only their branch's queue — they are the
people who actually know those relatives. Super admins (config) see all of
it, which is where cross-branch merges happen.

No password configured means the interface is **closed**, not open.

## The queue

Each pending submission shows the claim, who made it, who they heard it from,
and who it might already be — with the evidence ("same father, same mother",
"already recorded as their child") and a score. The four actions from the
spec:

- **Approve** — creates the people. Refused with the reason when it looks
  like a near-certain duplicate.
- **Same person — merge** — links to an existing person (pre-filled with the
  best match) and still applies the relationship. Merging is not discarding.
- **Reject** — requires a reason; the contributor can see it.
- **Approve anyway** — for when the duplicate guard is wrong.

**Fix a spelling before approving** edits what gets created, never the
submission. The original stays byte-for-byte as sent — that is the whole
provenance model, and there is a test for it.

## One implementation of the rules

Every button calls the same `review.approve` / `merge` / `reject` as the
command line. `review.py` and this interface are two ways of pressing the
same buttons, so they cannot drift apart.
