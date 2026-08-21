#!/bin/bash
# Runs once, when the machine is first created.
#
# Everything here is idempotent so it can be re-run by hand after a change:
#   sudo bash /opt/family-tree/deploy/setup.sh
set -euo pipefail

APP=/opt/family-tree
DATA=/var/lib/family-tree
REPO="${repository}"
BRANCH="${branch}"
BUCKET="${bucket}"
REGION="${region}"
TOKEN="${telegram_token}"
ADMIN_PASSWORD="${admin_password}"
BACKUP_KEY="${backup_key}"
BACKUP_SECRET="${backup_secret}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git awscli sqlite3

# --- the code --------------------------------------------------------------

if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch origin "$BRANCH"
  git -C "$APP" reset --hard "origin/$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO" "$APP"
fi

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

# --- the database ----------------------------------------------------------
#
# Deliberately outside the checkout, so pulling new code can never touch it.

mkdir -p "$DATA"
chown -R root:root "$DATA"
chmod 750 "$DATA"

# --- configuration ---------------------------------------------------------
#
# Created once and never overwritten: re-running this script must not wipe the
# token someone pasted in by hand.

if [ ! -f "$APP/.env" ]; then
  cat > "$APP/.env" <<ENV
# The token from @BotFather. If blank, paste it in and then:
#   sudo systemctl restart family-tree
TELEGRAM_BOT_TOKEN=$TOKEN

# Password for the review interface. Share it with your branch admins; their
# Telegram ID decides what each of them can see.
ADMIN_PASSWORD=$ADMIN_PASSWORD

# Signs the review interface's login sessions. Generated at setup.
SECRET_KEY=$(openssl rand -hex 32)

FAMILY_TREE_DB=$DATA/family.db

# So nightly backups can reach S3.
AWS_ACCESS_KEY_ID=$BACKUP_KEY
AWS_SECRET_ACCESS_KEY=$BACKUP_SECRET
AWS_DEFAULT_REGION=$REGION
BACKUP_BUCKET=$BUCKET
ENV
  chmod 600 "$APP/.env"
fi

# --- the service -----------------------------------------------------------

cat > /etc/systemd/system/family-tree.service <<UNIT
[Unit]
Description=Family tree Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/.venv/bin/python -m bot
Restart=always
RestartSec=10
# A dropped connection to Telegram should not become an outage nobody notices.
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable family-tree

# Only start it if a token is actually present; otherwise it would restart in
# a loop until somebody logs in.
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$APP/.env"; then
  systemctl restart family-tree
fi

# --- the review interface --------------------------------------------------
#
# Bound to localhost only: reviewers reach it over an SSH tunnel, so there is
# no public port, no TLS certificate, and nothing for the internet to find.
#
#   ssh -L 8080:localhost:8080 admin@<server>   then  http://localhost:8080

cat > /etc/systemd/system/family-tree-admin.service <<UNIT
[Unit]
Description=Family tree review interface
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$APP/.venv/bin/python -m admin
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable family-tree-admin
if grep -q '^ADMIN_PASSWORD=.\+' "$APP/.env"; then
  systemctl restart family-tree-admin
fi

# --- backups ---------------------------------------------------------------

cat > /usr/local/bin/family-tree-backup <<'BACKUP'
#!/bin/bash
# One SQLite file and one JSON export, nightly. The JSON matters as much as
# the database: it is readable by anything, so the data outlives this project.
set -euo pipefail
source /opt/family-tree/.env

STAMP=$(date -u +%Y-%m-%d)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# .backup rather than cp: safe to run while the bot is writing.
sqlite3 "$FAMILY_TREE_DB" ".backup '$WORK/family.db'"
/opt/family-tree/.venv/bin/python /opt/family-tree/review.py \
  --db "$FAMILY_TREE_DB" --export "$WORK/family.json" >/dev/null

aws s3 cp "$WORK/family.db"   "s3://$BACKUP_BUCKET/$STAMP/family.db"   --only-show-errors
aws s3 cp "$WORK/family.json" "s3://$BACKUP_BUCKET/$STAMP/family.json" --only-show-errors
logger -t family-tree "backup written for $STAMP"
BACKUP
chmod +x /usr/local/bin/family-tree-backup

cat > /etc/systemd/system/family-tree-backup.service <<UNIT
[Unit]
Description=Back the family tree up to S3

[Service]
Type=oneshot
ExecStart=/usr/local/bin/family-tree-backup
UNIT

cat > /etc/systemd/system/family-tree-backup.timer <<UNIT
[Unit]
Description=Nightly family tree backup

[Timer]
OnCalendar=*-*-* 14:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now family-tree-backup.timer

# --- convenience -----------------------------------------------------------
#
# So reviewing is `tree-review`, not a path to remember.

cat > /usr/local/bin/tree-review <<'REVIEW'
#!/bin/bash
source /opt/family-tree/.env
exec /opt/family-tree/.venv/bin/python /opt/family-tree/review.py \
  --db "$FAMILY_TREE_DB" --as "$${SUPER_ADMIN:-0}" "$@"
REVIEW
chmod +x /usr/local/bin/tree-review

cat > /usr/local/bin/tree-chart <<'CHART'
#!/bin/bash
# Rebuild the visual and print where it landed.
source /opt/family-tree/.env
OUT=$${1:-/tmp/family-tree.html}
exec /opt/family-tree/.venv/bin/python /opt/family-tree/web/build.py \
  --db "$FAMILY_TREE_DB" --out "$OUT"
CHART
chmod +x /usr/local/bin/tree-chart

cat > /usr/local/bin/tree-update <<'UPDATE'
#!/bin/bash
# Pull new code and restart. The database is elsewhere and is not touched.
set -euo pipefail
git -C /opt/family-tree pull --ff-only
/opt/family-tree/.venv/bin/pip install -q -r /opt/family-tree/requirements.txt
systemctl restart family-tree
systemctl --no-pager status family-tree | head -5
UPDATE
chmod +x /usr/local/bin/tree-update

logger -t family-tree "setup complete"
