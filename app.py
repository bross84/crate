# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi", "uvicorn", "mcp>=2.3,<3", "openai>=3,<4"]
# ///
"""Crate: a small web front end for the music-discovery prompt.

Serves index.html, lets you edit prompt.md, and runs the prompt on demand
with only the MCP tools from .mcp.json. Each run is saved to
picks/YYYY-MM-DD_HHMM.md.

Two backends, picked with CRATE_BACKEND (environment or .env file):
  claude (default)  runs Claude Code: `claude -p`
  openai            any OpenAI-compatible model (OpenRouter, OpenAI, Ollama, ...),
                    with Crate itself running the MCP servers (see agent.py)
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

BASE = pathlib.Path(__file__).resolve().parent
PICKS = BASE / "picks"
ARCHIVE = PICKS / "archive"  # archived runs: hidden from the list, still searchable and still count as suggested
PROMPT = BASE / "prompt.md"  # your own prompt (git-ignored); starts from prompt.example.md
PROMPT_EXAMPLE = BASE / "prompt.example.md"
MARKS = BASE / "marks.json"  # {"artist|album" (lowercased): "got" | "love" | "pass"}
SCHEDULE = BASE / "schedule.json"  # {"enabled": bool, "days": [0-6, Monday = 0], "time": "HH:MM"}
OLD_GOT = BASE / "got.json"  # earlier format: a list of keys marked as got
ARCHIVE.mkdir(parents=True, exist_ok=True)


def load_dotenv(path=BASE / ".env"):
    """Read KEY=value lines from .env without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if value[:1] in ("'", '"'):
                value = value[1:].split(value[0], 1)[0]  # quoted: keep everything inside the quotes
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0]  # unquoted: drop inline comments
            os.environ.setdefault(key.strip(), value)


load_dotenv()
BACKEND = os.environ.get("CRATE_BACKEND", "claude").strip().lower()
CLAUDE_MODEL = os.environ.get("CRATE_CLAUDE_MODEL", "")  # optional, e.g. claude-sonnet-5-5
BASE_URL = os.environ.get("CRATE_BASE_URL", "")          # e.g. https://openrouter.ai/api/v1
API_KEY = os.environ.get("CRATE_API_KEY", "")
MODEL = os.environ.get("CRATE_MODEL", "")
if BACKEND not in ("claude", "openai"):
    raise SystemExit(f"CRATE_BACKEND must be 'claude' or 'openai', not {BACKEND!r}.")
if BACKEND == "openai" and not MODEL:
    raise SystemExit("CRATE_BACKEND=openai needs CRATE_MODEL (and usually CRATE_BASE_URL and CRATE_API_KEY).")

ALLOWED_TOOLS = ["mcp__navidrome", "mcp__lastfm"]  # claude backend: nothing else, no shell, file edits or web
HISTORY_RUNS = 12             # how many past runs to show the model so it doesn't repeat itself
TIMEOUT_SECONDS = 15 * 60
PORT = 8095

app = FastAPI()
state = {"running": False, "started": None, "error": None}
lock = threading.Lock()


def read_prompt():
    for path in (PROMPT, PROMPT_EXAMPLE):
        if path.exists():
            return path.read_text()
    return ""


PICK_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{4}\.md")


def pick_files(folder=PICKS):
    return sorted(folder.glob("*.md"), reverse=True)


def all_pick_files():
    """Active and archived runs, newest first."""
    return sorted(pick_files() + pick_files(ARCHIVE), key=lambda p: p.name, reverse=True)


def find_pick(name):
    """Return (path, archived) for a run name, or raise 404."""
    if PICK_NAME.fullmatch(name):
        for folder, archived in ((PICKS, False), (ARCHIVE, True)):
            if (folder / name).exists():
                return folder / name, archived
    raise HTTPException(404, "No such picks file.")


def build_prompt():
    prompt = read_prompt()
    past = "\n\n".join(p.read_text() for p in all_pick_files()[:HISTORY_RUNS])
    marks = read_marks()

    def listed(kind):
        rows = [" – ".join(key.split("|", 1)) for key, mark in sorted(marks.items()) if mark == kind]
        return "\n".join(f"- {r}" for r in rows) or "(none yet)"

    return (
        f"{prompt}\n\n"
        "## Checking my Navidrome library\n"
        "Use Navidrome's search_all to check whether I own an artist or album: one call returns "
        "matching artists and albums. Don't use search_artists. It rejects every call in "
        "navidrome-mcp 3.0.0, so there's no need to mention it.\n\n"
        "## Previously suggested (do not repeat any of these albums)\n"
        f"{past or '(none yet)'}\n\n"
        "## Earlier picks I loved (artist – album)\n"
        "These really stood out. They're my strongest taste signal: weight them above everything "
        "else when choosing what to suggest. Don't suggest them again.\n"
        f"{listed('love')}\n\n"
        "## Earlier picks I got (artist – album)\n"
        "I liked these enough to pick them up. Treat them as strong signals of my taste "
        "and lean toward artists and sounds like them. Don't suggest them again.\n"
        f"{listed('got')}\n\n"
        "## Earlier picks I passed on after previewing (artist – album)\n"
        "These particular albums weren't for me, so don't suggest them again and treat them "
        "as a mild signal about sounds I'm less into. Other albums by these artists are still fine.\n"
        f"{listed('pass')}"
    )


def run_claude(prompt):
    cmd = ["claude", "-p", prompt, "--allowedTools", *ALLOWED_TOOLS]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    try:
        result = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise TimeoutError
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        raise RuntimeError((result.stderr or output or "Claude returned no output.").strip()[-1500:])
    return output


