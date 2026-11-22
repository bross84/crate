# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "mcp>=2.3,<3", "openai>=3,<4", "httpx", "yt-dlp[default]"]
# ///
"""Crate: a small web front end for the music-discovery prompt.

Serves index.html, lets you edit prompt.md, and runs the prompt on demand
with only the MCP servers from .mcp.json (or .mcp.example.json), filled in
with the accounts saved on the settings page. Each run is saved to
picks/YYYY-MM-DD_HHMM.md.

Two backends, picked with CRATE_BACKEND (environment or .env file):
  claude (default)  runs Claude Code: `claude -p`
  openai            any OpenAI-compatible model (OpenRouter, OpenAI, Ollama, ...),
                    with Crate itself running the MCP servers (see agent.py)
"""
import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

BASE = pathlib.Path(__file__).resolve().parent
PICKS = BASE / "picks"
ARCHIVE = PICKS / "archive"  # archived runs: hidden from the list, still searchable and still count as suggested
PROMPT = BASE / "prompt.md"  # your own prompt (git-ignored); starts from prompt.example.md
PROMPT_EXAMPLE = BASE / "prompt.example.md"
MARKS = BASE / "marks.json"  # {"artist|album" (lowercased): "got" | "love" | "pass"}
SCHEDULE = BASE / "schedule.json"  # {"enabled": bool, "days": [0-6, Monday = 0], "time": "HH:MM"}
OLD_GOT = BASE / "got.json"  # earlier format: a list of keys marked as got
PREVIEWS = BASE / "previews"  # previews/<run>/: downloaded preview songs and playlist.json
SETTINGS = BASE / "settings.json"  # accounts and scrobbling, from the settings page (holds tokens and passwords)
PENDING_LISTENS = BASE / "listens.pending.json"  # {service: [listens it didn't take yet]}; retried every 10 minutes
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

ALLOWED_TOOLS = ["mcp__navidrome", "mcp__listenbrainz", "mcp__lastfm"]  # claude backend: nothing else, no shell, file edits or web
HISTORY_RUNS = 12             # how many past runs to show the model so it doesn't repeat itself
TIMEOUT_SECONDS = 15 * 60
PORT = 8095

