"""Local playlist snapshots. Callers hold LOCK across load, mutation and save."""

import copy
import json
import os
import secrets
import threading
import time
from pathlib import Path

from id_detector.io import atomic_write_json, fsync_directory

LOCK = threading.Lock()


def now_ms() -> int:
    return int(time.time() * 1000)


def default_store_path(work_root: Path) -> Path:
    override = os.environ.get("IDEA_PLAYLISTS_PATH")
    path = Path(override) if override else work_root.parent / "data/local/playlists.json"
    if override and not path.is_absolute():
        raise ValueError("IDEA_PLAYLISTS_PATH must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def ensure_likes(store: dict) -> dict:
    playlists = store.setdefault("playlists", [])
    likes = next((p for p in playlists if p.get("id") == "likes"), None)
    if likes is None:
        likes = {"id": "likes", "created_ms": 0, "tracks": []}
    likes.update(name="Likes", builtin=True)
    likes.setdefault("tracks", [])
    store["playlists"] = [likes] + [p for p in playlists if p.get("id") != "likes"]
    store.setdefault("version", 1)
    return store


def load_store(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError("invalid store")
        playlists = result["playlists"]
        if not isinstance(playlists, list):
            raise ValueError("invalid playlists")
        for playlist in playlists:
            if not isinstance(playlist, dict):
                raise ValueError("invalid playlist")
            if not all(isinstance(playlist.get(k), str) for k in ("id", "name")):
                raise ValueError("invalid playlist identity")
            if not isinstance(playlist.get("created_ms"), int):
                raise ValueError("invalid timestamp")
            if not isinstance(playlist.get("tracks"), list):
                raise ValueError("invalid tracks")
            for track in playlist["tracks"]:
                if not isinstance(track, dict) or not isinstance(track.get("candidate_id"), str):
                    raise ValueError("invalid track")
                if not isinstance(track.get("sources"), list) or not all(
                    isinstance(s, dict) for s in track["sources"]
                ):
                    raise ValueError("invalid sources")
        return ensure_likes(result)
    except (OSError, ValueError, KeyError, TypeError):
        return ensure_likes({"version": 1, "playlists": []})


def save_store(path: Path, store: dict) -> None:
    atomic_write_json(path, store)
    fsync_directory(path.resolve().parent)


def list_playlists(store: dict) -> list[dict]:
    ensure_likes(store)
    ordered = sorted(store["playlists"], key=lambda p: (p["id"] != "likes", p["created_ms"]))
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "builtin": p.get("builtin", False),
            "count": len(p["tracks"]),
        }
        for p in ordered
    ]


def get_playlist(store: dict, playlist_id: str) -> dict | None:
    ensure_likes(store)
    return next((p for p in store["playlists"] if p["id"] == playlist_id), None)


def _name(name: str) -> str:
    if not name.strip():
        raise ValueError("playlist name is required")
    return name.strip()[:80]


def _playlist(store: dict, playlist_id: str) -> dict:
    playlist = get_playlist(store, playlist_id)
    if playlist is None:
        raise ValueError("playlist not found")
    return playlist


def create_playlist(store: dict, name: str) -> tuple[dict, str]:
    name = _name(name)
    ensure_likes(store)
    key = secrets.token_hex(8)
    store["playlists"].append(
        {"id": key, "name": name, "builtin": False, "created_ms": now_ms(), "tracks": []}
    )
    return store, key


def rename_playlist(store: dict, playlist_id: str, name: str) -> dict:
    playlist = _playlist(store, playlist_id)
    if playlist_id == "likes":
        raise ValueError("cannot rename Likes")
    playlist["name"] = _name(name)
    return store


def delete_playlist(store: dict, playlist_id: str) -> dict:
    playlist = _playlist(store, playlist_id)
    if playlist_id == "likes":
        raise ValueError("cannot delete Likes")
    store["playlists"].remove(playlist)
    return store


def add_track(store: dict, playlist_id: str, track: dict) -> dict:
    playlist = _playlist(store, playlist_id)
    candidate = track.get("candidate_id")
    if not candidate:
        raise ValueError("candidate_id is required")
    existing = next((t for t in playlist["tracks"] if t["candidate_id"] == candidate), None)
    if existing is None:
        existing = {"candidate_id": candidate, "added_ms": now_ms(), "sources": []}
        playlist["tracks"].append(existing)
    # Latest authoritative snapshot wins; preserve acquisition data if a later run lacks it.
    for field in ("artist", "title", "badge", "version_status", "acquire"):
        if track.get(field) is not None or field not in existing:
            existing[field] = copy.deepcopy(track.get(field))
    source = copy.deepcopy(track.get("source") or {})
    keys = ("source_key", "media_key", "episode_id")
    if not any(all(s.get(k) == source.get(k) for k in keys) for s in existing["sources"]):
        existing["sources"].append(source)
    return store


def remove_track(store: dict, playlist_id: str, candidate_id: str) -> dict:
    playlist = _playlist(store, playlist_id)
    playlist["tracks"] = [t for t in playlist["tracks"] if t["candidate_id"] != candidate_id]
    return store


def toggle_like(store: dict, track: dict) -> tuple[dict, bool]:
    likes = _playlist(store, "likes")
    liked = any(t["candidate_id"] == track.get("candidate_id") for t in likes["tracks"])
    if liked:
        remove_track(store, "likes", track["candidate_id"])
    else:
        add_track(store, "likes", track)
    return store, not liked


def state(store: dict) -> dict:
    summaries = list_playlists(store)
    membership: dict[str, list[str]] = {}
    for playlist in store["playlists"]:
        for track in playlist["tracks"]:
            membership.setdefault(track["candidate_id"], []).append(playlist["id"])
    return {
        "playlists": summaries,
        "liked": [t["candidate_id"] for t in store["playlists"][0]["tracks"]],
        "membership": membership,
    }
