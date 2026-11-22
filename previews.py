"""Preview playlists: a couple of top songs from each album in a run, downloaded with yt-dlp.

For each pick, the songs are chosen by ListenBrainz popularity (needs LISTENBRAINZ_TOKEN in the
listenbrainz server's env in .mcp.json). When ListenBrainz knows too little about an album, the
album's MusicBrainz tracklist is ranked by YouTube views instead.

Audio goes to previews/<run>/ with a playlist.json, and never touches Navidrome.
"""
import json
import pathlib
import re
import time
import unicodedata

import httpx
import yt_dlp

SONGS_PER_ALBUM = 2
UA = {"User-Agent": "crate-previews/0.1"}
LB = "https://api.listenbrainz.org/1"
MB = "https://musicbrainz.org/ws/2"
# Words that mark a video as something other than the album version, unless the song title has them too.
NOT_ALBUM_VERSION = re.compile(
    r"\b(live|cover|reaction|lesson|tutorial|karaoke|remix|playthrough|drum ?cam|guitar ?cam|"
    r"full album|instrumental|acoustic|slowed|sped up|nightcore|8d|rehearsal|bass cover|vocal cover)\b",
    re.I,
)


def fold(text):
    """Lowercase, strip accents and punctuation: for loose name matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def bare(title):
    """A title without bracketed extras like (Remastered) or [Deluxe Edition]."""
    return fold(re.sub(r"[\(\[].*?[\)\]]", " ", title or ""))


def has_words(haystack, needle):
    """True if needle appears in haystack as whole words, after folding both."""
    needle = fold(needle)
    return bool(needle) and f" {needle} " in f" {fold(haystack)} "


def parse_picks(markdown):
    """(artist, album) for each row of the first table with Artist and Album columns."""
    def cells(line):
        return [clean(c) for c in line.strip().strip("|").split("|")]

    def clean(cell):
        cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)  # [text](url) -> text
        return cell.replace("**", "").replace("`", "").strip()

    lines = [l for l in markdown.splitlines() if l.strip().startswith("|")]
    for i, line in enumerate(lines):
        heads = [h.lower() for h in cells(line)]
        if "artist" in heads and "album" in heads:
            ai, bi = heads.index("artist"), heads.index("album")
            rows = []
            for row in lines[i + 1:]:
                c = cells(row)
                if set("".join(c)) <= set("-: "):
                    continue  # the |---|---| divider
                if len(c) > max(ai, bi) and c[ai] and c[bi]:
                    rows.append((c[ai], c[bi]))
            return rows
    return []


class Lookup:
    """ListenBrainz and MusicBrainz lookups, keeping to MusicBrainz's one request a second."""

    def __init__(self, token):
        self.token = token
        self.http = httpx.Client(headers=UA, timeout=30, follow_redirects=True)
        self.mb_last = 0.0

    def mb(self, path, **params):
        for attempt in range(5):
            wait = 1.05 * 2**attempt - (time.monotonic() - self.mb_last)
            if wait > 0:
                time.sleep(wait)
            resp = self.http.get(f"{MB}/{path}", params={**params, "fmt": "json"})
            self.mb_last = time.monotonic()
            if resp.status_code != 503:
                resp.raise_for_status()
                return resp.json()
        resp.raise_for_status()

    def artist_mbid(self, artist):
        found = self.mb("artist", query=f'artist:"{artist}"', limit=5).get("artists", [])
        exact = [a for a in found if fold(a["name"]) == fold(artist)]
        return (exact or found or [{}])[0].get("id")

    def popular_songs(self, artist_mbid, album):
        """The album's songs ranked by ListenBrainz listens: [(title, seconds, art URL, recording MBID)], unknowns None."""
        if not (self.token and artist_mbid):
            return []
        resp = self.http.get(
            f"{LB}/popularity/top-recordings-for-artist/{artist_mbid}",
            headers={"Authorization": f"Token {self.token}"},
        )
        if resp.status_code != 200:
            return []
        want = bare(album)
        songs, seen = [], set()
        for rec in sorted(resp.json(), key=lambda r: r.get("total_listen_count") or 0, reverse=True):
            if bare(rec.get("release_name")) != want:
                continue
            key = bare(rec.get("recording_name"))
            if key in seen or NOT_ALBUM_VERSION.search(rec.get("recording_name") or ""):
                continue
            seen.add(key)
            length = rec.get("length")
            art = (f"https://coverartarchive.org/release/{rec['caa_release_mbid']}/{rec['caa_id']}-250.jpg"
                   if rec.get("caa_release_mbid") and rec.get("caa_id") else None)
            songs.append((rec["recording_name"], length / 1000 if length else None, art, rec.get("recording_mbid")))
        return songs

    def tracklist(self, artist_mbid, album):
        """The album's songs in order from MusicBrainz: [(title, seconds or None, art URL, recording MBID)]."""
        if not artist_mbid:
            return []
        groups = self.mb(
            "release-group", query=f'releasegroup:"{album}" AND arid:{artist_mbid}', limit=5
        ).get("release-groups", [])
        group = next((g for g in groups if bare(g["title"]) == bare(album)), groups[0] if groups else None)
        if not group:
            return []
        releases = self.mb("release", **{"release-group": group["id"], "inc": "recordings", "limit": 5})
        for release in releases.get("releases", []):
            tracks = [t for medium in release.get("media", []) for t in medium.get("tracks", [])]
            if tracks:
                art = f"https://coverartarchive.org/release-group/{group['id']}/front-250"
                return [(t["title"], t["length"] / 1000 if t.get("length") else None, art,
                         (t.get("recording") or {}).get("id")) for t in tracks]
        return []