app = FastAPI()
state = {"running": False, "started": None, "error": None, "playlist": None}
lock = threading.Lock()
playlist_lock = threading.Lock()  # one playlist build at a time


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

    accounts = read_settings()
    who = []
    if accounts["listenbrainz"]["username"]:
        who.append(f"The ListenBrainz tools use my account ({accounts['listenbrainz']['username']}) by default.")
    if accounts["lastfm"]["username"] and accounts["lastfm"]["api_key"]:
        who.append(f"My Last.fm username is {accounts['lastfm']['username']}.")
    who = f"## My accounts\n{' '.join(who)}\n\n" if who else ""

    return (
        f"Today is {datetime.now():%Y-%m-%d}.\n\n"
        f"{prompt}\n\n"
        f"{who}"
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


def run_claude(prompt, mcp_config):
    cmd = ["claude", "-p", prompt, "--mcp-config", str(mcp_config), "--strict-mcp-config",
           "--allowedTools", *ALLOWED_TOOLS]
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


def run_openai(prompt, mcp_config):
    import agent

    try:
        output = agent.run_sync(
            prompt, base_url=BASE_URL, api_key=API_KEY, model=MODEL,
            mcp_json=mcp_config, timeout=TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise TimeoutError
    if not output:
        raise RuntimeError("The model returned no output.")
    return output


def run_picks():
    name = None
    try:
        with mcp_config() as config:
            output = (run_openai if BACKEND == "openai" else run_claude)(build_prompt(), config)
        name = datetime.now().strftime("%Y-%m-%d_%H%M") + ".md"
        (PICKS / name).write_text(output + "\n")
    except TimeoutError:
        state["error"] = f"Stopped after {TIMEOUT_SECONDS // 60} minutes without finishing."
    except Exception as exc:
        state["error"] = f"Run failed: {str(exc)[-1500:]}"
    finally:
        if name and not state["playlist"]:  # mark it before the page sees the run finish
            state["playlist"] = {"name": name, "done": 0, "total": 0}
        state["running"] = False
    if name:
        build_playlist(name)  # the picks show right away; the playlist fills in behind them


# --- Accounts: saved from the settings page, handed to the MCP servers on each run ---

# service -> field -> the environment variable its MCP server reads (None: only Crate uses it)
ACCOUNT_FIELDS = {
    "listenbrainz": {"username": "LISTENBRAINZ_USERNAME", "token": "LISTENBRAINZ_TOKEN"},
    # shared_secret and the session (from Connect Last.fm) are only for scrobbling
    "lastfm": {"username": None, "api_key": "LASTFM_API_KEY", "shared_secret": None,
               "session_key": None, "session_user": None},
    "navidrome": {"url": "NAVIDROME_URL", "username": "NAVIDROME_USERNAME", "password": "NAVIDROME_PASSWORD"},
}
SECRET_FIELDS = {"token", "api_key", "password", "shared_secret", "session_key"}  # never sent back to the page
SESSION_FIELDS = {"session_key", "session_user"}  # set only by Connect Last.fm, not by saving the form
settings_lock = threading.Lock()


def mcp_servers():
    """Server definitions: your .mcp.json if there is one, else the example's."""
    for path in (BASE / ".mcp.json", BASE / ".mcp.example.json"):
        try:
            return json.loads(path.read_text()).get("mcpServers", {})
        except FileNotFoundError:
            continue
    return {}


def read_settings():
    try:
        saved = json.loads(SETTINGS.read_text())
    except (FileNotFoundError, ValueError):
        saved = None
    if saved is None:  # first start: carry over the accounts already in .mcp.json
        servers = {} if not (BASE / ".mcp.json").exists() else mcp_servers()
        saved = {service: {field: (servers.get(service, {}).get("env") or {}).get(var, "") if var else ""
                           for field, var in fields.items()}
                 for service, fields in ACCOUNT_FIELDS.items()}
        if not saved["listenbrainz"]["token"]:
            saved["listenbrainz"]["token"] = os.environ.get("LISTENBRAINZ_TOKEN", "")
        if any(v for fields in saved.values() for v in fields.values()):
            write_settings(saved)
    settings = {service: {field: str((saved.get(service) or {}).get(field) or "") for field in fields}
                for service, fields in ACCOUNT_FIELDS.items()}
    settings["scrobble"] = saved.get("scrobble", True) is not False  # to ListenBrainz
    settings["scrobble_lastfm"] = saved.get("scrobble_lastfm", True) is not False
    return settings


def write_settings(settings):
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(settings, indent=2))
    tmp.replace(SETTINGS)


@contextlib.contextmanager
def mcp_config():
    """A private, temporary MCP config for one run: the servers with your saved accounts filled in.
    Last.fm and Navidrome are left out when they aren't set up."""
    accounts = read_settings()
    servers = {}
    for name, server in mcp_servers().items():
        server = json.loads(json.dumps(server))
        env = server.setdefault("env", {})
        if name in ACCOUNT_FIELDS:
            for field, var in ACCOUNT_FIELDS[name].items():
                if var:
                    env[var] = accounts[name][field]
        if "LASTFM_API_KEY" in env:  # navidrome-mcp also uses the Last.fm key, when there is one
            env["LASTFM_API_KEY"] = accounts["lastfm"]["api_key"]
        if name == "lastfm" and not accounts["lastfm"]["api_key"]:
            continue
        if name == "navidrome" and not accounts["navidrome"]["url"]:
            continue
        servers[name] = server
    fd, path = tempfile.mkstemp(prefix="crate-mcp-", suffix=".json")  # mkstemp files are owner-only
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": servers}, f)
        yield pathlib.Path(path)
    finally:
        os.unlink(path)


def public_settings():
    """Settings for the page: secrets only say whether they're set, plus their last 4 characters."""
    settings = read_settings()
    out = {"scrobble": settings["scrobble"], "scrobble_lastfm": settings["scrobble_lastfm"]}
    for service, fields in ACCOUNT_FIELDS.items():
        out[service] = {}
        for field in fields:
            value = settings[service][field]
            if field in SECRET_FIELDS:
                out[service][field] = {"set": bool(value), "hint": value[-4:] if len(value) >= 12 else ""}
            else:
                out[service][field] = value
    return out


class SettingsIn(BaseModel):
    # {service: {field: value}}. A secret left out or null keeps the saved one; "" removes it.
    listenbrainz: dict[str, str | None] = {}
    lastfm: dict[str, str | None] = {}
    navidrome: dict[str, str | None] = {}
    scrobble: bool | None = None
    scrobble_lastfm: bool | None = None


def merged_settings(body: SettingsIn):
    settings = read_settings()
    lastfm_app = (settings["lastfm"]["api_key"], settings["lastfm"]["shared_secret"])
    for service, fields in ACCOUNT_FIELDS.items():
        for field, value in (getattr(body, service) or {}).items():
            if field in fields and field not in SESSION_FIELDS and value is not None:
                settings[service][field] = value.strip()
    if (settings["lastfm"]["api_key"], settings["lastfm"]["shared_secret"]) != lastfm_app:
        settings["lastfm"]["session_key"] = settings["lastfm"]["session_user"] = ""  # belongs to the old key
    if settings["navidrome"]["url"]:
        settings["navidrome"]["url"] = settings["navidrome"]["url"].rstrip("/")
        if not re.match(r"https?://", settings["navidrome"]["url"]):
            raise HTTPException(400, "The Navidrome address needs to start with http:// or https://.")
    if body.scrobble is not None:
        settings["scrobble"] = body.scrobble
    if body.scrobble_lastfm is not None:
        settings["scrobble_lastfm"] = body.scrobble_lastfm
    return settings


@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    with settings_lock:
        write_settings(merged_settings(body))
    return public_settings()


LB_API = "https://api.listenbrainz.org/1"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LASTFM_RETRY = {11, 16, 29}  # Last.fm errors worth trying again later: offline, temporarily down, rate limited


class LastfmError(Exception):
    def __init__(self, code, message):
        super().__init__(f"Last.fm says: {message}")
        self.code = code


def lastfm_call(a, method, params, sign=False, post=False):
    """Call the Last.fm API. Signed calls (sessions, scrobbles) need the shared secret."""
    params = {**params, "method": method, "api_key": a["api_key"]}
    if sign:
        raw = "".join(f"{k}{params[k]}" for k in sorted(params)) + a["shared_secret"]
        params["api_sig"] = hashlib.md5(raw.encode()).hexdigest()
    params["format"] = "json"
    r = (httpx.post(LASTFM_API, data=params, timeout=20) if post
         else httpx.get(LASTFM_API, params=params, timeout=20))
    try:
        info = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if "error" in info:
        raise LastfmError(info["error"], info.get("message", "error"))
    r.raise_for_status()
    return info


ORIGIN = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d+)?")


