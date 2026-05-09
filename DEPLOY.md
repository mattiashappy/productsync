# Deploying ProductSync to Heroku

**Current setup:** one production app on Heroku, autodeploying from `main` on
GitHub. Testing and staging happen on your laptop until traffic justifies
spinning up additional Heroku stages.

```
┌────────────┐                ┌─────────────────────┐
│ git push   │  →  origin/main  →  Heroku autodeploy → released
└────────────┘                └─────────────────────┘

local dev (sqlite)            production (Postgres)
  ↑
  daily work
```

---

## First-time configuration of the autodeploy app

After you've created the app in the Heroku dashboard and connected GitHub
autodeploy, **run the configure script once** to:

- Provision Postgres
- Generate and set `SECRET_KEY` and `ENCRYPTION_KEY` (strong random)
- Set `FLASK_APP=wsgi.py`, `ENABLE_SCHEDULER=1`, `PUBLIC_BASE_URL=<app URL>`
- Apply database migrations
- Seed the default admin user

```bash
heroku login                                     # one-time
heroku apps                                      # find your app name
./scripts/heroku-configure.sh <your-app-name>
```

The script is **idempotent** — re-running it is safe, and crucially it will
**never overwrite an existing `ENCRYPTION_KEY`**. (Rotating that key would
make every encrypted store credential unrecoverable.)

After this:

```
heroku open -a <your-app-name>                   # open in browser
# log in as admin@example.com / changeme123 — change the password immediately
```

---

## Daily deploy workflow

Just push to GitHub. Heroku does the rest.

```bash
git add .
git commit -m "fix: <what you fixed>"
git push origin main
```

Heroku watches `origin/main`, builds a slug, runs the release phase
(`flask db upgrade`), and rolls new dynos. If migrations fail, the release
phase aborts and the old dynos keep serving traffic — you don't get a
half-migrated app.

Watch the build:

```bash
heroku logs --tail -a <your-app-name>
```

---

## Local dev = your "testing/staging"

For now, develop and test locally before pushing to main:

```bash
cd productsync
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Generate ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste it into .env

flask db upgrade
flask seed
flask --app wsgi run --debug
```

Each branch / feature can be tested locally against a SQLite DB before being
merged to `main` and shipped to prod. When you eventually want a real
preprod environment, see the "Adding a staging Heroku app" section below.

### Faking webhooks against your local instance

WooCommerce can't reach your laptop directly. Use a tunnel:

```bash
# ngrok (easiest)
ngrok http 5001
# Cloudflare Tunnel is the free permanent alternative:
# https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
```

Set `PUBLIC_BASE_URL` in `.env` to the tunnel URL, then connect WooCommerce.
The auto-registered webhook will point at the tunnel and orders will fire
through to your local dev instance.

---

## Things you'll want to do soon (recommended order)

1. **Change the default admin password.** `admin@example.com / changeme123` is
   public knowledge — rotate it via the app or a `heroku run python` shell.

2. **Hook up a custom domain** when you're ready:
   ```bash
   heroku domains:add productsync.com -a <app>
   heroku certs:auto:enable -a <app>
   heroku config:set PUBLIC_BASE_URL=https://productsync.com -a <app>
   ```

3. **Enable backups:**
   ```bash
   heroku pg:backups:schedule DATABASE_URL --at "02:00 Europe/Stockholm" -a <app>
   ```

4. **Upgrade dyno** if it's getting hit:
   ```bash
   heroku ps:type web=basic -a <app>     # always-on, $7/mo
   heroku ps:type web=standard-1x -a <app>   # production-grade, $25/mo
   ```

---

## Adding a staging Heroku app later (when traffic justifies it)

Same pattern: create another app, connect GitHub autodeploy from a different
branch, run the configure script. Suggested branch layout:

| Branch    | Heroku app             | Purpose                |
|-----------|------------------------|------------------------|
| `main`    | `productsync-prod`     | Live customers         |
| `staging` | `productsync-staging`  | Pre-prod validation    |

Workflow becomes:
- Develop on a feature branch → merge to `staging` → autodeploys to staging
- Test against the staging app
- Promote to `main` → autodeploys to prod

The configure script works the same way for the new app.

---

## Common gotchas

- **`ENCRYPTION_KEY is not set`** at runtime → the configure script wasn't run
  (or skipped silently). Run it. If you have already encrypted credentials in
  the DB and lost the key, those credentials are unrecoverable; you'll have to
  delete and re-add the affected stores.

- **Webhooks return 404** in WooCommerce → `PUBLIC_BASE_URL` is wrong. Check
  `heroku config -a <app>` against `heroku info -a <app>`.

- **Release phase fails with "no such command 'db'"** → `FLASK_APP` not set.
  Run `heroku config:set FLASK_APP=wsgi.py -a <app>`.

- **Build fails on requirements** → make sure you're pushing from the project
  root where `Procfile` and `requirements.txt` live. If your repo has the
  ProductSync code in a subdirectory (e.g. `productsync/`), Heroku won't find
  it; either move files to the repo root or use the multi-procfile buildpack.

- **App says "Not Found" on the homepage** → the deploy itself probably worked
  but something in the app is throwing. Check `heroku logs --tail`.

- **TLS certificate errors when connecting WooCommerce stores** → the app uses
  the `truststore` package, which on Heroku reads the system CA bundle.
  Should "just work" — but if a store is behind an unusual cert authority,
  contact me and we'll add the cert.

---

## Useful per-app commands

```bash
heroku logs --tail -a <app>                           # live logs
heroku ps -a <app>                                    # dyno status
heroku run flask --app wsgi shell -a <app>            # interactive REPL
heroku pg:psql -a <app>                               # Postgres shell
heroku config -a <app>                                # all env vars
heroku releases -a <app>                              # deploy history
heroku rollback -a <app>                              # undo last deploy
```
