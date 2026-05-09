#!/usr/bin/env bash
# One-time per-stage Heroku provisioning for ProductSync.
#
# Usage:  ./scripts/heroku-setup.sh test|staging|prod
#
# Idempotent — safe to re-run; existing apps/addons/config are detected and
# skipped rather than re-created.

set -euo pipefail

STAGE="${1:-}"
case "$STAGE" in
  test|staging|prod) ;;
  *)
    echo "Usage: $0 test|staging|prod" >&2
    exit 1
    ;;
esac

APP="productsync-${STAGE}"
REMOTE="heroku-${STAGE}"

# Pretty output
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red() { printf "\033[31m%s\033[0m\n" "$*" >&2; }
step() { echo; printf "\033[1;36m▸ %s\033[0m\n" "$*"; }

# Sanity: heroku CLI must be installed and logged in
if ! command -v heroku >/dev/null 2>&1; then
  red "Heroku CLI not found. Install it: https://devcenter.heroku.com/articles/heroku-cli"
  exit 1
fi
heroku auth:whoami >/dev/null 2>&1 || { red "Not logged in. Run: heroku login"; exit 1; }

# Sanity: this script must run from the productsync/ project root
if [[ ! -f Procfile ]] || [[ ! -f requirements.txt ]]; then
  red "Run this script from the productsync/ project root (where Procfile lives)."
  exit 1
fi

# Sanity: must be a git repo
if [[ ! -d .git ]]; then
  red "productsync/ is not a git repo yet. Run:"
  red "  git init -b main && git add . && git commit -m 'Initial ProductSync commit'"
  exit 1
fi

# Generate strong secrets
SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

step "Stage: ${STAGE}  →  Heroku app: ${APP}"

# 1. Create the app (if not already)
if heroku apps:info -a "$APP" >/dev/null 2>&1; then
  yellow "App $APP already exists — skipping create"
else
  step "Creating Heroku app $APP"
  heroku apps:create "$APP"
fi

# 2. Provision Postgres (if not already)
if heroku addons -a "$APP" 2>/dev/null | grep -q "heroku-postgresql"; then
  yellow "Postgres add-on already present — skipping"
else
  step "Adding Postgres essential-0 (~\$5/mo)"
  heroku addons:create heroku-postgresql:essential-0 -a "$APP"
fi

# 3. Set required config vars (only if not already set, so we don't rotate keys)
step "Setting config vars (only if missing)"
existing_cfg="$(heroku config -a "$APP" --shell 2>/dev/null || true)"

set_if_missing() {
  local key="$1" val="$2"
  if echo "$existing_cfg" | grep -q "^${key}="; then
    yellow "  ${key} already set — keeping existing value"
  else
    heroku config:set "${key}=${val}" -a "$APP" >/dev/null
    green  "  ${key} set"
  fi
}
set_if_missing SECRET_KEY      "$SECRET_KEY"
set_if_missing ENCRYPTION_KEY  "$ENCRYPTION_KEY"
set_if_missing FLASK_APP       "wsgi.py"
set_if_missing ENABLE_SCHEDULER "1"
# Placeholder — user updates after first deploy with the actual Heroku URL
set_if_missing PUBLIC_BASE_URL "https://${APP}.herokuapp.com"

# 4. Add git remote (if not present)
if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  yellow "Git remote $REMOTE already exists — skipping"
else
  step "Adding git remote $REMOTE"
  heroku git:remote -a "$APP" -r "$REMOTE"
fi

# 5. Scale worker to 0 (we're not using RQ yet; web stays at 1 by default after first push)
step "Scaling: web=1, worker=0 (deferred until we wire RQ)"
heroku ps:scale worker=0 -a "$APP" 2>/dev/null || true

# 6. Print summary + next-step instructions
APP_URL="$(heroku apps:info -a "$APP" --json 2>/dev/null | python -c 'import sys,json;print(json.load(sys.stdin)["app"]["web_url"].rstrip("/"))')"

step "Done provisioning $APP"
cat <<EOF

  Heroku app:       $APP
  Git remote:       $REMOTE
  Live URL:         $APP_URL

Next steps:

  1. Deploy:
       git push $REMOTE main

  2. Update PUBLIC_BASE_URL to the actual URL Heroku assigned:
       heroku config:set PUBLIC_BASE_URL=$APP_URL -a $APP

  3. Seed the first admin user (only on first deploy):
       heroku run flask seed -a $APP

  4. Open it:
       heroku open -a $APP

EOF

green "✓ $APP ready."