@app.get("/lastfm/connect")
def lastfm_connect(origin: str):
    """Send you to Last.fm to let Crate scrobble; Last.fm sends you back to /lastfm/callback.
    `origin` is the address you opened Crate at, so this works over a LAN address or Tailscale."""
    a = read_settings()["lastfm"]
    if not (a["api_key"] and a["shared_secret"]):
        raise HTTPException(400, "Save a Last.fm API key and shared secret first.")
    if not ORIGIN.fullmatch(origin):
        raise HTTPException(400, "Bad origin.")
    query = urllib.parse.urlencode({"api_key": a["api_key"], "cb": f"{origin}/lastfm/callback"})
    return RedirectResponse(f"https://www.last.fm/api/auth/?{query}")


@app.get("/lastfm/callback")
def lastfm_callback(token: str = ""):
    def back(**query):
        return RedirectResponse(f"/settings?{urllib.parse.urlencode(query)}#acctHead")

    if not token:
        return back(lastfm_error="Last.fm didn't send a login token back. Try Connect Last.fm again.")
    if not read_settings()["lastfm"]["shared_secret"]:
        return back(lastfm_error="Save the Last.fm shared secret first, then press Connect Last.fm.")
    with settings_lock:
        settings = read_settings()
        try:
            session = lastfm_call(settings["lastfm"], "auth.getSession", {"token": token}, sign=True)["session"]
        except LastfmError as exc:
            return back(lastfm_error=str(exc))
        except (httpx.HTTPError, ValueError, KeyError):
            return back(lastfm_error="Couldn't reach Last.fm. Try again in a bit.")
        settings["lastfm"].update(session_key=session["key"], session_user=session["name"])
        if not settings["lastfm"]["username"]:
            settings["lastfm"]["username"] = session["name"]
        write_settings(settings)
    return back(lastfm="connected")


@app.post("/api/lastfm/disconnect")
def lastfm_disconnect():
    with settings_lock:
        settings = read_settings()
        settings["lastfm"]["session_key"] = settings["lastfm"]["session_user"] = ""
        write_settings(settings)
    return public_settings()


