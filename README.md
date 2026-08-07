# The Daily Brief — template

A personalized morning news briefing, generated end-to-end by GitHub Actions: web research
across your configured sources, Claude-written synthesis, a styled HTML brief, optional
text-to-speech audio, and email delivery — in your inbox before you wake up.

This is the **template** (public) edition of a running personal deployment. Everything
personal — recipient, sender domain, weather locations, calendar, reading queue — enters
through **Actions secrets and environment variables**, never through committed files. Fork
it, set your secrets, and it runs.

## What it does

Every scheduled run the pipeline:

1. Checks your calendar (`CALENDAR_ICS_URL`) to find your local timezone and only proceeds
   at your configured hour, wherever you are.
2. Researches each source/category in `config.json` within a word budget.
3. Writes the brief with the Anthropic API, runs it through a prose-quality linter
   (`brief_quality.py`), and renders `templates/brief_template.html`.
4. Optionally narrates it to MP3 (Deepgram) and commits the audio so the email can link it.
5. Emails the result (Resend) to `RECIPIENT_EMAILS`.

A "Deep Dive" section can surface your reading/listening queue — it is read from a
**private** store via `PERSONAL_DATA_DIR` and is stripped from anything committed to the
repo. Never track personal queue state in a public repo; the parent project once did, and
un-publishing it took a history rewrite, a support ticket, and still left a fork behind.

## Setup

1. Fork (or use as a template).
2. Repo secrets: `ANTHROPIC_API_KEY` (required); `RESEND_API_KEY`, `RECIPIENT_EMAILS`,
   `SENDER_FROM`, `RECIPIENT_NAME`, `DEEPGRAM_API_KEY`, `CALENDAR_ICS_URL`,
   `WEATHER_LOCATION_HOME`, `WEATHER_LOCATION_WORK` (all optional — features degrade
   gracefully when unset).
3. Edit `config.json`: your sources, categories, and word budget.
4. Test with a manual run: Actions → *Daily Brief* → *Run workflow* with `dry_run: true`
   (full generation, no email/audio/commits; output lands as a run artifact).
5. Go live: uncomment the `schedule:` block in `.github/workflows/daily-brief.yml`.

## Guard rails included

- `pii-public-scan.yml` — every push is scanned for email/phone/SSN-shaped strings
  (masked output). This repo is public; treat every committed byte as published.
- The generator **fails closed** on the personal-data mount: unset or missing means the
  Deep Dive section is omitted with a warning, never guessed.
- Dry-run artifacts contain your personal sections — don't print or publish them
  (the parent project removed its artifact-printing job for exactly this reason).

## Layout

```
generate_brief.py     the pipeline (research → synthesis → render → audio → email)
brief_quality.py      prose-quality linter (see NOTICE for adapted parts)
config.json           sources, categories, word budget — the example set
templates/            HTML template for the brief + email
test_quality.py       linter tests
test_schedule.py      travel-aware scheduling tests
.github/workflows/    daily-brief.yml (the run), tests.yml, pii-public-scan.yml
```

## License

MIT — see `LICENSE`. Third-party attributions in `NOTICE`.