def search(query, count=8):
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                           "js_runtimes": {"node": {}}}) as ydl:
        return ydl.extract_info(f"ytsearch{count}:{query}", download=False).get("entries") or []


def candidates(artist, title, seconds):
    """Search results that look like the album version of a song, most likely first."""
    scored = []
    for e in search(f"{artist} {title}"):
        name, dur = e.get("title") or "", e.get("duration")
        if not dur or not has_words(name, title):
            continue
        if NOT_ALBUM_VERSION.search(name) and not NOT_ALBUM_VERSION.search(title):
            continue
        # Lengths from ListenBrainz/MusicBrainz are sometimes for another version of the song, so
        # only a length that's way off rules a video out. A close length is a tie-breaker below.
        if seconds:
            if abs(dur - seconds) > max(30, 0.35 * seconds):
                continue
        elif not 30 <= dur <= 1500:
            continue
        channel = e.get("channel") or e.get("uploader") or ""
        topic = fold(channel).endswith(" topic")  # YouTube's auto-generated album uploads
        score = (
            # Where it's from: the official album upload beats the artist's channel, which beats
            # anyone else (labels, fans) whose video title names the artist.
            3 if topic and has_words(channel, artist) else 2 if has_words(channel, artist)
            else 1 if topic or has_words(name, artist) else 0,
            not seconds or abs(dur - seconds) <= max(15, 0.12 * seconds),  # about the right length
            e.get("view_count") or 0,  # then the most watched, usually the best-sounding copy
        )
        scored.append((score, e))
    return [e for _, e in sorted(scored, key=lambda x: x[0], reverse=True)]


def rank_by_views(artist, album, tracks):
    """Order an album's tracks by YouTube views, from one search for the album."""
    views = {}
    for e in search(f"{artist} {album}", count=25):
        name = e.get("title") or ""
        for title, *_ in tracks:
            if bare(title) and has_words(name, bare(title)):
                views[title] = max(views.get(title, 0), e.get("view_count") or 0)
    ranked = sorted((t for t in tracks if t[0] in views), key=lambda t: views[t[0]], reverse=True)
    return ranked + [t for t in tracks if t[0] not in views]


def download(video_id, dest_stem):
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "retries": 3, "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
        "outtmpl": f"{dest_stem}.%(ext)s",
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
    return pathlib.Path(info["requested_downloads"][0]["filepath"]).name


def build(picks_md, folder, token, songs_per_album=SONGS_PER_ALBUM, skip=(), progress=lambda done, total: None):
    """Download previews for a run into folder and write folder/playlist.json as it goes.

    skip holds "artist|album" keys (lowercased, as in marks.json) of albums to leave out.
    """
    folder.mkdir(parents=True, exist_ok=True)
    picks, skipped = [], []
    for artist, album in parse_picks(picks_md):
        (skipped if f"{artist}|{album}".lower() in skip else picks).append((artist, album))
    playlist = {"state": "building", "albums": len(picks), "tracks": [], "missing": [],
                "skipped": [{"artist": a, "album": b} for a, b in skipped]}
    manifest = folder / "playlist.json"

    def save():
        tmp = manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(playlist, indent=1))
        tmp.replace(manifest)

    save()
    lookup = Lookup(token)
    for n, (artist, album) in enumerate(picks):
        progress(n, len(picks))
        try:
            last_error = None
            mbid = lookup.artist_mbid(artist)
            songs = [(*song, "listenbrainz") for song in lookup.popular_songs(mbid, album)]
            if len(songs) < songs_per_album:
                try:  # extra songs are a bonus: keep what ListenBrainz found if MusicBrainz is down
                    have = {bare(t) for t, *_ in songs}
                    songs += [(*song, "youtube views") for song in
                              rank_by_views(artist, album, lookup.tracklist(mbid, album)) if bare(song[0]) not in have]
                except Exception as exc:
                    if not songs:
                        raise
                    last_error = str(exc)
            got = 0
            for title, seconds, art, recording_mbid, source in songs:
                if got == songs_per_album:
                    break
                file = video = None
                for video in candidates(artist, title, seconds)[:3]:
                    try:  # YouTube sometimes refuses one video (403); try the next match
                        file = download(video["id"], folder / f"{len(playlist['tracks']) + 1:02d}")
                        break
                    except yt_dlp.utils.DownloadError as exc:
                        last_error = str(exc)
                if not file:
                    continue
                playlist["tracks"].append({
                    "artist": artist, "album": album, "title": title, "file": file,
                    "video_id": video["id"], "picked_by": source, "recording_mbid": recording_mbid,
                    "seconds": round(seconds or video.get("duration") or 0),
                    "art": art or f"https://i.ytimg.com/vi/{video['id']}/hqdefault.jpg",
                })
                got += 1
                save()
            if not got:
                playlist["missing"].append({"artist": artist, "album": album,
                                            "reason": last_error or "No matching songs found on YouTube."})
        except Exception as exc:  # one album failing shouldn't stop the rest
            playlist["missing"].append({"artist": artist, "album": album, "reason": str(exc)[:300]})
        save()
    playlist["state"] = "done"
    save()
    progress(len(picks), len(picks))
    return playlist