def check_account(service, a):
    """Try an account's details. Returns a sentence saying how it went; raises ValueError if they don't work."""
    with httpx.Client(timeout=20, headers={"User-Agent": "Crate (self-hosted music discovery)"}) as http:
        if service == "listenbrainz":
            if not a["username"]:
                raise ValueError("Enter your ListenBrainz username.")
            if a["token"]:
                r = http.get(f"{LB_API}/validate-token", headers={"Authorization": f"Token {a['token']}"})
                r.raise_for_status()
                info = r.json()
                if not info.get("valid"):
                    raise ValueError("ListenBrainz says that token isn't valid.")
                if info.get("user_name", "").lower() != a["username"].lower():
                    raise ValueError(f"That token belongs to {info.get('user_name')}, not {a['username']}.")
                return f"Connected as {info['user_name']}."
            r = http.get(f"{LB_API}/user/{a['username']}/listen-count")
            if r.status_code == 404:
                raise ValueError(f"ListenBrainz has no user called {a['username']}.")
            r.raise_for_status()
            return (f"Found {a['username']}. Without a token, Crate can't scrobble "
                    "and picks preview songs less well.")
        if service == "lastfm":
            if not a["api_key"]:
                raise ValueError("Enter a Last.fm API key.")
            try:
                if a["username"]:
                    lastfm_call(a, "user.getInfo", {"user": a["username"]})
                else:
                    lastfm_call(a, "artist.getInfo", {"artist": "Cher"})
                said = f"Found {a['username']}." if a["username"] else "The API key works."
                if a["session_key"]:
                    if not a["shared_secret"]:
                        return said + " Add the shared secret to scrobble."
                    user = lastfm_call(a, "user.getInfo", {"sk": a["session_key"]}, sign=True)["user"]["name"]
                    return said + f" Scrobbling as {user}."
            except LastfmError as exc:
                raise ValueError(str(exc))
            return said + (" Press Connect Last.fm to scrobble." if a["shared_secret"]
                           else " Add the shared secret and connect to scrobble.")
        if service == "navidrome":
            if not (a["url"] and a["username"] and a["password"]):
                raise ValueError("Enter the Navidrome address, username and password.")
            salt = secrets.token_hex(6)
            r = http.get(f"{a['url']}/rest/ping.view", params={
                "u": a["username"], "s": salt, "t": hashlib.md5((a["password"] + salt).encode()).hexdigest(),
                "v": "1.16.1", "c": "crate", "f": "json",
            })
            r.raise_for_status()
            reply = r.json().get("subsonic-response", {})
            if reply.get("status") != "ok":
                raise ValueError(f"Navidrome says: {reply.get('error', {}).get('message', 'login failed')}")
            return f"Connected to Navidrome {reply.get('serverVersion', '')}".strip() + "."
    raise HTTPException(404, "No such account.")


@app.post("/api/settings/test/{service}")
def test_account(service: str, body: SettingsIn):
    """Try the details on the page (unsaved ones too) without saving them."""
    if service not in ACCOUNT_FIELDS:
        raise HTTPException(404, "No such account.")
    try:
        return {"ok": True, "message": check_account(service, merged_settings(body)[service])}
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except httpx.TimeoutException:
        return {"ok": False, "message": "No answer after 20 seconds. The service may be down; try again later."}
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return {"ok": False, "message": f"Couldn't check: {str(exc)[:200] or type(exc).__name__}"}


# --- Scrobbling: the player reports a song when it starts and once you've heard half of it ---

class ScrobbleIn(BaseModel):
    name: str
    file: str
    kind: Literal["playing_now", "single"]
    listened_at: int | None = None  # when the song started (Unix seconds); for "single"


pending_lock = threading.Lock()


def scrobble_targets(settings):
    """The services to scrobble to: switched on and fully set up."""
    targets = []
    if settings["scrobble"] and settings["listenbrainz"]["token"]:
        targets.append("listenbrainz")
    a = settings["lastfm"]
    if settings["scrobble_lastfm"] and a["api_key"] and a["shared_secret"] and a["session_key"]:
        targets.append("lastfm")
    return targets


