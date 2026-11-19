# Crate

A small self-hosted web page that suggests albums you probably haven't heard, based on what you've been listening to.

![Crate showing a table of album picks with Got, love and Pass marks on the first three rows, and the sidebar with search, earlier picks and the schedule set to every Monday at 7:00 AM](docs/screenshot-schedule.png)

Press **Dig up new picks** and Crate runs your prompt through an AI model: [Claude Code](https://claude.com/claude-code) by default, or any OpenAI-compatible model (OpenRouter, OpenAI, Ollama, LM Studio) if you bring your own key. The model reads your [ListenBrainz](https://listenbrainz.org/) listening history, finds similar artists, checks your [Navidrome](https://www.navidrome.org/) library so it skips albums you already own, and returns a table of 10 picks. Each pick has Bandcamp and YouTube search links so you can preview it.

Then mark each pick:

- **Got**: you picked it up. The row is struck through.
- **♥**: it really stood out. The row is highlighted.
- **Pass**: you previewed it and it's not for you. The row fades out.

Your marks are fed back into every future run. Loved albums count most, Got albums count as liked, and passed albums are a mild "less of this" hint. Marked albums and anything from the last 12 runs are never suggested again.

Use **Search** in the sidebar to find a pick across all your lists, by artist, album, year or any word in the description.

To tidy up the sidebar, open a list and press **Archive this list**. Archived lists move to a collapsed "Archived" section, still show up in searches, and still count as suggested, so they won't come back. From there you can **Restore** a list or **Delete** it permanently. Deleting a list keeps your Got, ♥ and Pass marks.

To get new picks without pressing the button, use **Schedule** in the sidebar: pick the days and a time, and Crate digs up picks on its own while it's running. Times use the server's clock.

## How it works

- `app.py`: a FastAPI server that serves the page, stores your prompt, marks and schedule, and starts each run, either when you press the button or on schedule. Each run is saved to `picks/YYYY-MM-DD_HHMM.md`, and archived lists move to `picks/archive/`.
- `agent.py`: the runner for the `openai` backend. Crate starts the MCP servers from `.mcp.json` itself and passes tool calls between them and the model.
- `index.html`: the whole front end, in one file with no build step.
- The prompt the model gets is your `prompt.md`, followed by your recent picks, then your loved, got and passed albums.

Either way, the model can only use the Navidrome, ListenBrainz and Last.fm tools: no shell, file edits or web access.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs the Python dependencies automatically)
- Node.js (`npx`), for the Navidrome MCP server
- A Navidrome server
- A free [ListenBrainz](https://listenbrainz.org/) account with your listens in it. Navidrome can scrobble to ListenBrainz (turn it on under Personal in its web UI), and ListenBrainz can [import your Last.fm history](https://listenbrainz.org/settings/import/).
- Optional: a [Last.fm API key](https://www.last.fm/api/account/create). Last.fm is moving API access behind a paid plan, so Crate no longer depends on it, but the `lastfm` server is still set up if you have a key.
- One of:
  - [Claude Code](https://claude.com/claude-code), installed and logged in (`claude` on your PATH), for the default backend
  - An API key for any OpenAI-compatible provider, or a local model server, for the `openai` backend

## MCP servers

Crate gives the model three [MCP](https://modelcontextprotocol.io/) servers, set up in `.mcp.json`:

| Server | Package | What the model uses it for |
|---|---|---|
| `navidrome` | [navidrome-mcp](https://github.com/Blakeem/Navidrome-MCP) (npm) | Searching your library, so it skips albums you already own |
| `listenbrainz` | `listenbrainz_mcp.py` (in this repo) | Your listening history, similar artists, and artists' albums |
| `lastfm` | [lastfm-mcp](https://pypi.org/project/lastfm-mcp/) (PyPI) | Optional. Last.fm history and similar artists, if you have an API key |

You don't install them yourself. `npx` and `uv` download them the first time a run starts, so the first run takes a little longer. `.mcp.example.json` already has the right commands. You only add your Navidrome login and ListenBrainz username (and a Last.fm API key if you have one).

The `listenbrainz` server takes two settings in its `env`:
- `LISTENBRAINZ_USERNAME`: the account whose listening stats it reads.
- `LISTENBRAINZ_TOKEN`: optional, from your [ListenBrainz settings](https://listenbrainz.org/settings/). With it, an artist's albums are ranked by how much people listen to them. Without it, they come from MusicBrainz, ordered by year.

ListenBrainz stats cover calendar periods (`this_month`, `quarter`, `this_year`, `all_time`, ...), not rolling windows. The tools default to `all_time`, and the model can look at shorter periods to see what you're into lately. New accounts and fresh imports can take a day or so before stats appear.

## Setup

```sh
git clone <this repo> crate
cd crate

# 1. MCP servers: add your Navidrome login and ListenBrainz username
cp .mcp.example.json .mcp.json
chmod 600 .mcp.json
$EDITOR .mcp.json

# 2. Pick a backend (see "Choosing a model" below).
#    Default backend (Claude Code): approve the two MCP servers for this folder once:
#    run `claude` here, accept the navidrome, listenbrainz and lastfm servers, then exit.
claude
#    Or, for your own model/key:
#    cp .env.example .env && $EDITOR .env

# 3. Start Crate
uv run app.py
```

Open <http://localhost:8095>. Before your first run, press **Edit prompt** at the bottom of the page, put in your ListenBrainz username, describe your taste, and press **Save**. It starts from `prompt.example.md` and is saved to `prompt.md`.

To keep Crate running in the background, see `crate.service.example` for a systemd user service. Crate has to be running for scheduled runs to happen.

## Choosing a model

Crate's backend is set with `CRATE_*` settings, either in a `.env` file (copy `.env.example`) or as environment variables. Environment variables win over `.env`. Restart Crate after changing them.

**Claude Code (default).** Nothing to set. Uses your Claude Code login. Optional:
- `CRATE_CLAUDE_MODEL`: a model name for Claude Code's `--model` flag, e.g. `claude-sonnet-5-5`.
- `ANTHROPIC_API_KEY`: bill an Anthropic API key instead of a Claude subscription.

**Any OpenAI-compatible model (bring your own key).** Set:

```sh
CRATE_BACKEND=openai
CRATE_BASE_URL=https://openrouter.ai/api/v1   # or OpenAI, Ollama, LM Studio: see .env.example
CRATE_API_KEY=your-key                        # local servers accept any value
CRATE_MODEL=provider/model-name
```

Claude Code isn't needed for this backend. Crate runs the same MCP servers from `.mcp.json` itself.

A run makes many ListenBrainz and Navidrome tool calls, so the model needs to be good at tool calling. Small local models often lose track partway through or return a broken table. With paid APIs, each run costs money: every tool call sends the conversation back to the model.

## Settings

Near the top of `app.py`:

| Setting | Default | What it does |
|---|---|---|
| `PORT` | `8095` | The port the page is served on |
| `HISTORY_RUNS` | `12` | How many past runs the model sees, so it doesn't repeat itself |
| `TIMEOUT_SECONDS` | `900` | How long a run can take before it's stopped |

The server listens on all network interfaces and has no login. Run it on a trusted network or put it behind a reverse proxy with authentication.

## Your data stays local

These files hold credentials or personal listening data. They're git-ignored and never committed:
`.mcp.json`, `.env`, `prompt.md`, `marks.json`, `schedule.json`, `picks/`.
