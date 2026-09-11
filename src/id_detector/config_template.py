"""The single documented ``idea.toml`` template and the ``config show`` renderer.

``idea config init`` writes :data:`CONFIG_TEMPLATE` verbatim; the committed
``idea.example.toml`` is the same bytes (a test asserts they never drift).  No secret ever
belongs in this file — provider credentials are read only from environment variables (see
``.env.example``) and the logger redacts them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from id_detector.providers.base import HINT_CONNECTORS, AppConfig

CONFIG_TEMPLATE = """\
# IDea configuration.  This file holds only NON-SECRET runtime preferences.
#
# Secrets (SoundCloud/AudD/ACRCloud/Discogs credentials) are NEVER read from here; they come only
# from environment variables listed in .env.example, and logs redact them.
#
# Precedence, highest wins:
#   1. command-line flags (e.g. --max-requests, --profile, --max-generations, --no-hints)
#   2. a frozen --profile (or default_profile below): fixes the engines, the transform/schedule/
#      rescan geometry and the hint/novelty toggles.  EVERYTHING ELSE in this file still applies
#      under a profile -- max_requests, lead_in_ms, [recognise], [deep], [cache], [hints] and the
#      [present] dials (min_track_ms, collapse, same_track_bridge_ms).
#   3. the values in THIS file
#   4. built-in defaults (what you see below)
#
# Copy this to idea.toml (that name is git-ignored) and edit.  `idea config show` prints the
# effective, resolved configuration and `idea config show --profile free` shows exactly what a run
# under that profile uses, marking the lines the profile fixes; `idea config init` writes this
# template.

# Uploading third-party audio to AudD/ACRCloud requires BOTH this flag AND a per-command
# confirmation (--i-own-this-audio-or-have-permission).  Leave it false unless you own the audio
# or have permission.
allow_third_party_upload = false

# Profile used when you do not pass --profile.  Leave commented to run the built-in defaults below.
# A profile is an immutable, evidence-derived artefact under profiles/<name>-v<K>.json.
# default_profile = "free"

# Per-run Shazam request budget: a hard ceiling on billable/physical attempts (--max-requests wins).
max_requests = 2000

# Web-page / export seek lead-in: jump this many milliseconds BEFORE a track's proved start so the
# mix-in is audible.  The page also exposes a live control seeded from this value.
lead_in_ms = 5000

# Recognition pacing (speed only; never changes results, which are content-addressed).
# requests_per_minute is the CEILING rate for Shazam clip queries: the limiter starts here and
# automatically backs off when Shazam's free endpoint returns 429s, then climbs back when it calms
# down -- so a higher ceiling is safe, it self-tunes to whatever Shazam currently tolerates.
# concurrency is how many recognitions run at once.  Old behaviour was 18 / 1 (serial); the
# defaults below are a safe ~2-3x speed-up on a cold analysis.  Lower them if you see heavy
# throttling; the free Shazam endpoint is the hard ceiling, not this tool.
[recognise]
requests_per_minute = 45
concurrency = 3

# Per-process Shazam breaker (one direct egress). Daily budget is provisional until S3.
# Open: new Free requests are refused as waiting (exit 6, no local queue); an admitted Free
# run continues. Deep skips its remaining secondary and reports degraded.
# Explicit re-enable: INCREMENT reenable_generation, save, then submit the next job.
# The long-running local server reads it per job; it clears the latch/rate history once,
# preserving today's spent budget. No timer clears the latch. Process restart loses state.
# IDEA_ENGINE_SHAZAM=off is always a hard off, even after re-enable.
[shazam_breaker]
failure_rate_e4 = 3000
window_seconds = 300
cooldown_seconds = 1800
minimum_sample = 20
shazam_daily_budget_per_egress = 2000
latch_count = 3
reenable_generation = 0

# Deep scans use every frozen window by default.  Density 2 selects even-indexed windows and halves
# AudD request volume; because density affects results it also produces a distinct recipe_id.
# audd_requests_per_minute is the ceiling of the paid sweep's token bucket (4 clips in flight, per
# the recipe); it backs off by itself on a 429/503.  Speed only, never results.
[deep]
primary_density = 1
audd_requests_per_minute = 120

# Stage 4b transform hypotheses.  policy = "off" | "rescan_only" (default) | "global".
[transforms]
policy = "rescan_only"
rate_e4 = [9200, 9600, 10400, 10800]
semitones = [-2, -1, 1, 2]

# Generation-0 window schedule (plan rev 5.2): coverage-complete at the active measured L_min.
# window_ms must not exceed 12000; phase_ms must be smaller than hop_ms.
[schedule]
window_ms = 12000
hop_ms = 9000
phase_ms = 0

# Base rescan policy (plan rev 5.2): the Stage 4b denser schedule the Stage 4c generation loop
# consumes.  max_generations counts generations AFTER generation 0; 0 disables rescans.
#
# DEFAULT 0 (rescans OFF): on real DJ mixes the pitch/rate-transform rescans recover ~no tracks that
# generation 0 misses, while adding famous-track phantoms and 5-65x the request load -- one 46-min
# mix took ~3 hours and used the whole 2000-request budget on them.  Raise this (e.g. to 3) only if
# you have heavily pitched tracks generation 0 misses; it caps whatever a --profile enables, and
# --max-generations wins over both.
[rescan]
window_ms = 12000
hop_ms = 5000
phase_ms = 0
max_generations = 0

# Recognition cache TTLs (days).  A positive match is trusted this long; a no-match a shorter time;
# errors are never cached.  --refresh bypasses both.
[cache]
positive_max_age_days = 180
no_match_max_age_days = 30