def listen_payload(track, listened_at=None):
    """A listen in ListenBrainz's format."""
    info = {"media_player": "Crate", "submission_client": "Crate", "music_service": "youtube.com",
            "origin_url": f"https://www.youtube.com/watch?v={track.get('video_id', '')}"}
    if track.get("seconds"):
        info["duration_ms"] = int(track["seconds"]) * 1000
    if track.get("recording_mbid"):
        info["recording_mbid"] = track["recording_mbid"]
    listen = {"track_metadata": {"artist_name": track["artist"], "track_name": track["title"],
                                 "release_name": track["album"], "additional_info": info}}
    if listened_at:
        listen["listened_at"] = listened_at
    return listen


def lastfm_payload(track, listened_at=None):
    """A scrobble in Last.fm's format."""
    entry = {"artist": track["artist"], "track": track["title"], "album": track["album"]}
    if track.get("seconds"):
        entry["duration"] = int(track["seconds"])
    if track.get("recording_mbid"):
        entry["mbid"] = track["recording_mbid"]
    if listened_at:
        entry["timestamp"] = listened_at
    return entry


def submit_listens(token, listen_type, listens):
    r = httpx.post(f"{LB_API}/submit-listens", timeout=20,
                   headers={"Authorization": f"Token {token}"},
                   json={"listen_type": listen_type, "payload": listens})
    r.raise_for_status()


def submit_scrobbles(a, entries):
    """Send up to 50 scrobbles to Last.fm in one call."""
    params = {"sk": a["session_key"]}
    for i, entry in enumerate(entries):
        params.update({f"{key}[{i}]": value for key, value in entry.items()})
    lastfm_call(a, "track.scrobble", params, sign=True, post=True)


def send_now(service, settings, track, kind, listened_at):
    """Send one listen to one service. Returns True if it went through. A finished listen that
    couldn't be sent for a reason that may pass (the service is down) is queued for later."""
    if service == "listenbrainz":
        token = settings["listenbrainz"]["token"]
        payload = listen_payload(track, listened_at if kind == "single" else None)
        send = lambda: submit_listens(token, kind, [payload])
    else:
        a = settings["lastfm"]
        payload = lastfm_payload(track, listened_at if kind == "single" else None)
        send = ((lambda: lastfm_call(a, "track.updateNowPlaying", {**payload, "sk": a["session_key"]},
                                     sign=True, post=True))
                if kind == "playing_now" else (lambda: submit_scrobbles(a, [payload])))
    try:
        send()
        return True
    except (httpx.HTTPError, LastfmError) as exc:
        retry = not isinstance(exc, LastfmError) or exc.code in LASTFM_RETRY
        if kind == "single" and retry:  # "now playing" is only a live status; not worth keeping
            with pending_lock:
                pending = read_pending()
                pending[service] = (pending.get(service, []) + [payload])[-500:]
                write_pending(pending)
        return False


@app.post("/api/scrobble")
def scrobble(body: ScrobbleIn):
    settings = read_settings()
    targets = scrobble_targets(settings)
    if not targets:
        return {"sent": {}}
    find_pick(body.name)  # a real run name, not a path
    try:
        tracks = json.loads((preview_dir(body.name) / "playlist.json").read_text())["tracks"]
        track = next(t for t in tracks if t["file"] == body.file)
    except (FileNotFoundError, ValueError, KeyError, StopIteration):
        raise HTTPException(404, "No such preview song.")
    now = int(time.time())
    listened_at = min(body.listened_at or now, now)
    sent = {service: send_now(service, settings, track, body.kind, listened_at) for service in targets}
    if body.kind == "single" and any(sent.values()):
        threading.Thread(target=send_pending, daemon=True).start()  # a service is up: catch up
    return {"sent": sent}


