# Flight deal agent: Lima ⇄ Madrid

An agent that runs free on GitHub Actions, checks flight prices on a schedule, and
**emails you the deals** (sent from your own Gmail to yourself). No server and no monthly cost.

- **Trip:** LIM → MAD round trip, 1 adult, USD. Outbound Jan 27–Feb 6 2027
  (preferred Feb 1), return Mar 25–Apr 8 2027 (preferred Apr 1), at most 1 stop and
  30 h each way. Change any of this in `config.yaml`.
- **Price caps (total, bags included):** no bag $800, carry-on $1,000, checked bag $1,100.
- **Alerts:** price, baggage tier, airline, stops, durations, dates, % below usual,
  and a booking link. Daily summary at 08:00 Lima time.
- **Control it by email:** send yourself an email with the subject `flights status`,
  `flights pause` or `flights resume`. Spanish works too: `vuelos estado`, `vuelos pausa`,
  `vuelos reanudar`. Each alert also has buttons that open these emails pre-filled, so you
  only press Send.

---

## 1. How it works (2-minute version)

| Source | What it gives | Cost |
|---|---|---|
| **Google Flights via SerpApi** | Live prices, plus bag prices and return-leg details for promising fares | Free plan: 250 searches/month |
| **Aviasales Data API (Travelpayouts)** | Cached prices from other travelers' searches for **all 165 date pairs** every scan | Free |
| **Secret Flying RSS feed** | Error fares and flash sales mentioning Lima/Peru → Madrid/Spain (or "error fare" → Europe) | Free |

APIs from the original brief that couldn't be used:

- **Amadeus Self-Service** shut down on 17 July 2026 (all keys disabled).
- **Kiwi Tequila** is invitation-only for new users.
- **Going** only covers US departure airports and has no public feed.

To add a new source later, see `agent/sources/__init__.py`.

**Saving Google searches.** 250 searches can't cover 165 date pairs every 2 hours, so:

1. The free Aviasales cache covers every date pair on every scan.
2. Google searches are spread evenly through the month. Most go to your preferred dates:
   on the free plan that's about **40 live checks/month** for Feb 1 → Apr 1, 10–15 for
   dates 1 day away, and about 45 different date pairs per month in total.
3. A cheap **cached** price is never alerted straight away. It jumps to the front of
   the queue for a live Google check. If the live price confirms it, you get the alert.
   If no live check happens within 12 h, you get it labeled "cached price".
4. For a promising fare the agent spends 2 extra searches to get the **return leg** and
   the **bag prices** from Google's booking page. It does at most 2 of these per run.

**Baggage rules.** Every fare gets a no-bag price. It also gets a carry-on and/or
checked-bag price **only when Google shows what bags cost or include**: fare + bag fee
×2, since the fee is charged on each direction. If the baggage info is missing, the fare
only competes against the no-bag cap and is marked **"baggage unverified"**. A no-bag fare
is never compared with the checked-bag cap. You can add known bag fees per airline under
`bag_fees:` in `config.yaml`. Those prices are labeled "est.".

**When you get an alert.** Any baggage tier meets one of these:

- (a) it's under your cap
- (b) it's ≥20% below the usual price
- (c) it looks like an error fare (under 55% of usual, or under $450 with no bag)
- (d) it suddenly dropped ≥10% (and ≥$60) since the last check, and is no more than 25% above your cap

"Usual" means the median of the last 30 days of prices for that baggage tier. Until
there are 20 data points, Google's own "typical price" is used instead.

**No repeats.** The same dates and baggage tier only alert again if the price drops at
least $20 (or 2%) more. When prices are similar, dates closer to Feb 1 / Apr 1 rank
higher (each day away counts as $6).

**Schedule.** The workflow wakes up every 30 minutes. It checks your inbox for command
emails every time, but only runs a full scan when one is due:

| Time before Jan 27 | Scan every |
|---|---|
| More than 60 days | 120 min |
| 31–60 days | 90 min |
| 15–30 days | 60 min |
| 14 days or less | 30 min |

**Storage.** Price history and state live in `data/state.json`, committed back to the
repo after each run. JSON instead of SQLite because it's small, readable in the
browser, and GitHub shows a clear diff for every run. Git can't merge a binary SQLite
file, so JSON also avoids that problem.

---

## 2. Setup, step by step (about 30 minutes)

### Step 1: Create a Gmail app password (about 5 minutes)

The agent sends email from your Gmail account to itself. Google requires a special
16-character **app password** for this. Your normal password won't work, and the app
password can't be used to sign in to your account in a browser.

