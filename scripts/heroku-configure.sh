#!/usr/bin/env bash
# Configure an existing Heroku app that's already wired up to autodeploy from GitHub.
# Sets the required env vars, ensures Postgres, applies migrations, seeds admin.
#
# Usage:  ./scripts/heroku-configure.sh <heroku-app-name>
#
# Idempotent — safe to re-run; values that already exist are NOT overwritten
# (critical for ENCRYPTION_KEY: rotating it makes encrypted credentials unrecoverable).

set -euo pipefail

APP="${1:-}"
if [[ -z "$APP" ]]; then
  echo "Usage: $0 <heroku-app-name>" >&2
  echo "Hint:  heroku apps    # to list your apps" >&2
  exit 1
fi

green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
step()   { echo; printf "\033[1;36m▸ %s\033[0m\n" "$*"; }

# Sanity checks
command -v heroku >/dev/null 2>&1 || { red "Heroku CLI not found. Install it first."; exit 1; }
heroku auth:whoami >/dev/null 2>&1 || { red "Not logged in. Run: heroku login"; exit 1; }
heroku apps:info -a "$APP" >/dev/null 2>&1 || { red "App '$APP' not found on Heroku."; exit 1; }

step "Configuring $APP"

# 1. Postgres add-on (autodeploy doesn't add this for you)
if heroku addons -a "$APP" 2>/dev/null | grep -q "heroku-postgresql"; then
  yellow "Postgres add-on already present — keeping it"
else
  step "Adding Postgres essential-0 (~\$5/mo)"
  heroku addons:create heroku-postgresql:essential-0 -a "$APP"
fi

# 2. Generate strong secrets
SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
APP_URL="$(heroku apps:info -a "$APP" --json 2>/dev/null | python -c 'import sys,json;print(json.load(sys.stdin)["app"]["web_url"].rstrip("/"))')"

# 3. Set config vars (only if missing — never rotate ENCRYPTION_KEY!)
step "Setting required config vars (only if missing)"
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
set_if_missing PUBLIC_BASE_URL "$APP_URL"

# 4. Make sure migrations are applied
step "Applying database migrations (idempotent)"
heroku run --no-tty -a "$APP" "flask db upgrade" || yellow "Migrations failed — see above. Often safe to re-run."

# 5. Seed initial admin user (idempotent — skips if any User exists)
step "Seeding initial admin user (skipped if any user already exists)"
heroku run --no-tty -a "$APP" "flask seed" || yellow "Seed failed — see above."

# 6. Done
step "Configured $APP"
cat <<EOF

  App URL:           $APP_URL
  Default admin:     admin@example.com / changeme123  (change immediately on prod)

  Useful commands:
    heroku logs --tail -a $APP            # live logs
    heroku config -a $APP                 # all env vars
    heroku ps -a $APP                     # dyno status
    heroku open -a $APP                   # open in browser

  Auto-deploy is wired up — every push to GitHub main triggers a Heroku build.
  The release phase runs 'flask db upgrade' before swapping dynos, so
  migrations apply automatically on each deploy.

EOF

green "✓ $APP ready."