# Text-hint connectors.  enabled = false is the same as --no-hints.  Set any connector below to
# false to turn just that one off.  Known connectors: {connectors}.
# tl1001 (the 1001tracklists title search) is off by default: the site is JS-gated, so the search
# rarely yields anything and contacts one more third party per run.  Set it to true to opt in.
[hints]
enabled = true
sc_comments = true
mixesdb = true
yt_comments = true
mixcloud = true
tl1001 = false
pointer_import = true

# Presentation.  collapse = true (the default, or --collapse) folds a contiguous run of competing
# near-duplicate matches of the same underlying track (e.g. six "Work (X Remix)" rows) into ONE
# tracklist / page row whose closest match is shown, with the other candidates listed as "could
# also be" alternatives.  collapse = false (or --no-collapse) emits the old one-row-per-episode
# view.  same_track_bridge_ms additionally stacks two appearances of the SAME exact track this far
# apart (default 180000 ms = 3 min) into one row, as long as no different confident track plays
# between them; a genuine repeat later in the set stays its own row.
[present]
collapse = true
same_track_bridge_ms = 180000
# min_track_ms: the shortest a low/medium-confidence match may play and still be LISTED as a track.
# Real tracks in a set run for minutes; most false positives are one fleeting ~12 s window.  A
# 30 s (30000) floor removes the bulk of false positives while keeping the real tracks; a strong
# (likely/verified) badge or a corroborating tracklist hint bypasses it.  Set 0 to list everything.
min_track_ms = 30000
""".replace("{connectors}", ", ".join(HINT_CONNECTORS))


def _toml_list(values: tuple[int, ...]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def render_effective_config(
    config: AppConfig,
    *,
    fixed_by: Mapping[str, str] | None = None,
    trailer: Sequence[str] = (),
) -> str:
    """Render the fully-resolved :class:`AppConfig` as readable TOML for ``config show``.

    ``fixed_by`` maps an :class:`AppConfig` field name to a short note (``fixed by profile
    "free"``) appended as a comment on that field's line, so a reader sees at a glance which
    values the profile decided and which the file did; ``trailer`` lines are appended verbatim
    (as comments) for what a profile fixes that no config line controls.  The output stays valid
    TOML: a test round-trips it through the loader.
    """

    fixed = dict(fixed_by or {})

    def line(field: str | None, text: str) -> str:
        note = fixed.get(field) if field is not None else None
        return f"{text}  # {note}" if note else text

    disabled = sorted(config.disabled_hint_connectors)
    lines = [
        "# Effective IDea configuration (resolved: file + profile + defaults).",
        "# Secrets are never shown here; they come only from environment variables.",
        line(
            "allow_third_party_upload",
            f"allow_third_party_upload = {str(config.allow_third_party_upload).lower()}",
        ),
        f"default_profile = {config.default_profile!r}"
        if config.default_profile
        else "# default_profile = (unset)",
        line("max_requests", f"max_requests = {config.max_requests}"),
        line("lead_in_ms", f"lead_in_ms = {config.lead_in_ms}"),
        "",
        "[deep]",
        line("deep_primary_density", f"primary_density = {config.deep_primary_density}"),
        line(
            "audd_requests_per_minute",
            f"audd_requests_per_minute = {config.audd_requests_per_minute}",
        ),
        "",
        "[recognise]",
        line(
            "shazam_requests_per_minute",
            f"requests_per_minute = {config.shazam_requests_per_minute}",
        ),
        line("recognise_concurrency", f"concurrency = {config.recognise_concurrency}"),
        "",
        "[shazam_breaker]",
        *(f"{name} = {value}" for name, value in vars(config.shazam_breaker).items()),
        "",
        "[transforms]",
        line("transforms_policy", f'policy = "{config.transforms_policy}"'),
        line("transform_rates_e4", f"rate_e4 = {_toml_list(config.transform_rates_e4)}"),
        line("transform_semitones", f"semitones = {_toml_list(config.transform_semitones)}"),
        "",
        "[schedule]",
        line("window_ms", f"window_ms = {config.window_ms}"),
        line("hop_ms", f"hop_ms = {config.hop_ms}"),
        line("phase_ms", f"phase_ms = {config.phase_ms}"),
        "",
        "[rescan]",
        line("rescan_window_ms", f"window_ms = {config.rescan_window_ms}"),
        line("rescan_hop_ms", f"hop_ms = {config.rescan_hop_ms}"),
        line("rescan_phase_ms", f"phase_ms = {config.rescan_phase_ms}"),
        line("rescan_max_generations", f"max_generations = {config.rescan_max_generations}"),
        "",
        "[cache]",
        # Launch-controlled in pricing.toml, shown here as a comment so `config show` stays a
        # complete picture without implying the owner's file could set it.
        f"# serve_free_from_deep = {str(config.serve_free_from_deep).lower()}  (pricing.toml)",
        line(
            "cache_positive_max_age_days",
            f"positive_max_age_days = {config.cache_positive_max_age_days}",
        ),
        line(
            "cache_no_match_max_age_days",
            f"no_match_max_age_days = {config.cache_no_match_max_age_days}",
        ),
        "",
        "[hints]",
        line("hints_enabled", f"enabled = {str(config.hints_enabled).lower()}"),
    ]
    for connector in HINT_CONNECTORS:
        lines.append(
            line(
                "disabled_hint_connectors",
                f"{connector} = {str(connector not in disabled).lower()}",
            )
        )
    lines.extend(
        [
            "",
            "[present]",
            line("collapse", f"collapse = {str(config.collapse).lower()}"),
            line("same_track_bridge_ms", f"same_track_bridge_ms = {config.same_track_bridge_ms}"),
            line("present_min_track_ms", f"min_track_ms = {config.present_min_track_ms}"),
        ]
    )
    if trailer:
        lines.append("")
        lines.extend(trailer)
    return "\n".join(lines) + "\n"