1. Go to **https://myaccount.google.com/security** and check that **2-Step Verification**
   is **On**. If it's off, click it and follow the steps (Google texts you a code).
2. Go to **https://myaccount.google.com/apppasswords**. Google hides this page if
   2-Step Verification is off.
3. In **App name** type `Flight agent` and click **Create**.
4. Google shows a code like `abcd efgh ijkl mnop`. Copy it to a note. This is
   `EMAIL_APP_PASSWORD`. **Don't paste it here in the chat:** it goes straight into
   GitHub in Step 5. Spaces don't matter.
5. Optional: in Gmail, create a filter for emails with the subject `[Flights]` and give them
   a label, or mark them as important, so deals stand out.

**Want the alerts at another address** (Hotmail, Outlook, work email…)? Add the optional
`EMAIL_TO` secret in Step 5 with that address, or several separated by commas. The Gmail
account only *sends*. Tip: you can create a fresh Gmail just for the agent, so your main
account needs no app password. Command emails ("flights pause"…) must be sent **to the
sending Gmail address**, from it or from any `EMAIL_TO` address. The buttons in each alert
already do this.
For Outlook or Yahoo, change `smtp_host` and `imap_host` in `config.yaml`.

### Step 2: Get a SerpApi key (Google Flights)

1. Sign up at **https://serpapi.com/users/sign_up** and choose the **Free plan** (250 searches/month).
2. Confirm your email (and phone, if asked).
3. In the dashboard, open **Api Key** (https://serpapi.com/manage-api-key) and copy
   "Your Private API Key". This is `SERPAPI_KEY`.

### Step 3: Get a Travelpayouts token (Aviasales cached prices)

1. Sign up at **https://www.travelpayouts.com** (free affiliate account). If it asks
   where you'll promote it, a short honest description works, e.g. "personal travel
   price monitor".
2. Go to **Programs**, find **Aviasales**, and click **Join**/**Connect**.
3. Open your **Profile**, find the **API token** section, and copy the token.
   This is `TRAVELPAYOUTS_TOKEN`.

If you skip this step, the agent still works on Google Flights + feeds, just with less coverage.

### Step 4: Create the GitHub repository

1. Sign in at https://github.com → **+** (top right) → **New repository**.
2. Name it, e.g. `flight-deal-agent`. **Public is recommended**: GitHub Actions minutes
   are unlimited for public repos. Your keys stay hidden in Secrets, but your config
   (route, dates, caps) and price history will be visible to anyone. If you prefer
   **Private**, change the cron in `.github/workflows/monitor.yml` to `"0 * * * *"`
   (hourly) to stay inside the 2,000 free minutes/month.
3. Click **Create repository**.
4. Upload the files. The **easiest reliable way** is with git on your computer:
   ```bash
   cd flight-deal-agent          # the folder from the zip
   git init -b main
   git add .
   git commit -m "Flight deal agent"
   git remote add origin https://github.com/<your-user>/flight-deal-agent.git
   git push -u origin main
   ```
   Without git: on the empty repo page click **uploading an existing file** and drag in
   **everything inside** the folder. Then check that `.github/workflows/monitor.yml`
   appears in the repo. Mac Finder hides folders starting with a dot: press
   `Cmd+Shift+.` to show them before dragging.

### Step 5: Add the secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four, one at a time (name exactly as written). Optionally add a fifth,
`EMAIL_TO`, with the address(es) where you want to receive the alerts, e.g.
`mary@hotmail.com` or `mary@hotmail.com, other@gmail.com`:

| Name | Value |
|---|---|
| `EMAIL_ADDRESS` | your Gmail address, e.g. `you@gmail.com` |
| `EMAIL_APP_PASSWORD` | the 16-character app password from Step 1 |
| `SERPAPI_KEY` | SerpApi private key |
| `TRAVELPAYOUTS_TOKEN` | Travelpayouts API token |

Then go to **Settings → Actions → General → Workflow permissions**, select **Read and
write permissions**, and click **Save**. This lets the agent save its price history.

### Step 6: Test it

1. Open the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **flight-deal-monitor** → **Run workflow** → mode **test-alert** → **Run workflow**.
   Within about a minute you should get an email "[Flights] 🧪 Test alert: your flight agent works".
   Check Spam the first time, and mark it **Not spam** if it landed there.
3. Run it again with mode **dry-run**. This does a real scan, but prints the messages in
   the run log instead of sending them. Click the run → **run** → **Run agent** to see them.
4. Run it once with mode **force-scan**. This is the first real scan.

From then on it runs by itself. To check on it, email yourself `flights status`. The
reply arrives the next time the agent wakes up, within about 30 minutes. For an
instant reply, use **Run workflow** with mode **status**.

---

## 3. Everyday use

- **Change dates, caps, or rules:** edit `config.yaml` on GitHub (pencil icon) and commit.
  The next run uses the new values.
- **Pause / resume:** email yourself `flights pause` / `flights resume`, use the buttons in
  any alert, or **Run workflow** with mode pause/resume. While paused, there are no scans,
  alerts or summaries.
- **See history:** open `data/state.json`. It has `history` (price points), `latest`
  (current price per date pair and tier), and `alerts_sent`.
- **Run on your computer** (optional):
  ```bash
  pip install -r requirements-dev.txt
  python -m agent.main --demo        # offline demo with sample data, sends nothing
  pytest -q                          # 41 tests
  export EMAIL_ADDRESS=... EMAIL_APP_PASSWORD=... SERPAPI_KEY=... TRAVELPAYOUTS_TOKEN=...
  python -m agent.main --dry-run --force-scan   # real scan, print instead of send
  python -m agent.main --test-alert
  ```
- **Upgrade later:** on a paid SerpApi plan, raise `sources.serpapi.monthly_budget` and
  `max_bucket`. Nothing else changes.
- **After your trip:** Actions tab → flight-deal-monitor → **⋯** → **Disable workflow**.

---

## 4. What can break and how to fix it

The agent emails you a warning when a source fails 6 runs in a row. The other
sources keep working, and every run's log is in the **Actions** tab.

| Symptom | Likely cause | Fix |
|---|---|---|
| Run is red with "login rejected (535)" | Wrong `EMAIL_ADDRESS`, or the app password is wrong/revoked, or you used your normal password | Create a new app password (Step 1) and update the secret |
| No email arrives but the run is green | The email went to Spam or Promotions | Search Gmail for `[Flights]` and mark it **Not spam**; add a filter |
| Command emails are ignored | Subject doesn't start with `flights` (or `vuelos`), or it was sent from a different address | Use the buttons in an alert, or send from `EMAIL_ADDRESS` / `EMAIL_TO` |
| Commands are slow to take effect | Inbox checked every 30 min, and GitHub sometimes delays scheduled runs 15–60 min | Use **Run workflow** with mode status/pause/resume for an instant result |
| You changed your Google password and alerts stopped | Google revokes app passwords when the account password changes | Create a new app password and update the secret |
| Source `google_flights` failing: "Invalid API key" | Key wrong or account not verified | Check the key at serpapi.com/manage-api-key; update the secret |
| Google stops being used near month end; summary shows 0 searches left | Monthly quota used (e.g. by manual tests) | Wait for the monthly reset, or lower `monthly_budget` |
| Source `aviasales_cache` failing with 401 | Token wrong, or Aviasales program not joined | Redo Step 3 |
| Aviasales finds 0 date pairs | Few people searched this route recently (the cache is sparse) | Harmless. Optionally add markets in `travelpayouts.markets` |
| Source `deal_feeds` failing with 403 | Secret Flying blocking GitHub's servers | Harmless (the APIs still run). Remove the URL or set `feeds.enabled: false` |
| Carry-on/checked tiers empty in the summary | Google hasn't shown bag prices for those fares yet | Fills in as fares get enriched. Or add known fees under `bag_fees:` |
| Run is red: "Could not push data/state.json" | Two runs overlapped, or write permission missing | Check Step 5's permission setting; the next run usually fixes itself |
| No runs for a long time | GitHub disables schedules in repos with no activity for 60 days, or a billing/minutes limit on a private repo | Actions tab → re-enable the workflow; for private repos see Step 4.2 |
| Alerts stop, `data/state.corrupt.json` appears | State file got damaged (e.g. a bad manual edit) | The agent already started fresh; delete the corrupt copy |
| SerpApi or Travelpayouts changes its response format | API update | The source shows as failing or finds 0 fares; update the parser in `agent/sources/<name>.py` |

---

## 5. Files

```
config.yaml                    all settings (no secrets)
.github/workflows/monitor.yml  schedule + run + save history
agent/main.py                  one run: commands → scan → alerts → summary → save
agent/planner.py               when to scan, Google quota, which dates to search
agent/deals.py                 deal rules, no-repeat logic, ranking
agent/baggage.py               baggage tiers and bag fees
agent/messages.py              message texts
agent/notify_email.py          Gmail sending (retries) + reading your command emails
agent/storage.py               data/state.json
agent/sources/                 serpapi_google.py, travelpayouts.py, feeds.py (each fails independently)
tests/                         unit tests + sample API responses
```
