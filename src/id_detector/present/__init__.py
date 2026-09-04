"""Presentation layer: flattened exports, the Stage 7 static page, and the local server."""

from __future__ import annotations

from id_detector.present.exports import (
    ExportResult,
    export_tracklist,
    flatten_tracklist,
    render_cue,
    render_m3u,
)
from id_detector.present.grouping import (
    DisplayTrack,
    group_display_tracks,
    normalise_title,
    work_key,
)
from id_detector.present.page import (
    DEFAULT_LEAD_IN_MS,
    EmbedPlan,
    generate_page,
    plan_embed,
    render_page,
    seek_argument,
    seek_target_ms,
)
from id_detector.present.server import (
    RunningServer,
    append_rescan_request,
    build_rescan_request,
    consume_rescan_queue,
    make_server,
    read_rescan_queue,
    rescan_queue_path,
    serve_in_background,
)

__all__ = [
    "DEFAULT_LEAD_IN_MS",
    "DisplayTrack",
    "EmbedPlan",
    "ExportResult",
    "RunningServer",
    "append_rescan_request",
    "build_rescan_request",
    "consume_rescan_queue",
    "export_tracklist",
    "flatten_tracklist",
    "generate_page",
    "group_display_tracks",
    "normalise_title",
    "work_key",
    "make_server",
    "plan_embed",
    "read_rescan_queue",
    "render_cue",
    "render_m3u",
    "render_page",
    "rescan_queue_path",
    "seek_argument",
    "seek_target_ms",
    "serve_in_background",
]