def run_openai(prompt):
    import agent

    try:
        output = agent.run_sync(
            prompt, base_url=BASE_URL, api_key=API_KEY, model=MODEL,
            mcp_json=BASE / ".mcp.json", timeout=TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise TimeoutError
    if not output:
        raise RuntimeError("The model returned no output.")
    return output


def run_picks():
    try:
        output = (run_openai if BACKEND == "openai" else run_claude)(build_prompt())
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        (PICKS / f"{stamp}.md").write_text(output + "\n")
    except TimeoutError:
        state["error"] = f"Stopped after {TIMEOUT_SECONDS // 60} minutes without finishing."
    except Exception as exc:
        state["error"] = f"Run failed: {str(exc)[-1500:]}"
    finally:
        state["running"] = False


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/state")
def get_state():
    return state


def try_start_run():
    """Start a run in the background. False if one is already going."""
    with lock:
        if state["running"]:
            return False
        state.update(running=True, started=time.time(), error=None)
    threading.Thread(target=run_picks, daemon=True).start()
    return True


@app.post("/api/run", status_code=202)
def start_run():
    if not try_start_run():
        raise HTTPException(409, "A run is already in progress.")
    return state


# --- Schedule: automatic runs on chosen weekdays at a set time (server's local time) ---

class ScheduleIn(BaseModel):
    enabled: bool
    days: list[int]
    time: str


def read_schedule():
    try:
        return ScheduleIn(**json.loads(SCHEDULE.read_text()))
    except (FileNotFoundError, ValueError, TypeError):
        return ScheduleIn(enabled=False, days=[0], time="07:00")


def next_run(sched, now=None):
    if not sched.enabled or not sched.days:
        return None
    now = now or datetime.now()
    hour, minute = map(int, sched.time.split(":"))
    for ahead in range(8):
        when = (now + timedelta(days=ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when.weekday() in sched.days and when > now:
            return when


def scheduler():
    fired = None  # the minute we last started a run for, so each slot runs once
    while True:
        now = datetime.now()
        sched = read_schedule()
        slot = now.strftime("%Y-%m-%d %H:%M")
        if (sched.enabled and now.weekday() in sched.days
                and now.strftime("%H:%M") == sched.time and slot != fired):
            fired = slot
            try_start_run()  # skipped quietly if a run is already going
        time.sleep(20)


@app.get("/api/schedule")
def get_schedule():
    sched = read_schedule()
    when = next_run(sched)
    return {**sched.model_dump(), "next": when.isoformat(timespec="minutes") if when else None}


@app.put("/api/schedule")
def put_schedule(body: ScheduleIn):
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time):
        raise HTTPException(400, "Time must be HH:MM, 24-hour.")
    days = sorted(set(body.days))
    if any(d not in range(7) for d in days):
        raise HTTPException(400, "Days must be 0 (Monday) to 6 (Sunday).")
    if body.enabled and not days:
        raise HTTPException(400, "Pick at least one day.")
    body.days = days
    tmp = SCHEDULE.with_suffix(".tmp")
    tmp.write_text(json.dumps(body.model_dump(), indent=1))
    tmp.replace(SCHEDULE)
    return get_schedule()


class PromptIn(BaseModel):
    text: str


@app.get("/api/prompt")
def get_prompt():
    return {"text": read_prompt()}


@app.put("/api/prompt")
def put_prompt(body: PromptIn):
    if not body.text.strip():
        raise HTTPException(400, "The prompt can't be empty.")
    PROMPT.write_text(body.text)
    return {"ok": True}


def read_marks():
    try:
        return json.loads(MARKS.read_text())
    except FileNotFoundError:
        pass
    except ValueError:
        return {}
    try:  # carry over checks saved in the older got.json format
        return {key: "got" for key in json.loads(OLD_GOT.read_text())}
    except (FileNotFoundError, ValueError):
        return {}


class MarkIn(BaseModel):
    key: str
    mark: Literal["got", "love", "pass"] | None


@app.get("/api/marks")
def get_marks():
    return read_marks()


@app.put("/api/marks")
def put_mark(body: MarkIn):
    with lock:
        marks = read_marks()
        if body.mark:
            marks[body.key] = body.mark
        else:
            marks.pop(body.key, None)
        tmp = MARKS.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(marks.items())), indent=1))
        tmp.replace(MARKS)
    return {"ok": True}


@app.get("/api/picks")
def list_picks():
    return {
        "active": [p.name for p in pick_files()],
        "archived": [p.name for p in pick_files(ARCHIVE)],
    }


@app.get("/api/search-index")
def search_index():
    """Every run's text, for searching across all picks in the page."""
    return [
        {"name": p.name, "archived": p.parent == ARCHIVE, "text": p.read_text()}
        for p in all_pick_files()
    ]


@app.get("/api/picks/{name}", response_class=PlainTextResponse)
def get_pick(name: str):
    return find_pick(name)[0].read_text()


@app.post("/api/picks/{name}/archive")
def archive_pick(name: str):
    path, archived = find_pick(name)
    if not archived:
        path.replace(ARCHIVE / name)
    return {"ok": True}


@app.post("/api/picks/{name}/restore")
def restore_pick(name: str):
    path, archived = find_pick(name)
    if archived:
        path.replace(PICKS / name)
    return {"ok": True}


@app.delete("/api/picks/{name}")
def delete_pick(name: str):
    """Permanently delete an archived run. Marks on its albums are kept."""
    path, archived = find_pick(name)
    if not archived:
        raise HTTPException(409, "Archive this run before deleting it.")
    path.unlink()
    return {"ok": True}


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