def read_pending():
    try:
        pending = json.loads(PENDING_LISTENS.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {"listenbrainz": pending} if isinstance(pending, list) else pending  # older format: a list


def write_pending(pending):
    pending = {service: listens for service, listens in pending.items() if listens}
    if pending:
        PENDING_LISTENS.write_text(json.dumps(pending))
    else:
        PENDING_LISTENS.unlink(missing_ok=True)


def send_pending():
    """Send listens a service couldn't take earlier. Quietly keeps them if it's still down."""
    with pending_lock:
        pending = read_pending()
        if not pending:
            return
        settings = read_settings()
        targets = scrobble_targets(settings)
        if "listenbrainz" in targets and pending.get("listenbrainz"):
            try:
                submit_listens(settings["listenbrainz"]["token"], "import", pending["listenbrainz"])
                pending["listenbrainz"] = []
            except httpx.HTTPError:
                pass
        if "lastfm" in targets and pending.get("lastfm"):
            cutoff = time.time() - 14 * 86400  # Last.fm turns down scrobbles older than two weeks
            left = [e for e in pending["lastfm"] if e.get("timestamp", 0) > cutoff]
            while left:
                try:
                    submit_scrobbles(settings["lastfm"], left[:50])
                except LastfmError as exc:
                    if exc.code in LASTFM_RETRY:
                        break
                except httpx.HTTPError:
                    break
                left = left[50:]  # sent, or turned down for good
            pending["lastfm"] = left
        write_pending(pending)


def retry_pending_listens():
    while True:
        send_pending()
        time.sleep(10 * 60)


# --- Preview playlists: a couple of songs per pick, downloaded to previews/<run>/ ---

def listenbrainz_token():
    return read_settings()["listenbrainz"]["token"]


def preview_dir(name):
    return PREVIEWS / name.removesuffix(".md")


def build_playlist(name):
    """Download a run's preview songs. False if another build is already going."""
    import previews

    if not playlist_lock.acquire(blocking=False):
        if (state["playlist"] or {}).get("name") == name and not state["playlist"]["total"]:
            state["playlist"] = None  # drop this run's placeholder; another build holds the lock
        return False
    try:
        path, _ = find_pick(name)
        folder = preview_dir(name)
        shutil.rmtree(folder, ignore_errors=True)
        state["playlist"] = {"name": name, "done": 0, "total": 0}
        skip = {key for key, mark in read_marks().items() if mark in ("got", "pass")}
        previews.build(
            path.read_text(), folder, listenbrainz_token(), skip=skip,
            progress=lambda done, total: state.update(playlist={"name": name, "done": done, "total": total}),
        )
    except Exception as exc:
        (preview_dir(name)).mkdir(parents=True, exist_ok=True)
        (preview_dir(name) / "playlist.json").write_text(json.dumps(
            {"state": "failed", "error": str(exc)[-500:], "tracks": [], "missing": []}))
    finally:
        state["playlist"] = None
        playlist_lock.release()
    return True


def remove_playlist(name):
    shutil.rmtree(preview_dir(name), ignore_errors=True)


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/settings")
def settings():
    return FileResponse(BASE / "settings.html")


ICONS = {"icon.svg": "image/svg+xml", "apple-touch-icon.png": "image/png"}  # the PNG is icon.svg at 180×180


@app.get("/apple-touch-icon.png")  # iOS also looks here on its own when adding to the home screen
@app.get("/icons/{file}")
def icon(file: str = "apple-touch-icon.png"):
    if file not in ICONS:
        raise HTTPException(404, "No such icon.")
    return FileResponse(BASE / "icons" / file, media_type=ICONS[file],
                        headers={"Cache-Control": "public, max-age=86400"})


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
        remove_playlist(name)
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
    remove_playlist(name)
    return {"ok": True}


@app.get("/api/picks/{name}/playlist")
def get_playlist(name: str):
    find_pick(name)
    building = state["playlist"] if (state["playlist"] or {}).get("name") == name else None
    try:
        playlist = json.loads((preview_dir(name) / "playlist.json").read_text())
    except (FileNotFoundError, ValueError):
        playlist = {"state": "building" if building else "none", "tracks": [], "missing": []}
    if building:
        playlist.update(state="building", progress=building)
    return playlist


@app.post("/api/picks/{name}/playlist", status_code=202)
def make_playlist(name: str):
    find_pick(name)
    if state["playlist"] or playlist_lock.locked():
        raise HTTPException(409, "Another playlist is being made. Try again when it's done.")
    state["playlist"] = {"name": name, "done": 0, "total": 0}
    threading.Thread(target=build_playlist, args=(name,), daemon=True).start()
    return {"ok": True}


PREVIEW_FILE = re.compile(r"\d{2}\.(webm|m4a|mp4|opus|ogg)")


@app.get("/api/previews/{name}/{file}")
def get_preview(name: str, file: str):
    find_pick(name)
    path = preview_dir(name) / file
    if not PREVIEW_FILE.fullmatch(file) or not path.exists():
        raise HTTPException(404, "No such preview.")
    media = {"webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4", "opus": "audio/ogg", "ogg": "audio/ogg"}
    return FileResponse(path, media_type=media[path.suffix[1:]])


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=retry_pending_listens, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
