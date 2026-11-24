# LinkedIn Connect

Sends a daily batch of LinkedIn connection requests and can accept incoming ones. Everything runs in one Docker container, controlled from a web dashboard:

- **Settings:** search keywords, required and excluded headline keywords, invites per day, weekly cap, warm-up, auto-withdraw of old invites, auto-accept of incoming requests, daily schedule
- **Buttons:** Run now, Dry run, Stop, Accept all requests, Sign in to LinkedIn
- **What you see:** invites sent, accepted and withdrawn today, the last 7 days against your weekly cap, today's invites, run history with success or failure and the reason, and a **live view of the bot's browser**

> ⚠️ LinkedIn's User Agreement forbids automation tools, and accounts that use them can be restricted. The bot keeps volume low, waits between actions, and **stops** if LinkedIn shows a limit warning, a security check, a CAPTCHA or an HTTP 999/429. After a warning it pauses runs for 3 days. Use at your own risk.

## Start
Optional: set your timezone so the daily schedule runs at your local time. Create a `.env` file containing a line such as `TZ=Europe/London`. The file is kept out of git, and the default is UTC.

In Docker Desktop, or from a terminal in this folder, build and start the container:
```bash
docker compose up -d --build
```
Then open **http://localhost:8000**.

The ports are bound to `127.0.0.1` only, so the dashboard isn't reachable from other machines. The container restarts on its own whenever Docker Desktop starts.

### First time: sign in
1. Click **Sign in to LinkedIn**. LinkedIn's login page opens in the *Live browser* panel.
2. Sign in there, including any 2-step verification. The header shows **LinkedIn: signed in** once you're done.
3. Click **Dry run** and check the log to see who would be invited. Nothing gets clicked.

The session is kept in `data/browser_profile/`, and the bot never sees your password. If LinkedIn signs you out later, a run fails with "not logged in". Sign in again the same way.

## Settings (dashboard)
| Setting | What it does |
|---|---|
| Search keywords | One LinkedIn people search per line. The order is shuffled each run. |
| Headline must include | A person is invited only if their headline contains one of these (whole words). |
| Skip if headline contains | For example `student`, `intern`. |
| Invites per day / Weekly cap | LinkedIn allows about **100 a week**, so about 15–20 a day is the safe pace. |
| Warm-up | Starts at 5 a day and adds 5 a week until it reaches your daily number. |
| Withdraw old pending invites | Withdraws unanswered invites older than N days, up to 10 per run. |
| Accept all incoming requests on every run | Or click **Accept all requests** any time. **Preview** lists them without clicking. |
| Daily schedule | Runs at the set time, plus a random 0–30 minute delay, while the container is up. |

The dashboard saves your settings to `data/settings.json`, which overrides [`config.yaml`](config.yaml). The YAML file still holds advanced options such as delays, active hours and pause length.

## Data
Everything lives in `./data`, which is mounted into the container:
- `history.db`: invites, acceptances, withdrawals and run history
- `settings.json`, `state.json`, `scheduler.json`
- `browser_profile/`: your LinkedIn session. Keep it private.
- `logs/`: one log file per run

## Day-to-day commands
Follow the container logs:
```bash
docker compose logs -f
```

Stop the container:
```bash
docker compose down
```

Rebuild after changing the code:
```bash
docker compose up -d --build
```

Emergency stop: click **Stop**, or run `touch data/STOP`. Delete the file to allow runs again.

## When LinkedIn changes its pages
Selectors and button names live in one block at the top of [`linkedin_bot.py`](linkedin_bot.py) (`SELECTORS`, `BUTTON_NAMES`). LinkedIn renders action buttons slowly, about 15 seconds on search results. `page_load_timeout_seconds` in `config.yaml` controls how long the bot waits.

## Running without Docker (optional)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
```

Then sign in once, start a run, or start the dashboard:
```bash
.venv/bin/python linkedin_bot.py login
```
```bash
.venv/bin/python linkedin_bot.py run --dry-run
```
```bash
.venv/bin/uvicorn ui.server:app --port 8000
```
Without Docker, the live browser panel is unavailable.
