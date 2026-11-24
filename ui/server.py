"""Web dashboard for the LinkedIn connection bot.

Runs the bot as a subprocess (one at a time), edits settings, shows stats and
live logs, and runs the daily schedule. Start with:
    uvicorn ui.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import linkedin_bot as lb  # noqa: E402  (uses DATA_DIR for all paths)

CONFIG_PATH = ROOT / "config.yaml"
SCHED_PATH = lb.DATA / "scheduler.json"
STATIC = Path(__file__).resolve().parent / "static"
lb.LOG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LinkedIn Connect")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
class Schedule(BaseModel):
    enabled: bool = False
    time: str = Field("10:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays_only: bool = True


class Settings(BaseModel):
    search_keywords: List[str] = []
    include_keywords: List[str] = []
    exclude_keywords: List[str] = []
    use_search: bool = True
    use_suggested: bool = True
    invites_per_day: int = Field(5, ge=1, le=100)
    weekly_cap: int = Field(100, ge=1, le=200)
    warmup_enabled: bool = True
    withdraw_enabled: bool = True
    withdraw_days: int = Field(21, ge=7, le=365)
    accept_on_run: bool = False
    schedule: Schedule = Schedule()


def read_overrides() -> dict:
    if lb.SETTINGS_PATH.exists():
        return json.loads(lb.SETTINGS_PATH.read_text() or "{}")
    return {}


def effective_config() -> dict:
    return lb.load_config(CONFIG_PATH)


def to_settings(cfg: dict) -> Settings:
    ui = cfg.get("ui") or {}
    sources = cfg.get("sources") or []
    kw = (cfg.get("search") or {}).get("keywords") or []
    f = cfg.get("filters") or {}
    wd = int(cfg.get("withdraw_pending_after_days") or 0)
    return Settings(
        search_keywords=[kw] if isinstance(kw, str) else list(kw),
        include_keywords=list(f.get("include_keywords") or []),
        exclude_keywords=list(f.get("exclude_keywords") or []),
        use_search="search" in sources,
        use_suggested="suggested" in sources,
        invites_per_day=int(cfg.get("daily_limit_max", 5)),
        weekly_cap=int(cfg.get("weekly_cap", 100)),
        warmup_enabled=bool((cfg.get("warmup") or {}).get("enabled", True)),
        withdraw_enabled=wd > 0,
        withdraw_days=wd if wd > 0 else int(ui.get("withdraw_days", 21)),
        accept_on_run=bool(cfg.get("accept_invites_on_run")),
        schedule=Schedule(**(ui.get("schedule") or {})),
    )


def write_settings(s: Settings) -> None:
    clean = lambda xs: [x.strip() for x in xs if x and x.strip()]  # noqa: E731
    sources = [name for name, on in (("search", s.use_search), ("suggested", s.use_suggested)) if on]
    if not sources:
        raise HTTPException(400, "Pick at least one source (search or suggested).")
    if s.use_search and not clean(s.search_keywords):
        raise HTTPException(400, "Add at least one search keyword, or turn off search.")
    over = read_overrides()
    was_enabled = bool(((over.get("ui") or {}).get("schedule") or {}).get("enabled"))
    if s.schedule.enabled and not was_enabled:
        # Turning the schedule on after today's time has passed: start tomorrow.
        hh, mm = map(int, s.schedule.time.split(":"))
        if datetime.now() >= datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0):
            SCHED_PATH.write_text(json.dumps({"last_date": date.today().isoformat(), "started": None}))
    over.update(
        {
            "sources": sources,
            "search": {**(over.get("search") or {}), "keywords": clean(s.search_keywords)},
            "filters": {
                **(over.get("filters") or {}),
                "include_keywords": [k.lower() for k in clean(s.include_keywords)],
                "exclude_keywords": [k.lower() for k in clean(s.exclude_keywords)],
            },
            "daily_limit_min": s.invites_per_day,
            "daily_limit_max": s.invites_per_day,
            "weekly_cap": s.weekly_cap,
            "warmup": {**(over.get("warmup") or {}), "enabled": s.warmup_enabled},
            "withdraw_pending_after_days": s.withdraw_days if s.withdraw_enabled else 0,
            "accept_invites_on_run": s.accept_on_run,
            "ui": {"withdraw_days": s.withdraw_days, "schedule": s.schedule.model_dump()},
        }
    )
    lb.SETTINGS_PATH.write_text(json.dumps(over, indent=2))
    # A new per-day number should apply today, not tomorrow.
    state = lb.load_state()
    if (state.get("target") or {}).get("date") == date.today().isoformat():
        state.pop("target")
        lb.save_state(state)


@app.get("/api/settings")
def get_settings() -> Settings:
    return to_settings(effective_config())


@app.put("/api/settings")
def put_settings(s: Settings) -> Settings:
    write_settings(s)
    return to_settings(effective_config())


# ---------------------------------------------------------------------------
# Running the bot (one process at a time)
# ---------------------------------------------------------------------------
class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.kind = ""
        self.started = ""
        self.log_path: Optional[Path] = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, kind: str, args: List[str]) -> None:
        with self.lock:
            if self.running():
                raise HTTPException(409, f"A {self.kind} is already running.")
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.log_path = lb.LOG_DIR / f"ui-{ts}-{kind}.log"
            out = open(self.log_path, "w")
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(
                [sys.executable, str(ROOT / "linkedin_bot.py"), *args],
                stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env,
            )
            self.kind, self.started = kind, datetime.now().isoformat(timespec="seconds")

    def stop(self) -> bool:
        if not self.running():
            return False
        self.proc.send_signal(signal.SIGINT)  # bot catches this and closes cleanly
        return True

    def status(self) -> dict:
        return {
            "running": self.running(),
            "kind": self.kind,
            "started": self.started,
            "exit_code": None if self.proc is None else self.proc.poll(),
        }


job = Job()


class RunRequest(BaseModel):
    dry_run: bool = False
    limit: Optional[int] = Field(None, ge=1, le=100)


@app.post("/api/run")
def run_now(req: RunRequest) -> dict:
    args = ["run", "--ignore-hours", "--trigger", "manual"]
    if req.dry_run:
        args.append("--dry-run")
    if req.limit:
        args += ["--limit", str(req.limit)]
    job.start("dry run" if req.dry_run else "run", args)
    return job.status()


class AcceptRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/accept")
def accept_all(req: AcceptRequest) -> dict:
    args = ["run", "--accept-only", "--ignore-hours", "--trigger", "accept"]
    if req.dry_run:
        args.append("--dry-run")
    job.start("accept (dry run)" if req.dry_run else "accept", args)
    return job.status()


@app.post("/api/login")
def login() -> dict:
    job.start("login", ["login", "--wait", "900"])
    return job.status()


@app.post("/api/stop")
def stop() -> dict:
    return {"stopping": job.stop()}


@app.post("/api/clear-pause")
def clear_pause() -> dict:
    state = lb.load_state()
    state.pop("paused_until", None)
    lb.save_state(state)
    return {"ok": True}


@app.get("/api/logs")
def logs(lines: int = 200) -> dict:
    if not job.log_path or not job.log_path.exists():
        return {"lines": [], "file": None}
    text = job.log_path.read_text(errors="replace").splitlines()
    return {"lines": text[-lines:], "file": job.log_path.name}


# ---------------------------------------------------------------------------
# Status & stats
# ---------------------------------------------------------------------------
def next_run_time(sched: Schedule, last_date: Optional[str]) -> Optional[str]:
    if not sched.enabled:
        return None
    hh, mm = map(int, sched.time.split(":"))
    now = datetime.now()
    for add in range(0, 8):
        d = (now + timedelta(days=add)).date()
        if sched.weekdays_only and d.weekday() >= 5:
            continue
        if d.isoformat() == last_date:
            continue
        t = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
        return t.isoformat(timespec="minutes")
    return None


def sched_state() -> dict:
    return json.loads(SCHED_PATH.read_text()) if SCHED_PATH.exists() else {}


@app.get("/api/status")
def status() -> dict:
    cfg = effective_config()
    s = to_settings(cfg)
    state = lb.load_state()
    paused = state.get("paused_until")
    if paused and paused <= date.today().isoformat():
        paused = None
    return {
        "job": job.status(),
        "logged_in": state.get("logged_in"),
        "paused_until": paused,
        "schedule": s.schedule.model_dump(),
        "next_run": next_run_time(s.schedule, sched_state().get("last_date")),
        "now": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/stats")
def stats() -> dict:
    cfg = effective_config()
    db = lb.History(lb.DB_PATH)
    q = lambda sql, *a: db.conn.execute(sql, a).fetchall()  # noqa: E731
    today = date.today().isoformat()
    days = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        sent = q("SELECT COUNT(*) FROM invites WHERE status='sent' AND created_at LIKE ?", d + "%")[0][0]
        wd = q("SELECT COUNT(*) FROM withdrawals WHERE created_at LIKE ?", d + "%")[0][0]
        acc = q("SELECT COUNT(*) FROM accepted WHERE created_at LIKE ?", d + "%")[0][0]
        days.append({"date": d, "sent": sent, "withdrawn": wd, "accepted": acc})
    state = lb.load_state()
    target = (state.get("target") or {})
    invites = [
        dict(zip(("time", "name", "headline", "url", "source", "status", "note"), r))
        for r in q(
            "SELECT created_at,name,headline,url,source,status,note_used FROM invites "
            "WHERE created_at LIKE ? ORDER BY id DESC", today + "%",
        )
    ]
    withdrawals = [
        dict(zip(("time", "name", "age_days"), r))
        for r in q("SELECT created_at,name,age_days FROM withdrawals WHERE created_at LIKE ? ORDER BY id DESC", today + "%")
    ]
    accepted = [
        dict(zip(("time", "name", "headline", "url"), r))
        for r in q("SELECT created_at,name,headline,url FROM accepted WHERE created_at LIKE ? ORDER BY id DESC", today + "%")
    ]
    runs = [
        dict(zip(("id", "started", "ended", "trigger", "dry_run", "status", "reason", "sent", "withdrawn", "code",
                  "accepted"), r))
        for r in q(
            "SELECT id,started_at,ended_at,trigger,dry_run,status,reason,sent,withdrawn,exit_code,accepted "
            "FROM runs ORDER BY id DESC LIMIT 15"
        )
    ]
    return {
        "today": {
            "sent": db.sent_today(),
            "target": target.get("value") if target.get("date") == today else None,
            "withdrawn": len(withdrawals),
            "accepted": len(accepted),
            "week_sent": db.sent_last_7_days(),
            "weekly_cap": int(cfg.get("weekly_cap", 100)),
            "all_time": q("SELECT COUNT(*) FROM invites WHERE status='sent'")[0][0],
        },
        "days": days,
        "invites": invites,
        "withdrawals": withdrawals,
        "accepted": accepted,
        "runs": runs,
    }


# ---------------------------------------------------------------------------
# Daily scheduler (replaces cron/launchd; runs while the container is up)
# ---------------------------------------------------------------------------
def scheduler_loop() -> None:
    while True:
        try:
            s = to_settings(effective_config()).schedule
            st = sched_state()
            now = datetime.now()
            hh, mm = map(int, s.time.split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now.date().isoformat()
            if (
                s.enabled
                and now >= due
                and st.get("last_date") != today
                and not (s.weekdays_only and now.weekday() >= 5)
                and not job.running()
            ):
                SCHED_PATH.write_text(json.dumps({"last_date": today, "started": now.isoformat(timespec="seconds")}))
                job.start("scheduled run", ["run", "--scheduled", "--trigger", "scheduled"])
        except HTTPException:
            pass
        except Exception as e:  # never let the scheduler thread die
            print("scheduler error:", e, flush=True)
        time.sleep(30)


@app.on_event("startup")
def _start_scheduler() -> None:
    threading.Thread(target=scheduler_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Static page
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
