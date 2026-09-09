#!/usr/bin/env sh
# S2 spike (plan §5 Phase S) — OWNER-RUN, LIVE: can this host's egress fetch a SoundCloud, a
# Mixcloud and a YouTube mix through yt-dlp, and does a proxy change the YouTube answer (R2)?
#
#     scripts/spike_ingest_vps.sh <sc-url> <mc-url> <yt-url> [proxy]
#
# Each case runs the real ingest steps — metadata (--skip-download) and then a full audio download
# into a temp dir that is deleted afterwards — and appends one dated section to
# docs/spikes/ingest-vps.md.  The report never records URLs, titles or uploader handles (the
# fixture audit scans docs/), only the platform label, extractor, duration, exit codes, bytes,
# seconds and a sanitised error line.  Never run in CI: it downloads real media from real platforms.
set -u

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "usage: $0 <sc-url> <mc-url> <yt-url> [proxy]" >&2
    exit 2
fi
if [ "${IDEA_TEST_MODE:-}" = "1" ] || [ -n "${PYTEST_CURRENT_TEST:-}" ]; then
    # Same guard as spike_shazam_vps.py: never reachable from the suite or CI.
    echo "refusing to run a live ingest spike in a test context" >&2
    exit 2
fi

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out="$repo_root/docs/spikes/ingest-vps.md"
sc_url=$1
mc_url=$2
yt_url=$3
proxy=${4:-}
tmp=$(mktemp -d "${TMPDIR:-/tmp}/idea-spike-ingest.XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT INT TERM

# The repository pins yt-dlp; prefer it over whatever the host has on PATH.
if command -v uv >/dev/null 2>&1 && [ -f "$repo_root/pyproject.toml" ]; then
    ytdlp="uv run --project $repo_root yt-dlp"
else
    ytdlp="yt-dlp"
fi

run_timeout() {
    # coreutils timeout is not universal (macOS); without it the case simply runs unbounded.
    if command -v timeout >/dev/null 2>&1; then
        timeout "$@"
    else
        shift
        "$@"
    fi
}

sanitise() {
    # Strip anything the fixture audit would flag or that identifies a person: URLs, @handles,
    # long digit runs (platform IDs) and table pipes; keep the line short.
    sed -e 's#https\{0,1\}://[^ ]*#<url>#g' -e 's#www\.[^ ]*#<url>#g' \
        -e 's#@[A-Za-z0-9_][A-Za-z0-9_]*#@…#g' -e 's#[0-9]\{6,\}#<id>#g' \
        | tr -d '|' | cut -c1-160
}

proxy_host() {
    # Only the proxy's host reaches the report — never its credentials or path.
    printf '%s' "$1" | sed -e 's#^[a-z0-9]*://##' -e 's#^[^@]*@##' -e 's#[/:].*$##'
}

case_row() { # label url egress proxy_arg
    label=$1
    url=$2
    egress=$3
    proxy_arg=$4
    meta_start=$(date +%s)
    # shellcheck disable=SC2086  # $ytdlp and $proxy_arg are intentionally word-split
    meta_out=$(run_timeout 180 $ytdlp --skip-download --no-playlist \
        --print "%(extractor)s|%(duration)s" $proxy_arg "$url" 2>"$tmp/err-$label-meta")
    meta_rc=$?
    meta_s=$(($(date +%s) - meta_start))
    extractor=$(printf '%s' "$meta_out" | head -n1 | cut -d'|' -f1 | sanitise)
    duration=$(printf '%s' "$meta_out" | head -n1 | cut -d'|' -f2 | tr -cd '0-9.')
    dl_start=$(date +%s)
    # shellcheck disable=SC2086
    run_timeout 900 $ytdlp --no-playlist -f "bestaudio/best" -o "$tmp/$label.%(ext)s" \
        $proxy_arg "$url" >/dev/null 2>"$tmp/err-$label-dl"
    dl_rc=$?
    dl_s=$(($(date +%s) - dl_start))
    bytes=0
    for file in "$tmp/$label".*; do
        [ -f "$file" ] || continue
        bytes=$((bytes + $(wc -c <"$file")))
    done
    rm -f "$tmp/$label".*
    error=$(cat "$tmp/err-$label-meta" "$tmp/err-$label-dl" 2>/dev/null | grep -i 'error' \
        | tail -n1 | sanitise)
    printf '| %s | %s | %s | %s | %s | %s | %s | %s | %s |\n' \
        "$label" "$egress" "${extractor:-?}" "${duration:-?}" "$meta_rc" "$dl_rc" \
        "$((meta_s + dl_s))" "$bytes" "${error:-}"
}

mkdir -p "$(dirname "$out")"
{
    printf '\n## %s — host %s\n\n' "$(date -u +%Y-%m-%dT%H:%MZ)" "$(hostname | sanitise)"
    # shellcheck disable=SC2086
    printf -- '- yt-dlp: %s\n' "$($ytdlp --version 2>/dev/null | head -n1 | sanitise)"
    if [ -n "$proxy" ]; then
        printf -- '- proxy host: %s\n\n' "$(proxy_host "$proxy" | sanitise)"
    else
        printf -- '- proxy: none\n\n'
    fi
    printf '| case | egress | extractor | duration_s | meta_rc | dl_rc | seconds | bytes | error |\n'
    printf '|---|---|---|---:|---:|---:|---:|---:|---|\n'
    case_row soundcloud "$sc_url" direct ""
    case_row mixcloud "$mc_url" direct ""
    case_row youtube "$yt_url" direct ""
    if [ -n "$proxy" ]; then
        case_row youtube "$yt_url" proxy "--proxy $proxy"
    fi
} | tee -a "$out"
echo "appended to $out"
