# Research: ingesting mix audio + "where to get it" sources

*Compiled 2026-09-03. Web research; URLs verified at time of writing.*

## Part A — Ingesting the mix audio

### yt-dlp (primary) and scdl (alternative)

**Site support** (confirmed in [supportedsites.md](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)):
- SoundCloud: `soundcloud`, `soundcloud:playlist`, `soundcloud:set`, `soundcloud:user`, `soundcloud:search` (`scsearch:` prefix), `SoundcloudEmbed`
- Mixcloud: `mixcloud`, `mixcloud:playlist`, `mixcloud:user`
- Bandcamp: `Bandcamp`, `Bandcamp:album`, `Bandcamp:user`
- YouTube: `youtube`, `youtube:playlist`, `youtube:search` (`ytsearch:`)
- No Hypeddit extractor ([issue #7948](https://github.com/yt-dlp/yt-dlp/issues/7948) open since 2023).

**Maintenance:** very active — releases 2026.08.19, 2026.07.04, 2026.06.09, 2026.03.17. 2026.02.21 fixed SoundCloud client_id extraction; 2026.08.19 added Bandcamp impersonation. Use the **nightly** channel.

**Audio-only flags:**
```
yt-dlp -f "ba/b" -x --audio-format best --write-info-json --write-description --write-comments --embed-metadata -o "%(id)s.%(ext)s" URL
```
Keep `--audio-format best` so nothing is transcoded before fingerprinting. `--write-info-json` gives `chapters`, `description`, `comments`.

**SoundCloud specifics** (from [extractor source](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/yt_dlp/extractor/soundcloud.py)):
- client_id is scraped from soundcloud.com JS assets, cached, re-fetched on 401/403. No account needed for public tracks.
- Endpoints: `api-v2.soundcloud.com/resolve`, `tracks/{id}`, `tracks/{id}/download`, `search/tracks`, `playlists/{id}`.
- Default formats: `http_aac, hls_aac, http_opus, hls_opus, http_mp3, hls_mp3`. If `downloadable && has_downloads_left`, a `download` format (original upload, often WAV/AIFF) is added.
- **Go+ tracks:** unauthenticated → 30-second preview only ([issue #8390](https://github.com/yt-dlp/yt-dlp/issues/8390)). Full access via `--username oauth --password <oauth_token>` or `--cookies-from-browser`. Username/password login is disabled.

**YouTube specifics:** datacenter IPs hit "Sign in to confirm you're not a bot". Mitigations: `--cookies-from-browser`, PO tokens, `--extractor-args "youtube:player_client=tv,web_safari"`, residential egress. Fine on a home machine.

**scdl** ([github](https://github.com/scdl-org/scdl), 4.1k stars): now a thin wrapper over yt-dlp's SoundCloud path; development "not active". Not needed.

### Metadata and tracklist hints

**SoundCloud official API (2026 status):**
- Self-serve registration returned May 2026 but **requires Artist Pro ($99/yr)** — [register-app](https://developers.soundcloud.com/docs/api/register-app), [announcement](https://developers.soundcloud.com/blog/vibe-coding-ai-agent-docs-self-serve-api-keys/), [OpenAPI spec](https://github.com/soundcloud/api/blob/master/openapi/api.yaml).
- OAuth 2.1; `client_credentials` suffices for public search/resolve. Limits: 15,000 stream req/24h; 50 tokens/12h.
- Track object fields of interest: `downloadable`, `download_url`, `download_count`, `purchase_url`, `purchase_title`, `license`, `description`, `access`.
- Unofficial `api-v2` + scraped client_id route (what yt-dlp does) works without paying — ToS grey area (SoundCloud ToU prohibits scraping/aggregation).

**Do SoundCloud mixes carry tracklists?** Yes — in descriptions and, more usefully, in **timed comments** ("ID - ID" convention; ID hunters post timestamped answers). Parse both.

**YouTube:** descriptions and pinned comments very often carry `HH:MM:SS - Artist - Title` lines. yt-dlp surfaces `chapters[]` (start_time/end_time/title) and, with `--write-comments`, comments with `is_pinned`, `author_is_uploader`, `like_count`, `text`. Cap with `--extractor-args "youtube:comment_sort=top;max_comments=100,all,10,5"`.

**Mixcloud:** free read API, no auth — swap `www.mixcloud.com/` → `api.mixcloud.com/`. Cloudcasts expose `sections[]` with `artist`, `song`, `start_time` when the uploader entered a tracklist. ([developers](https://www.mixcloud.com/developers/))

**1001tracklists:** no API. robots.txt allows `/tracklist/` with 8-second crawl-delay. Scrapers: [leandertolksdorf/1001-tracklists-api](https://github.com/leandertolksdorf/1001-tracklists-api), [GodLesZ/1001tracklists-scraper](https://github.com/GodLesZ/1001tracklists-scraper). Expect Cloudflare friction.

**MixesDB:** MediaWiki — API live at `https://www.mixesdb.com/w/api.php` with standard `query`/`parse`/`opensearch` plus custom `mixesdbtrackidquery`, `mixesdb_player_search`, `mixesdbsearchtypeahead`. Also hosts [Help:Tracklist_Generation_Tools](https://www.mixesdb.com/w/Help:Tracklist_Generation_Tools), a community catalogue of every ID tool.

## Part B — Where a track can be acquired

### SoundCloud free downloads and gates
- Native: `downloadable: true` + `download_url` (official) or `downloadable && has_downloads_left` → `api-v2 /tracks/{id}/download`. yt-dlp pulls the original automatically.
- `purchase_url`/`purchase_title` holds the gate link (Hypeddit etc.) — surface as a "Free DL / Buy" button.
- **Gate landscape collapsed in 2026:** SoundCloud paused Hypeddit's (and other gate tools') API on 11 June 2026, so follow/like/repost are no longer enforced ([edm.com](https://edm.com/news/soundcloud-cuts-hypeddit-api-access-free-download-campaigns/)). ToneDen shut down 2024. The Artist Union shut down 2020. Newer gates: BetterGate, TimbrGate, Backstaged, Fangate.eu, Stillhype.
- No public gate API. Automation tools exist ([hypeddit-dl](https://pypi.org/project/hypeddit-dl/), [hypeddit-skip](https://github.com/HypedditSkip/hypeddit-skip)) but Hypeddit ToS §2.1(vi) forbids bots. **Decision: link out, don't automate.**

### Bandcamp
- Search: `https://bandcamp.com/search?q=<artist title>&item_type=t`. Plain fetch now returns a JS "Client Challenge"; [bandcamp-fetch](https://github.com/patrickkfkan/bandcamp-fetch) v3 needs a session cookie. Scrapers use `POST /api/bcsearch_public_api/1/autocomplete_elastic`.
- Libraries: bandcamp-fetch (TS), [fabi321/bandcamp](https://github.com/fabi321/bandcamp) (`bandcamp_lib`), [bandcamp-dl](https://github.com/iheanyi/bandcamp-dl), [bandcamp_name_your_price_dl](https://github.com/Layerex/bandcamp_name_your_price_dl).
- "Name your price" with $0 minimum = legal free download. Deep-link: `?action=download` / `?action=buy`.

### Beatport / Traxsource / Juno
- **Beatport v4:** OAuth at `api.beatport.com/v4`, [Swagger](https://api.beatport.com/v4/docs/), partner-gated; individuals get "No Access". Community workaround: scrape client_id + log in ([gist](https://gist.github.com/kemo/506ca56e35b9506ee5233bc4d773c1c8), [beets-beatport4](https://pypi.org/project/beets-beatport4/)). Link-out: `https://www.beatport.com/search/tracks?q=<q>`. Site 403s non-browser fetches.
- **Traxsource:** no public API. Link-out `https://www.traxsource.com/search?term=<q>`.
- **Juno Download shut down 1 June 2026** — drop.

### Free-legal aggregators
FMA API shut down. Jamendo v3 needs free client_id. ccMixter open. Low relevance for club music — skip.

### Streaming lookup for confirmation/linking
- **Deezer** — no auth, ~50 req/5s, `https://api.deezer.com/search?q=artist:"X" track:"Y"` → id/title/link/preview/duration/cover. **Best zero-friction option.**
- **iTunes Search** — no auth, ~20/min, `https://itunes.apple.com/search?term=&media=music&entity=song` → `trackViewUrl`, `previewUrl`.
- **MusicBrainz** — no auth, 1 req/s, descriptive User-Agent required. Weak for promos/white labels.
- **Discogs** — personal token (free, instant), 60/min. Best for vinyl/catno.
- **Spotify** — **avoid**: since Feb 2026 Dev Mode = 5 users, owner needs Premium, client-credentials being phased out ([blog](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security)).
- **Odesli/song.link** — **dead** for new integrations (401 today, retirement announced).

## Part C — Player / seek UX
- **SoundCloud Widget API** ([docs](https://developers.soundcloud.com/docs/api/html5-widget)): load `https://w.soundcloud.com/player/api.js`, `SC.Widget(iframe)`, on `READY` → `seekTo(ms)`, `play()`, `getPosition(cb)`, `PLAY_PROGRESS`. Embedding a single third-party mix per analysis page is within ToU; a browsable catalogue of embeds is not.
- **YouTube IFrame API** ([docs](https://developers.google.com/youtube/iframe_api_reference)): `player.seekTo(seconds, true)`, `getCurrentTime()`, `onStateChange`; use `enablejsapi=1&origin=<host>`. Min 200×200, no overlays.
- **Mixcloud** also has an embed widget with play/pause/seek JS API.

## Recommended ingestion stack
1. `yt-dlp` (nightly) as the sole downloader. `-f ba -x --audio-format best --write-info-json --write-description --write-comments`. Optional user-supplied SoundCloud OAuth token for Go+/private.
2. Hint extraction from info.json: `chapters`, `description`, `comments` (filter pinned/uploader/timestamped). SoundCloud timed comments via api-v2. Mixcloud `sections[]`.
3. Optional: MixesDB API, 1001tracklists polite scrape.
4. Fingerprint on the raw stream, confirm via Deezer → iTunes → MusicBrainz/Discogs.

## "Where to get it" sources, ranked by ease (no auth)
1. Deezer search API
2. iTunes/Apple Music Search API
3. MusicBrainz
4. Mixcloud API (metadata only)
5. SoundCloud `downloadable`/`purchase_url` via api-v2 (grey) or official with Artist Pro
6. Discogs (personal token)
7. Bandcamp — link-out trivial; programmatic needs cookie/browser
8. Beatport — link-out trivial; API partner-gated
9. Traxsource — link-out only
10. Hypeddit/gates — link-out only
11. Odesli — dead
12. Spotify — skip
13. Juno Download — closed
