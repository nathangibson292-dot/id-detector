"""The single documented ``id-detector.toml`` template and the ``config show`` renderer.

``id-detector config init`` writes :data:`CONFIG_TEMPLATE` verbatim; the committed
``id-detector.example.toml`` is the same bytes (a test asserts they never drift).  No secret ever
belongs in this file — provider credentials are read only from environment variables (see
``.env.example``) and the logger redacts them.
"""

from __future__ import annotations

from id_detector.providers.base import HINT_CONNECTORS, AppConfig

CONFIG_TEMPLATE = """\
# id-detector configuration.  This file holds only NON-SECRET runtime preferences.
#
# Secrets (SoundCloud/AudD/ACRCloud/Discogs credentials) are NEVER read from here; they come only
# from environment variables listed in .env.example, and logs redact them.
#
# Precedence, highest wins:
#   1. command-line flags (e.g. --max-requests, --profile, --max-generations, --no-hints)
#   2. a frozen --profile: fixes the engines and the transform/schedule/rescan geometry and the
#      hint/novelty toggles (but this file still supplies lead_in_ms, max_requests, cache and the
#      per-connector hint switches)
#   3. the values in THIS file
#   4. built-in defaults (what you see below)
#
# Copy this to id-detector.toml (that name is git-ignored) and edit.  `id-detector config show`
# prints the effective, resolved configuration; `id-detector config init` writes this template.

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
[rescan]
window_ms = 12000
hop_ms = 5000
phase_ms = 0
max_generations = 3

# Recognition cache TTLs (days).  A positive match is trusted this long; a no-match a shorter time;
# errors are never cached.  --refresh bypasses both.
[cache]
positive_max_age_days = 180
no_match_max_age_days = 30

# Text-hint connectors.  enabled = false is the same as --no-hints.  Set any connector below to
# false to turn just that one off.  Known connectors: {connectors}.
[hints]
enabled = true
sc_comments = true
mixesdb = true
yt_comments = true
mixcloud = true
tl1001 = true
pointer_import = true

# Presentation.  collapse = true (the default, or --collapse) folds a contiguous run of competing
# near-duplicate matches of the same underlying track (e.g. six "Work (X Remix)" rows) into ONE
# tracklist / page row whose closest match is shown, with the other candidates listed as "could
# also be" alternatives.  collapse = false (or --no-collapse) emits the old one-row-per-episode
# view.
[present]
collapse = true
""".replace("{connectors}", ", ".join(HINT_CONNECTORS))


def _toml_list(values: tuple[int, ...]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def render_effective_config(config: AppConfig) -> str:
    """Render the fully-resolved :class:`AppConfig` as readable TOML for ``config show``."""

    disabled = sorted(config.disabled_hint_connectors)
    lines = [
        "# Effective id-detector configuration (resolved: file + profile + defaults).",
        "# Secrets are never shown here; they come only from environment variables.",
        f"allow_third_party_upload = {str(config.allow_third_party_upload).lower()}",
        f"default_profile = {config.default_profile!r}"
        if config.default_profile
        else "# default_profile = (unset)",
        f"max_requests = {config.max_requests}",
        f"lead_in_ms = {config.lead_in_ms}",
        "",
        "[transforms]",
        f'policy = "{config.transforms_policy}"',
        f"rate_e4 = {_toml_list(config.transform_rates_e4)}",
        f"semitones = {_toml_list(config.transform_semitones)}",
        "",
        "[schedule]",
        f"window_ms = {config.window_ms}",
        f"hop_ms = {config.hop_ms}",
        f"phase_ms = {config.phase_ms}",
        "",
        "[rescan]",
        f"window_ms = {config.rescan_window_ms}",
        f"hop_ms = {config.rescan_hop_ms}",
        f"phase_ms = {config.rescan_phase_ms}",
        f"max_generations = {config.rescan_max_generations}",
        "",
        "[cache]",
        f"positive_max_age_days = {config.cache_positive_max_age_days}",
        f"no_match_max_age_days = {config.cache_no_match_max_age_days}",
        "",
        "[hints]",
        f"enabled = {str(config.hints_enabled).lower()}",
    ]
    for connector in HINT_CONNECTORS:
        lines.append(f"{connector} = {str(connector not in disabled).lower()}")
    lines.extend(["", "[present]", f"collapse = {str(config.collapse).lower()}"])
    return "\n".join(lines) + "\n"
