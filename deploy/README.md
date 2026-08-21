# Putting it on a machine

One small AWS box that runs the bot and holds the database. About **$5 a
month**, up in ten minutes, and nothing on your laptop.

## Why not serverless

SQLite wants a real disk. App Runner has no persistent storage, Lambda has
none, and SQLite over EFS has locking problems that surface exactly when two
relatives submit at the same moment.

This is a starting point, not a commitment. `db.py` is the only file that
touches the database, so moving to Postgres and Lambda later is contained —
and `review.py --export` means the data is portable whatever happens. Doing
that port *before* the pilot would mean a week of infrastructure work before
a single real name is captured.

## Once

You need Terraform and AWS credentials on whatever you're typing on.

```bash
cd deploy
terraform init
terraform apply
```

It prints an address. Then, one SSH session:

```bash
ssh admin@<that address>
sudo nano /opt/family-tree/.env
```

Three lines to fill in:

- `TELEGRAM_BOT_TOKEN=` — from @BotFather
- `AWS_ACCESS_KEY_ID=` and `AWS_SECRET_ACCESS_KEY=` — from
  `terraform output -json backup_credentials`

Save, then:

```bash
sudo systemctl restart family-tree
```

Message the bot. It should answer.

The token is never in git and never in a chat window. It lives in that one
file, readable only by root.

## Day to day

Three commands on the box:

```bash
tree-review                       # what came in, with duplicate evidence
tree-review --who 15              # everything about person 15, and who said it
tree-review --find Saide          # which Saide? get their number
tree-review --merge 8 --into 12   # same person, link rather than duplicate
tree-review --approve 8           # accept it

tree-chart                        # rebuild the visual
tree-update                       # pull new code and restart
```

`tree-chart` writes an HTML file you can scp down and send round. The database
is at `/var/lib/family-tree/family.db`, deliberately outside the checkout, so
pulling new code can never touch it.

## Backups

Every night at midnight Sydney time, two files go to S3: the database, and a
JSON export of everything. Kept a year, versioned.

The JSON matters as much as the database. It is readable by anything, so what
your relatives typed survives this project — not just a disk failure. That is
the difference between "we lost it, can you send it again" and never having to
ask.

Check it is running:

```bash
systemctl status family-tree-backup.timer
sudo /usr/local/bin/family-tree-backup     # run one now
aws s3 ls s3://<bucket>/ --recursive | tail
```

To restore, copy a `family.db` back into `/var/lib/family-tree/` and restart.

## When something looks wrong

```bash
sudo journalctl -u family-tree -n 50 --no-pager     # what the bot is doing
sudo systemctl restart family-tree
```

The service restarts itself on failure, so a dropped connection to Telegram
recovers on its own. Conversation state is in memory, so a restart mid-chat
means a relative sends `/start` again — everything they had already sent is
safe in the queue.

## Taking it down

```bash
terraform destroy
```

The backup bucket survives on purpose — deleting the machine should not delete
the family. Empty it by hand if you really mean it.
