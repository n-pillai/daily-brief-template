# The Daily Brief — template

A personalized morning news briefing that runs itself. Every morning GitHub researches the news
from the sources you choose, Claude writes it up as a short brief, and it lands in your inbox
around 5am your time — optionally with an audio version you can listen to.

You don't need to be a programmer to set this up. You need a free GitHub account, about 30–45
minutes, and a few accounts with the services listed below. Everything personal (your email,
your city, your calendar) goes into GitHub's private **secrets** settings, never into the files
themselves.

---

## Start here

### What you'll need

| Service | What it does | Required? | Cost |
|---|---|---|---|
| [GitHub](https://github.com) | Runs the brief on a schedule | Yes | Free |
| [Anthropic](https://console.anthropic.com) | Claude researches and writes the brief | Yes | Pay per use — you add credit to your account |
| [Resend](https://resend.com) | Sends the email | Yes, to get an email | Has a free plan |
| [Deepgram](https://deepgram.com) | Reads the brief aloud as an MP3 | No | Free credit when you sign up |
| A calendar's private address (e.g. Google Calendar) | Lets the brief follow you when you travel | No | Free |

**How much will it cost?** The Anthropic charge depends on how many sections and sources you
keep. Do the test run in step 5, then look at **Usage** in the Anthropic console to see what one
brief cost you, and multiply by 30 for a month. You can set a monthly spending limit in the
console so there are no surprises.

### Step 1 — Make your own copy

1. Sign in to GitHub and open this repository's page.
2. Click the green **Use this template** button → **Create a new repository**. (Don't use
   **Fork**: a fork of a public repo can't be made private.)
3. Give it a name, and choose **Public** or **Private**. Read
   [Public or private?](#public-or-private) below before you decide.
4. Click **Create repository**. From now on, every step happens in *your* copy.

### Step 2 — Get your keys

A "key" is a long password that lets GitHub use a service on your behalf. Copy each one somewhere
safe for a few minutes — you'll paste them into GitHub in step 3.

- **Anthropic (required):** sign in at [console.anthropic.com](https://console.anthropic.com) →
  **API Keys** → **Create Key**. Add some credit under **Billing**.
- **Resend (for email):** sign up at [resend.com](https://resend.com) → **API Keys** →
  **Create API Key**. See [Sending email with Resend](#sending-email-with-resend) — there's one
  catch about which address you can send from.
- **Deepgram (optional, for audio):** sign up at [deepgram.com](https://deepgram.com) → create an
  API key in the console.
- **Calendar (optional, for travel):** in Google Calendar, open **Settings** → your calendar →
  **Integrate calendar** → copy the **Secret address in iCal format**.

### Step 3 — Add your secrets to GitHub

In your copy of the repository:

1. Click **Settings** (top of the page) → **Secrets and variables** (left side) → **Actions**.
2. Click **New repository secret**. Type the **Name** exactly as shown below, paste the value,
   and click **Add secret**.
3. Repeat for each secret you want to use.

| Name | What to paste | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic key | `sk-ant-…` |
| `RESEND_API_KEY` | Your Resend key | `re_…` |
| `RECIPIENT_EMAILS` | Who gets the brief (separate several with commas) | `you@example.com` |
| `SENDER_FROM` | The "from" line of the email | `The Daily Brief <brief@example.com>` |
| `RECIPIENT_NAME` | Your first name, used in the greeting | `Sam` |
| `HOME_TIMEZONE` | Your timezone, from [this list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (the "TZ identifier" column) | `Europe/London` |
| `WEATHER_LOCATION_HOME` | Your home city, for the weather | `London, UK` |
| `WEATHER_LOCATION_WORK` | Your work city, if different | `Cambridge, UK` |
| `DEEPGRAM_API_KEY` | Your Deepgram key | — |
| `CALENDAR_ICS_URL` | Your calendar's secret iCal address | `https://calendar.google.com/…/basic.ics` |

Only `ANTHROPIC_API_KEY` is strictly required. Anything you leave out is simply skipped — no
audio without Deepgram, no weather without a city, no travel without a calendar.

- **Email takes three secrets together:** `RESEND_API_KEY`, `RECIPIENT_EMAILS` and `SENDER_FROM`.
  Set all three to get the email, or none of them to only save the brief in your repository. If
  only some are set, the run stops straight away (before spending anything) and names the
  missing ones.
- **Do set `HOME_TIMEZONE`**: without it the brief assumes you live in US Pacific time.

### Step 4 — Choose your news

Open `config.json` in your copy, click the ✏️ pencil icon to edit, and change it to suit you.
The file that ships here is just an example — one person's sources and outlets — so replace them
with your own.

What you can change safely:

- `sources` → the publications and podcasts to draw from (`core`, `subscriptions`,
  `explore_pool`, `podcasts`).
- `trusted_sources` → for each section, the outlets its stories are allowed to come from. For
  example, swap the outlets under `"india"` for the ones you trust.
- In each line under `categories`: `name` (the heading shown in the brief), `weight` (how much
  space the section gets — higher means more) and `freshness_hours` (how recent its stories must
  be).
- `word_budget` → `reading_minutes` is how long the brief should take to read.

What you can't change from this file: the brief always has the same six sections — World, India,
Tech, Business, Science and Sports — plus a "worth exploring" pick. **Don't delete a section or
change its `id`**, or the run will fail. To make a section smaller, lower its `weight`. Adding,
removing or renaming sections means editing `generate_brief.py`.

Click **Commit changes** when you're done. Keep the commas, quotes and brackets exactly as they
are — if the file is broken, the next run will fail.

The **sports** roundup (yesterday's NBA scores and a list of sports in season, including cricket)
is also built into `generate_brief.py`.

### Step 5 — Do a test run

1. Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and
   enable them**.
2. Click **Daily Brief** on the left → **Run workflow** (on the right).
3. Set **dry_run** to `true` and click the green **Run workflow** button.
4. Wait a few minutes. A green ✓ means it worked. Click the run, scroll to **Artifacts**, and
   download **dry-run-brief** to see your brief.

A dry run does the full research and writing but sends no email, makes no audio and saves
nothing to the repository. The download contains your personal sections, so don't share it.

If you get a red ✗, click the run, then the failed step, and read the last few lines — they
usually name the missing secret or the problem.

### Step 5b — Send yourself a real one

The dry run doesn't touch email or audio, so check those now rather than at 5am tomorrow:

1. **Actions** → **Daily Brief** → **Run workflow**.
2. Leave **dry_run** at `false`, set **force** to `true`, and click **Run workflow**.
3. Within a few minutes the email should arrive (check spam the first time), and a new file
   appears in the `briefs/` folder.

`force` skips the wait for 5am. A forced run counts as today's brief, so the scheduled run won't
send a second one today.

### Step 6 — Turn on the daily schedule

1. Open `.github/workflows/daily-brief.yml` and click the ✏️ pencil icon.
2. Find these two lines near the top:
   ```yaml
   # schedule:
   #   - cron: "0 * * * *"
   ```
3. Delete the `#` and the space after it at the start of **both** lines, leaving the indenting
   as it is:
   ```yaml
   schedule:
     - cron: "0 * * * *"
   ```
4. Click **Commit changes**.

That's it. GitHub now checks every hour and sends the brief once it's 5am where you are
(GitHub's scheduler often runs late, so expect it between about 5:00 and 6:00). To pause it, put
the two `#` back.

**What success looks like:** a "Daily Brief" email each morning, and a new file in the `briefs/`
folder of your repository each day. GitHub keeps the last 7 days and deletes older ones
automatically.

---

## Good to know

### Public or private?

Each day's brief (and its audio, if you use Deepgram) is saved into your repository's `briefs/`
folder. That brief includes your name and your local weather.

- **Private repository:** nobody else can see your briefs. But the "Listen" link in the email
  won't play, because it points at a file only you can open.
- **Public repository:** the Listen link works, but anyone can read your daily briefs.

If you don't need the audio, choose **Private**.

### Sending email with Resend

Resend only sends from email addresses it can verify are yours.

- **If you own a domain** (like `example.com`): add it under **Domains** in Resend and follow
  its steps. Then set `SENDER_FROM` to something like `The Daily Brief <brief@example.com>`, and
  you can send to any address.
- **If you don't:** set `SENDER_FROM` to `The Daily Brief <onboarding@resend.dev>`, and set <!-- [pii-allow: email] -->
  `RECIPIENT_EMAILS` to the same email address you signed up to Resend with. Resend's test
  sender only delivers to that address.

### Travelling

If you add `CALENDAR_ICS_URL`, the brief looks for an event in your calendar today that lasts two
days or more and has a city in its location (for example, "Trip to Paris" with location "Paris").
It then delivers at 5am *there*. Update your calendar and the brief follows you.

### The Deep Dive section (advanced, off by default)

The template can also show a "Deep Dive" box with one thing to read and one thing to listen to,
drawn from a reading list you keep somewhere private. **It is off unless you set it up, and the
brief works fine without it.**

To use it, you need a second, private place for the list: the generator reads
`private/daily-brief/deep_dive_state.json` inside the folder named by the `PERSONAL_DATA_DIR`
environment variable. The workflow does not set that variable, so you'd have to add a step to
`daily-brief.yml` that fetches your private list and sets it. Never put your reading list in this
repository's files. The file looks like this:

```json
{
  "current_read":   {"title": "…", "url": "https://…", "source": "…", "first_shown": "2026-01-15"},
  "current_listen": {"title": "…", "url": "https://…", "source": "…", "first_shown": "2026-01-15"}
}
```

---

## How it works

Every hourly run:

1. Checks the time where you are (your `HOME_TIMEZONE`, or where your calendar says you're
   travelling) and only carries on once it's 5am there. The hour is `TARGET_LOCAL_HOUR` in
   `generate_brief.py`.
2. Researches each section in `config.json` within the word budget.
3. Writes the brief with Claude, checks the writing with a style linter (`brief_quality.py`),
   and lays it out using `templates/brief_template.html`.
4. Optionally reads it aloud to an MP3 (Deepgram) and saves it so the email can link to it.
5. Emails it (Resend) to `RECIPIENT_EMAILS`.

## Guard rails included

- `pii-public-scan.yml` — every push is checked for anything shaped like an email address, phone
  number or Social Security number, so personal details don't get committed by accident. If it
  fails, look at the run's log for the file and line.
- The generator **fails closed** on `PERSONAL_DATA_DIR`: if it's unset or missing, the Deep Dive
  section is left out with a warning, never guessed.
- Dry-run downloads contain your personal sections — don't print or publish them.

## Files

```
generate_brief.py     the pipeline (research → writing → layout → audio → email)
brief_quality.py      writing-style linter (see NOTICE for adapted parts)
config.json           your sources, sections and word budget (an example set to start)
templates/            layout for the brief
test_quality.py       linter tests (run automatically)
test_regressions.py   checks that sections don't bleed into or repeat each other (run automatically)
test_schedule.py      travel-aware scheduling check — run by hand: python test_schedule.py [YYYY-MM-DD]
.github/workflows/    daily-brief.yml (the daily run), tests.yml, pii-public-scan.yml
```

## License

MIT — see `LICENSE`. Third-party attributions in `NOTICE`.
