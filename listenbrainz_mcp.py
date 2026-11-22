# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx"]
# ///
"""A small MCP server for ListenBrainz: listening stats, recent listens and similar artists.

Run with `uv run listenbrainz_mcp.py` (stdio). Settings come from the environment:
  LISTENBRAINZ_USERNAME  default user for the stats tools
  LISTENBRAINZ_TOKEN     optional user token (listenbrainz.org/settings). Needed for
                         artist popularity; without it, get_artist_albums uses MusicBrainz.

Artist names are turned into MusicBrainz IDs with the MusicBrainz search API.
"""
import asyncio
import functools
import logging
import os
import time

import httpx
from mcp.server.mcpserver import MCPServer

LB = "https://api.listenbrainz.org/1"
LABS = "https://labs.api.listenbrainz.org"
MB = "https://musicbrainz.org/ws/2"
# ListenBrainz's session-based similarity model, the one listenbrainz.org uses for artist pages.
SIMILARITY_ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"

USERNAME = os.environ.get("LISTENBRAINZ_USERNAME", "").strip()
TOKEN = os.environ.get("LISTENBRAINZ_TOKEN", "").strip()

server = MCPServer(
    "listenbrainz",
    instructions=(
        "ListenBrainz listening history and artist similarity. Stats ranges are calendar periods: "
        "'month', 'quarter', 'half_yearly' and 'year' are the last complete one, and 'this_*' "
        "is the current one so far. 'all_time' (the default) never comes back empty; use the "
        "shorter ranges to see what the user is into lately."
    ),
)
http = httpx.AsyncClient(
    headers={"User-Agent": "crate-listenbrainz-mcp/0.1"}, timeout=30, follow_redirects=True
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # no line per request on stderr
mb_lock = asyncio.Lock()
mb_last = 0.0


class ToolError(Exception):
    pass


async def get(url, params=None, auth=False):
    headers = {"Authorization": f"Token {TOKEN}"} if auth and TOKEN else {}
    resp = await http.get(url, params=params, headers=headers)
    if resp.status_code == 204:
        return None  # ListenBrainz: stats not calculated yet
    if resp.status_code == 404:
        raise ToolError(_error(resp) or "Not found.")
    if resp.status_code == 401:
        raise ToolError(_error(resp) or "This endpoint needs a ListenBrainz token.")
    resp.raise_for_status()
    return resp.json()


def _error(resp):
    try:
        return resp.json().get("error")
    except ValueError:
        return None


async def mb_get(path, params):
    """MusicBrainz allows about one request per second and answers 503 when busy."""
    global mb_last
    async with mb_lock:
        for attempt in range(4):
            wait = 1.05 * 2**attempt - (time.monotonic() - mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await get(f"{MB}/{path}", {**params, "fmt": "json"})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 503 or attempt == 3:
                    raise
            finally:
                mb_last = time.monotonic()


def user_or_default(user):
    user = (user or USERNAME).strip()
    if not user:
        raise ToolError("No ListenBrainz username given and LISTENBRAINZ_USERNAME isn't set.")
    return user


async def search_artists(name, limit=5):
    data = await mb_get("artist", {"query": f'artist:"{name}"', "limit": limit})
    return [
        {
            "mbid": a["id"],
            "name": a["name"],
            "disambiguation": a.get("disambiguation", ""),
            "country": a.get("country", ""),
            "type": a.get("type", ""),
            "score": a.get("score"),
        }
        for a in (data or {}).get("artists", [])
    ]


async def resolve_artist(artist, mbid):
    """Return (mbid, label) from an MBID or the best MusicBrainz name match.

    The label includes MusicBrainz's disambiguation so a wrong match is easy to spot.
    """
    if mbid:
        return mbid, artist or mbid
    if not artist:
        raise ToolError("Give an artist name or artist_mbid.")
    matches = await search_artists(artist, limit=1)
    if not matches:
        raise ToolError(f"MusicBrainz has no artist matching {artist!r}.")
    best = matches[0]
    label = best["name"] + (f" ({best['disambiguation']})" if best["disambiguation"] else "")
    return best["mbid"], label


def safe(fn):
    """Return errors as plain messages so the model can read them and move on."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            return {"error": str(exc)}
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__  # timeouts have no message of their own
            return {"error": f"Request failed: {detail}. ListenBrainz may be down or slow; try again later."}

    return wrapper


async def stats(kind, user, range, count):
    user = user_or_default(user)
    data = await get(
        f"{LB}/stats/user/{user}/{kind}", {"range": range, "count": max(1, min(count, 100))}
    )
    if not data:
        return None, user
    return data["payload"], user


@server.tool()
@safe
async def get_user_top_artists(range: str = "all_time", count: int = 25, user: str = "") -> dict:
    """A user's most-listened artists for a stats range: this_week, this_month, this_year, week, month, quarter, half_yearly, year, all_time.

    user defaults to LISTENBRAINZ_USERNAME.
    """
    payload, user = await stats("artists", user, range, count)
    if not payload:
        return {"user": user, "range": range, "artists": [], "note": "No stats for this range yet."}
    return {
        "user": user,
        "range": range,
        "from": time.strftime("%Y-%m-%d", time.gmtime(payload["from_ts"])),
        "to": time.strftime("%Y-%m-%d", time.gmtime(payload["to_ts"])),
        "artists": [
            {"name": a["artist_name"], "mbid": a.get("artist_mbid"), "listens": a["listen_count"]}
            for a in payload["artists"]
        ],
    }


@server.tool()
@safe
async def get_user_top_albums(range: str = "all_time", count: int = 25, user: str = "") -> dict:
    """A user's most-listened albums (release groups) for a stats range: this_week, this_month, this_year, week, month, quarter, half_yearly, year, all_time.

    user defaults to LISTENBRAINZ_USERNAME.
    """
    payload, user = await stats("release-groups", user, range, count)
    if not payload:
        return {"user": user, "range": range, "albums": [], "note": "No stats for this range yet."}
    return {
        "user": user,
        "range": range,
        "from": time.strftime("%Y-%m-%d", time.gmtime(payload["from_ts"])),
        "to": time.strftime("%Y-%m-%d", time.gmtime(payload["to_ts"])),
        "albums": [
            {
                "artist": rg["artist_name"],
                "album": rg["release_group_name"],
                "mbid": rg.get("release_group_mbid"),
                "listens": rg["listen_count"],
            }
            for rg in payload["release_groups"]
        ],
    }


@server.tool()
@safe
async def get_user_recent_listens(count: int = 100, user: str = "") -> dict:
    """A user's most recent listens, newest first (up to 1000).

    user defaults to LISTENBRAINZ_USERNAME.
    """
    user = user_or_default(user)
    data = await get(f"{LB}/user/{user}/listens", {"count": max(1, min(count, 1000))}, auth=True)
    listens = (data or {}).get("payload", {}).get("listens", [])
    return {
        "user": user,
        "listens": [
            {
                "listened_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(item["listened_at"])),
                "artist": item["track_metadata"].get("artist_name"),
                "track": item["track_metadata"].get("track_name"),
                "album": item["track_metadata"].get("release_name"),
            }
            for item in listens
        ],
    }


@server.tool()
@safe
async def search_artist(name: str, limit: int = 5) -> dict:
    """Find artists by name on MusicBrainz, with MBIDs and disambiguation, to pick the right one."""
    return {"artists": await search_artists(name, max(1, min(limit, 25)))}


@server.tool()
@safe
async def get_similar_artists(artist: str = "", artist_mbid: str = "", limit: int = 25) -> dict:
    """Artists similar to the given one, from ListenBrainz listening sessions.

    Pass an artist_mbid when you have one (the top-artists tool returns them), or a name,
    which is matched on MusicBrainz. If the returned artist isn't the one you meant, find the
    right MBID with search_artist and call again.
    Higher score means more similar. Results come from the global catalog, not the user's library.
    """
    mbid, name = await resolve_artist(artist, artist_mbid)
    data = await get(
        f"{LABS}/similar-artists/json",
        {"artist_mbids": mbid, "algorithm": SIMILARITY_ALGORITHM},
    )
    rows = data or []
    return {
        "artist": name,
        "mbid": mbid,
        "similar": [
            {
                "name": r["name"],
                "mbid": r["artist_mbid"],
                "score": r["score"],
                "comment": r.get("comment") or "",
            }
            for r in rows[: max(1, min(limit, 100))]
        ],
    }


@server.tool()
@safe
async def get_artist_albums(artist: str = "", artist_mbid: str = "") -> dict:
    """An artist's studio albums with release years.

    With LISTENBRAINZ_TOKEN set, they're sorted by ListenBrainz listen counts (most popular
    first). Otherwise they come from MusicBrainz, sorted by year.
    """
    mbid, name = await resolve_artist(artist, artist_mbid)
    if TOKEN:
        try:
            data = await get(f"{LB}/popularity/top-release-groups-for-artist/{mbid}", auth=True)
            albums = [
                {
                    "album": rg["release_group"]["name"],
                    "year": (rg["release_group"].get("date") or "")[:4] or None,
                    "type": rg["release_group"].get("type"),
                    "mbid": rg["release_group_mbid"],
                    "listens": rg.get("total_listen_count"),
                }
                for rg in data or []
                if rg.get("release_group", {}).get("type") in (None, "Album")
            ]
            if albums:
                return {"artist": name, "mbid": mbid, "source": "listenbrainz", "albums": albums}
        except (ToolError, httpx.HTTPError, KeyError, TypeError):
            pass  # fall back to MusicBrainz
    data = await mb_get("release-group", {"artist": mbid, "type": "album", "limit": 100})
    albums = [
        {
            "album": rg["title"],
            "year": (rg.get("first-release-date") or "")[:4] or None,
            "mbid": rg["id"],
        }
        for rg in (data or {}).get("release-groups", [])
        if not rg.get("secondary-types")  # skip live albums, compilations, etc.
    ]
    albums.sort(key=lambda a: a["year"] or "9999")
    return {"artist": name, "mbid": mbid, "source": "musicbrainz", "albums": albums}


if __name__ == "__main__":
    server.run("stdio")
