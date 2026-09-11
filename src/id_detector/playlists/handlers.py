"""Framework independent dispatch; the caller must validate CSRF and Origin for POSTs."""

import json
import re
from collections.abc import Mapping
from pathlib import Path

from . import store, view


def validate_source(source_key: str, media_key: str, episode_id: str) -> None:
    if not all(re.fullmatch(r"[0-9a-f]+", k) for k in (source_key, media_key)):
        raise ValueError("invalid source or media key")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", episode_id):
        raise ValueError("invalid episode_id")


def snapshot_track(
    work_root: Path, source_key: str, media_key: str, episode_id: str
) -> dict | None:
    validate_source(source_key, media_key, episode_id)
    # Lazy import: bundles imports page, which imports the playlist assets.
    from id_detector.present.bundles import result_dir

    media_dir = work_root / source_key / media_key
    if not media_dir.resolve().is_relative_to(work_root.resolve()):
        raise ValueError("invalid media path")
    try:
        payload = json.loads((result_dir(media_dir) / "tracklist.json").read_text(encoding="utf-8"))
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        entry = next(
            e for e in entries if e.get("episode_id") == episode_id and e.get("kind") == "track"
        )
        snapshot = {
            k: entry.get(k)
            for k in ("candidate_id", "artist", "title", "badge", "version_status", "acquire")
        }
        snapshot["candidate_id"] = snapshot["candidate_id"] or episode_id
    except (OSError, ValueError, TypeError, AttributeError, StopIteration):
        return None
    mix_title = ""
    try:
        source = json.loads((media_dir / "ingest/source.json").read_text(encoding="utf-8"))
        mix_title = source.get("title") or ""
    except (OSError, ValueError, AttributeError):
        pass
    snapshot["source"] = {
        "source_key": source_key,
        "media_key": media_key,
        "episode_id": episode_id,
        "start_ms": entry.get("start_ms"),
        "mix_title": mix_title,
    }
    return snapshot


def _json(status: int, **payload) -> tuple[int, bytes, str]:
    return status, json.dumps(payload).encode("utf-8"), "application/json"


def handle_get(
    route: str, query: Mapping[str, list[str]], *, work_root: Path
) -> tuple[int, bytes, str]:
    with store.LOCK:
        data = store.load_store(store.default_store_path(work_root))
    if route == "/playlists/state":
        return _json(200, **store.state(data))
    body = None
    if route == "/playlists":
        body = view.render_index(data)
    elif route.startswith("/playlists/p/"):
        body = view.render_playlist(data, route.removeprefix("/playlists/p/"))
    return (
        200 if body is not None else 404,
        body if body is not None else view.render_not_found(),
        "text/html; charset=utf-8",
    )


def handle_post(route: str, form: Mapping[str, str], *, work_root: Path) -> tuple[int, bytes, str]:
    action = route.removeprefix("/playlists/")
    if route != f"/playlists/{action}" or action not in {
        "like",
        "add",
        "remove",
        "create",
        "rename",
        "delete",
    }:
        return _json(405, ok=False, error="unknown action")
    try:
        track = None
        if action in {"like", "add"}:
            keys = [form.get(k, "") for k in ("source_key", "media_key", "episode_id")]
            validate_source(*keys)
            if not form.get("candidate_id", ""):
                raise ValueError("candidate_id is required")
            track = snapshot_track(work_root, *keys)
            if track is None:
                return _json(404, ok=False, error="track not found")
            if track["candidate_id"] != form.get("candidate_id", ""):
                raise ValueError("track identity changed; reload the mix")
        with store.LOCK:
            path = store.default_store_path(work_root)
            data = store.load_store(path)
            key = form.get("playlist_id", "")
            if action in {"add", "remove", "rename", "delete"}:
                if not key:
                    raise ValueError("playlist_id is required")
                if not (action == "add" and key == "__new__") and not store.get_playlist(data, key):
                    return _json(404, ok=False, error="playlist not found")
            extra = {}
            if action == "create":
                _, key = store.create_playlist(data, form.get("name", ""))
                extra = {"id": key, "name": store.get_playlist(data, key)["name"]}
            elif action == "like":
                _, liked = store.toggle_like(data, track)
                extra = {"liked": liked}
            elif action == "add":
                if key == "__new__":
                    _, key = store.create_playlist(data, form.get("name", ""))
                store.add_track(data, key, track)
                extra = {"playlist_id": key, "liked_count": len(store.state(data)["liked"])}
            elif action == "remove":
                candidate = form.get("candidate_id", "")
                if not candidate:
                    raise ValueError("candidate_id is required")
                store.remove_track(data, key, candidate)
            elif action == "rename":
                store.rename_playlist(data, key, form.get("name", ""))
            elif action == "delete":
                store.delete_playlist(data, key)
            store.save_store(path, data)
        return _json(200, ok=True, **extra)
    except ValueError as exc:
        return _json(400, ok=False, error=str(exc))
    except OSError:
        return _json(500, ok=False, error="could not save playlists")
