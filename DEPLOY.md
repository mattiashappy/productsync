# Deploying ProductSync to Heroku

Three environments, three Heroku apps, one git repo with three remotes:

| Stage      | Heroku app             | Git remote        | Purpose                              |
|------------|------------------------|-------------------|--------------------------------------|
| Testing    | `productsync-test`     | `heroku-test`     | Latest commits, throwaway data       |
| Staging    | `productsync-staging`  | `heroku-staging`  | Pre-prod validation, near-prod data  |
| Production | `productsync-prod`     | `heroku-prod`     | Live customers                       |

**Key rule:** each environment has its **own** `ENCRYPTION_KEY`, `SECRET_KEY`, Postgres database, and webhook URLs. Credentials encrypted in test will *never* decrypt in prod. That's intentional — it's a security boundary, not a bug.

---

## One-time setup (do this once on your laptop)

### 1. Git repo

ProductSync lives at <https://github.com/mattiashappy/productsync> — that's the canonical source. Heroku apps are deployed from local pushes via per-stage remotes (set up by the script in step 3). The flow looks like:

```
local main ── push ──► origin (GitHub)        # canonical history, backup, PRs
            └─ push ─► heroku-test            # auto-deploys to test
            └─ push ─► heroku-staging         # auto-deploys to staging
            └─ push ─► heroku-prod            # auto-deploys to production
```

If you're starting on a fresh laptop:

```bash
git clone https://github.com/mattiashappy/productsync.git
cd productsync
```

If git's TLS handshake fails with "unable to get local issuer certificate" (corporate AV / Avast / Zscaler intercepting HTTPS), switch git to use the OS certificate store:

```bash
git config --global http.sslBackend schannel   # Windows
```

### 2. Install + log into the Heroku CLI

```bash
brew install heroku/brew/heroku           # macOS
# or: https://devcenter.heroku.com/articles/heroku-cli for Windows/Linux
heroku login
```

### 3. Provision all three Heroku apps

The included `scripts/heroku-setup.sh` handles one stage at a time. Run it three times:

```bash
./scripts/heroku-setup.sh test
./scripts/heroku-setup.sh staging
./scripts/heroku-setup.sh prod
```

Each invocation will:

- Create the Heroku app (e.g. `productsync-test`)
- Provision a Heroku Postgres add-on (essential-0)
- Generate a fresh `SECRET_KEY` and `ENCRYPTION_KEY` and set them as config vars
- Set `ENABLE_SCHEDULER=1` (hourly reconciliation runs in the web dyno)
- Add a git remote (`heroku-test`, `heroku-staging`, `heroku-prod`)
- Scale the worker dyno to 0 (we don't use RQ yet — Procfile entry stays in place for later)
- Print the auto-generated app URL so you can set `PUBLIC_BASE_URL` next

### 4. Set `PUBLIC_BASE_URL` per stage

Heroku assigns each app a URL like `https://productsync-test-1a2b3c4d.herokuapp.com`. After app creation, copy the URL the script prints and set it:

```bash
heroku config:set PUBLIC_BASE_URL=https://productsync-test-1a2b3c.herokuapp.com -a productsync-test
heroku config:set PUBLIC_BASE_URL=https://productsync-staging-... -a productsync-staging
heroku config:set PUBLIC_BASE_URL=https://productsync-prod-... -a productsync-prod
```

`PUBLIC_BASE_URL` is what the app uses when registering webhooks with WooCommerce/Shopify — it must match the public URL of the Heroku app, otherwise webhooks won't be delivered.

---

## Daily deploy workflow

```bash
# 1. Always test first
git push heroku-test main

# 2. If it works, promote to staging
git push heroku-staging main

# 3. If staging is good for a day, promote to prod
git push heroku-prod main
```

The `release:` step in [Procfile](Procfile) runs `flask db upgrade` automatically on each push, so migrations apply before the new dynos take traffic. If a migration fails, the release phase aborts and the old dynos keep serving — you don't get a half-migrated app.

### Seed the first admin user (once per environment, on first deploy only)

```bash
heroku run flask seed -a productsync-test
heroku run flask seed -a productsync-staging
heroku run flask seed -a productsync-prod
```

This creates `admin@example.com` / `changeme123`. **Change the password immediately on prod** by signing in and rotating it (or by running a one-off `heroku run python` script — until we build a settings page).

---

## Useful per-stage commands

```bash
heroku logs --tail -a productsync-prod                           # live logs
heroku ps -a productsync-prod                                    # dyno status
heroku run flask --app wsgi shell -a productsync-prod            # interactive REPL
heroku pg:psql -a productsync-prod                               # Postgres shell
heroku config -a productsync-prod                                # all env vars
heroku releases -a productsync-prod                              # deploy history
heroku rollback -a productsync-prod                              # undo last deploy
```

---

## What costs you per month (USD, as of writing)

Per app:
- **Eco dyno** (web): $5 (sleeps after 30 min idle — fine for testing)
- OR **Basic dyno** (web): $7 (always-on; recommended for staging + prod)
- **Postgres essential-0**: $5
- **Total per stage**: $10–12

Three stages: ~$30–36/month total. You can downgrade testing to Eco dynos any time:

```bash
heroku ps:type web=eco -a productsync-test
heroku ps:type web=basic -a productsync-prod
```

---

## When you actually need the worker dyno (later)

Right now product import runs inside the web request via a Python thread. Heroku web dynos have a 30-second request timeout, so for stores beyond ~500 products the import won't finish before the dyno kills the request — though it will still commit each completed page.

When real users hit that limit:

```bash
heroku addons:create heroku-redis:mini -a productsync-prod   # ~$15/mo
heroku ps:scale worker=1 -a productsync-prod
```

…then swap the Python thread in `addons_ui/routes.py` for an RQ enqueue (`get_queue().enqueue(...)`). The `SyncJob`-based progress model already works the same way regardless of which executor runs the import.

---

## Custom domains (when you're ready)

```bash
heroku domains:add productsync.com -a productsync-prod
heroku domains:add staging.productsync.com -a productsync-staging
heroku certs:auto:enable -a productsync-prod
```

Heroku will print the DNS targets to point your A/CNAME records at. Then update `PUBLIC_BASE_URL` to the new domain.

---

## Disaster recovery

```bash
# Backups
heroku pg:backups:capture -a productsync-prod
heroku pg:backups:download -a productsync-prod        # downloads latest.dump

# Restore
heroku pg:backups:restore latest -a productsync-prod  # from prod
heroku pg:backups:restore $(heroku pg:backups:url -a productsync-prod) DATABASE_URL -a productsync-staging   # copy prod -> staging
```

---

## Common first-deploy gotchas

- **"No web processes running"** — first deploy didn't include `Procfile`. Confirm `Procfile` is at the same level as `requirements.txt` in the deployed dir.
- **Release phase fails** — the `flask db upgrade` step needs `FLASK_APP=wsgi.py` to find the app. Set it: `heroku config:set FLASK_APP=wsgi.py -a productsync-test`.
- **`ENCRYPTION_KEY is not set`** at runtime — the setup script sets this; if you skipped the script, generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and `heroku config:set ENCRYPTION_KEY=...`.
- **Webhooks return 404** in WooCommerce — `PUBLIC_BASE_URL` is wrong. Check `heroku info -a <app>` for the actual URL.
