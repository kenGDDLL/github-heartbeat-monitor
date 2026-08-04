# Telegram Device Heartbeat Monitor (GitHub Actions edition)

Polls a Telegram bot's chat every 30 minutes for `TG <id> is alive` heartbeat
messages from 6 standalone networks, flags a device DOWN once it's overdue by
more than the grace threshold, emails an alert on down-transitions, and
publishes a status dashboard via GitHub Pages.

This runs entirely on GitHub's infrastructure — no dependency on Claude/Cowork
being open, and no network-allowlist issues, since GitHub Actions has normal
internet access.

## What's in here

- `scripts/sync.py` — the poller/alerter. Reads Telegram, updates `state.json`,
  rewrites `docs/index.html`'s embedded data, emails on down-transitions.
- `.github/workflows/heartbeat-sync.yml` — runs `sync.py` every 30 minutes and
  commits the result back to the repo.
- `docs/index.html` — the dashboard itself (served by GitHub Pages once enabled).
- `state.json` — current known status per device, plus bookkeeping (last
  processed Telegram update id, last poll time/result).

## One-time setup

1. **Create a repo** on GitHub (public is easiest — public repos get unlimited
   free Actions minutes; private repos get 2,000 free minutes/month, which
   this easily fits at 30-min intervals, but public avoids the question
   entirely). Push everything in this folder to it.

2. **Add repo secrets** — Settings → Secrets and variables → Actions → New
   repository secret:
   - `TELEGRAM_BOT_TOKEN` — your bot's token
   - `TELEGRAM_CHAT_ID` — `-1003859236761` (the group chat id)
   - `SMTP_HOST` — e.g. `smtp.gmail.com` (see "Email options" below)
   - `SMTP_PORT` — usually `587`
   - `SMTP_USER` — the mailbox that sends the alert
   - `SMTP_PASS` — its password / app password
   - `ALERT_EMAIL_FROM` — usually same as `SMTP_USER`
   - `ALERT_EMAIL_TO` — `kenneth_LUM@csit.gov.sg` (or wherever you want alerts)

   If you skip the SMTP secrets, the workflow still runs and updates the
   dashboard — it just logs the alert instead of emailing it, so you can wire
   up email later without anything breaking now.

3. **Enable GitHub Pages** — Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, folder: `/docs`. After a minute or two you'll get
   a public URL like `https://<your-username>.github.io/<repo-name>/`.

4. **Trigger the first run** — go to the Actions tab → "Telegram heartbeat
   sync" → "Run workflow" (this is the `workflow_dispatch` trigger baked into
   the workflow, so you don't have to wait for the schedule). Check the run
   logs; if Telegram/email are configured correctly you should see either
   "No new heartbeats this cycle." or heartbeats being processed.

5. **Adjust the schedule if you want** — the cron in
   `.github/workflows/heartbeat-sync.yml` is `*/30 * * * *` (every 30 min).
   GitHub's actual minimum granularity is 5 minutes, but schedules can lag by
   several minutes under load, so don't go tighter than you need — 15–30 min
   is plenty given heartbeats only arrive every 4 hours.

## Email options

Any standard SMTP account works. A few common choices:

- **Gmail** — `smtp.gmail.com`, port `587`. Requires a Google Account
  **App Password** (Google Account → Security → 2-Step Verification → App
  passwords) since Gmail no longer accepts your normal login password for
  SMTP from scripts.
- **Your organization's mail relay** — if csit.gov.sg has an internal SMTP
  relay, that avoids using a personal Gmail account. Ask your IT team for the
  host/port and whether it needs auth.
- **A transactional email API** (SendGrid, Resend, etc.) — these have generous
  free tiers and simpler auth, but would need a small code change in
  `send_alert()` in `scripts/sync.py` to use their API instead of SMTP. Ask if
  you'd like this swapped in.

## Adjusting behavior

- **Down threshold / heartbeat interval** — set via the `THRESHOLD_MINUTES`
  and `HEARTBEAT_INTERVAL_MINUTES` secrets (or env vars if you run it
  manually) — defaults are 270 and 240 (4h30m / 4h), matching what's
  configured today.
- **Alerting on recovery too** — right now it only alerts when a device goes
  DOWN, not when it comes back UP. Easy to add in `scripts/sync.py` if wanted
  — look at the `newly_down` list in `main()` for the pattern to copy.
- **Repeat reminders while still down** — currently it alerts once on the
  transition and stays silent while the device remains down (to avoid alert
  fatigue). If you'd rather get reminded every cycle until it's fixed, that's
  a one-line change to the alert condition in `main()`.

## How this connects back to the Cowork dashboard

Once this repo is live, tell Claude (in your Cowork session) the repo's
`owner/name` — it can then point the existing hourly Cowork scheduled task at
this repo's raw `state.json` (via
`https://raw.githubusercontent.com/<owner>/<repo>/main/state.json`, which this
sandbox can reach even though it can't reach Telegram directly) so the Cowork
artifact mirrors the same data. Alerting itself stays owned by this repo's
email step — the Cowork side would just be a read-only mirror for viewing.
