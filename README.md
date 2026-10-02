# insta-tapes 🎛️

Instagram profile → **video transcripts** (Groq `whisper-large-v3`, turbo fallback)
+ **all comments with replies** → two big text files.

Built for running **only on GitHub Actions** (clean Azure IP fetches anon IG pages;
our own egress is login-walled). Nothing runs locally, ever.

## Outputs (download from the run's artifacts)

- `ANDROO_TAPES.txt` — per post: link, date, caption, full video transcript
- `ANDROO_CHATTER.txt` — per post: every comment + replies (threaded)
- `manifest.json` — coverage stats (posts / transcripts / comments collected vs declared)
- `run.log` — step-by-step strategy log (which IG fallback paths fired)

## How to run (re-run anytime)

Actions → **harvest** → Run workflow → paste profile URL (default = the androo.agi
share link) → artifacts appear when done.

The workflow is **dispatch-only**: there is no cron, nothing stays running.
(Groq key lives in repo secrets, set once.)

## Layout

- `pipeline.py` — the whole bot (profile grid → post pages → whisper → outputs)
- `.github/workflows/harvest.yml` — temp runner, dispatch-only
- Local bare mirror (survives GitHub): `/storage/emulated/0/HERMES/bots/mirrors/insta-tapes.git`
