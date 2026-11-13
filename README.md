# Crate

A small self-hosted web page that suggests albums you probably haven't heard, based on what you've been listening to.

Press **Dig up new picks** and Crate runs [Claude Code](https://claude.com/claude-code) headlessly with your prompt. Claude reads your Last.fm listening history, finds similar artists, checks your [Navidrome](https://www.navidrome.org/) library so it skips albums you already own, and returns a table of 10 picks. Each pick has Bandcamp and YouTube search links so you can preview it.

Then mark each pick:

- **Got**: you picked it up. The row is struck through.
- **♥**: it really stood out. The row is highlighted.
- **Pass**: you previewed it and it's not for you. The row fades out.

Your marks are fed back into every future run. Loved albums count most, Got albums count as liked, and passed albums are a mild "less of this" hint. Marked albums and anything from the last 12 runs are never suggested again.

## How it works

- `app.py`: a FastAPI server that serves the page, stores your prompt and marks, and runs `claude -p` with only the `navidrome` and `lastfm` MCP tools allowed (no shell, file edits or web access). Each run is saved to `picks/YYYY-MM-DD_HHMM.md`.
- `index.html`: the whole front end, in one file with no build step.
- The prompt Claude gets is your `prompt.md`, followed by your recent picks, then your loved, got and passed albums.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the Python dependencies automatically)
- [Claude Code](https://claude.com/claude-code), installed and logged in (`claude` on your PATH)
- Node.js (`npx`), for the Navidrome MCP server
- A Navidrome server, and a [Last.fm API key](https://www.last.fm/api/account/create)

## Setup

```sh
git clone <this repo> crate
cd crate

# 1. MCP servers: add your Navidrome login and Last.fm API key
cp .mcp.example.json .mcp.json
chmod 600 .mcp.json
$EDITOR .mcp.json

# 2. Approve the two MCP servers for this folder (one time):
#    run `claude` here, accept the navidrome and lastfm servers, then exit.
claude

# 3. Start Crate
uv run app.py
```

Open <http://localhost:8095>. Before your first run, edit the prompt on the page: put in your Last.fm username, describe your taste, and press **Save prompt**. It starts from `prompt.example.md` and is saved to `prompt.md`.

To keep Crate running in the background, see `crate.service.example` for a systemd user service.

## Settings

Near the top of `app.py`:

| Setting | Default | What it does |
|---|---|---|
| `PORT` | `8095` | The port the page is served on |
| `HISTORY_RUNS` | `12` | How many past runs Claude sees, so it doesn't repeat itself |
| `TIMEOUT_SECONDS` | `900` | How long a run can take before it's stopped |

The server listens on all network interfaces and has no login. Run it on a trusted network or put it behind a reverse proxy with authentication.

## Your data stays local

These files hold credentials or personal listening data. They're git-ignored and never committed:
`.mcp.json`, `prompt.md`, `marks.json`, `picks/`.
