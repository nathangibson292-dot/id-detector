# Research: track IDs in comments and descriptions (SoundCloud, YouTube, Mixcloud, MixesDB, 1001tracklists)

*Compiled 2026-09-03. Verified live: api-v2 pulls of ~7,000 comments across six DJ sets, yt-dlp dumps, GraphQL/REST probes, MixesDB/1001tracklists API calls. Raw corpora are kept locally in `data/raw/comments/` (git-ignored); committed fixtures are derived — see [data/fixtures/README.md](../../data/fixtures/README.md).*

## 1. SoundCloud comments — exact API shape

**Unofficial api-v2 (verified live):**
`GET https://api-v2.soundcloud.com/tracks/{track_id}/comments?client_id=…&threaded=0&limit=200&offset=0`

- Top-level: `collection`, `next_href`, `query_urn`. `next_href` is offset-based and **drops `client_id`** — re-append it. `limit` clamps at 200.
- Comment keys: `body, created_at, id, kind, self{urn}, timestamp, track_id, user, user_id`. **`timestamp` is ms into the track.** Example: `{"body":"tiktok got me here","created_at":"2026-09-03T09:01:40Z","timestamp":1785933,"track_id":189305064,"user_id":795467125}`.
- `user` keys include `permalink, username, verified, followers_count, full_name`. Uploader detection = `comment.user_id == track.user_id`.
- **No threading.** `threaded=1`, `filter_replies=1`, `sort=` all returned identical results unauthenticated. There is no parent/reply field. Replies are plain comments whose body starts with `@<permalink>` (profile slug, not display name): `@user-715295512: Kaytranada - AT ALL`, `@ben-bohmer that's your year, mate!`. Across 6,584 comments, zero replies shared an exact timestamp with another comment — a reply gets its own playhead position.
- `client_id` extraction: fetch `https://soundcloud.com/`, collect `https://a-v2.sndcdn.com/assets/*.js`, regex `client_id\s*:\s*"([0-9a-zA-Z]{32})"`.

**yt-dlp `--write-comments` works for SoundCloud.** Live run on Dekmantel Podcast 267 returned 510 comments with keys `[author, author_id, author_is_verified, author_thumbnail, author_url, end_time, id, start_time, text, timestamp]`. `start_time`/`end_time` = track position in seconds; `timestamp` = created_at unix. No `parent`, no `like_count` (SoundCloud has neither).

**Official v1 API:** `GET /tracks/{track_urn}/comments` with `limit`, `offset`, `linked_partitioning`. Same fields. Requires OAuth + Artist Pro.

**Open-source SC-comment ID parsers:** none of substance exist. This is greenfield.

## 2. Social conventions — measured on six sets (~7,000 comments)

Sets: Solomun Boiler Room Tulum (2,578), Fred again.. Boiler Room London (1,282), Ben Böhmer Cercle Cappadocia (1,493), Kaytranada Boiler Room (627), Dekmantel 267 Djrum (404), Kuko HÖR Berlin (604). API returns ~80–85% of `comment_count` (deleted/hidden are counted).

**Questions** = 6–10% of comments. Verbatim: `ID?`, `id???`, `track id?`, `track id pls?`, `Song???`, `track?!`, `What's the last track?`, `Anyone knows the ID here ?`, `does anyone know this song? not the anunaku one`. Inline-time variants: `Track at 1.02?`, `Track id at 22:48?`, `52:38 anyone know the track id?`, `anyone know the tune around 43:00?`.

**Answers** are rare and mostly standalone: `Song is Baby - clock opera remix`, `red dressed - worakls` (title-first), `I make changes - Dave ft Headie One` (inverted), `Track ID: SOL (Ben Böhmer Remix) by Pryda`, `@johanna-meyer die tonight ~ kuko` (tilde), `@user-715295512: Kaytranada - AT ALL`. Pointers: `look on yt. in the comments. There is every track listed`, `trakclist :) https://boilerroom.tv/recording/solomun/#/video`.

**"Tracklist" mega-comments are the highest-value SoundCloud artefact.** Posted at 0:00–2:00 with inline times (ignore their own `timestamp`):
- `Tracklist:  00:17 Kuko - Fühlst du den Schmerz 03:07 Kuko - No Tears … 15:57 Kuko - Unreleased*` (single line, space-separated)
- `1:10 not alone\n1:30 kammy\n6:15 danielle…` (titles only)
- `1:10      Fred again.. & Swedish House Mafia - Not Alone (Intro Edit)\n… 18:11    Overmono vs. Lil Baby - BBY vs. Real As It Gets (Fred again.. Bootleg)` (shows `w/`, `vs.`, `x`, `(… Bootleg)`, `[… edit]`)
- `Tracklist :  00:00 ID - ID 6:30 ID - ID 11:00 Monolink - Father Ocean (Ben Böhmer Remix) … 33:00 ID - ID (probably : Pryda - SOL (Ben Böhmer unreleased Remix))`
- DJ Bigos style: `(00)Mohammad Reza Mortazavi-Riding Time (05)Kimyan Law-Kin … (36)? … (63)Autechre-Vletrmx?` — minute-only cues, `?` = unknown. Mirrors MixesDB wikitext `# [05] Kimyan Law - Kin`.

**Keywords:** `unreleased`, `Unreleased*`, `forthcoming on`, `dubplate`, `ID - ID` (fully unknown), `What a mashup`. 1001tracklists semantics: "ID" = unreleased or no Beatport link.

**Uploader replies:** zero uploader comments across all six sets. Boiler Room/Cercle descriptions instead link out: `Get the tracklist here: https://blrrm.tv/…`.

**Is the comment `timestamp` reliable?** Yes as "this track is playing now", **not** as "track start". Measured against crowd tracklists — seconds after listed track start:
- HÖR set, 62 questions: `<15s: 3, 15–30: 9, 30–60: 15, 60–120: 19, 120–180: 12, 180–300: 4` → **median 77s**
- Fred again, 116 questions: `<15: 12, <30: 12, <60: 31, <120: 26, <180: 4, <300: 9, ≥300: 22` → **median 64s**

**Rule:** a question at time T means the track started roughly T − (30…120s); a question almost never precedes the mix-in point. Cluster questions within ~90s windows; ≥3 questions in a window = an "interesting track" whose start is ~30–120s before the median question. This is a **boundary signal** even when nobody answers.

## 3. YouTube

**Descriptions.** Cercle: `SET TRACKLIST\n\n0:00 Ben Böhmer - Beyond Beliefs\n6:20 …`; yt-dlp derived 15 `chapters` from it. Boiler Room: no tracklist, links out. Chapter activation: first stamp `0:00`, ≥3 ascending, ≥10s apart. yt-dlp's parser: `duration_re = r'(?:\d+:)?\d{1,2}:\d{2}'`, tries timestamp-first then title-first, drops non-monotonic entries.

**Comments via yt-dlp:** fields `id, parent ('root' or parent id), text, like_count, author_id, author, author_is_uploader, author_is_verified, is_favorited (creator heart), is_pinned, timestamp, _time_text`. Extractor args: `comment_sort=top|new`, `max_comments=max-comments,max-parents,max-replies,max-replies-per-thread,max-depth`. Working command:
```
yt-dlp --skip-download --write-comments -j --extractor-args "youtube:max_comments=60,30,30,2;comment_sort=top" URL
```
(on Windows add `--js-runtimes node`).

**Key finding: pinned ≠ tracklist.** Fred again Boiler Room: pinned comment is the artist's thank-you (121k likes). Cercle's pinned uploader comment is vinyl promo. **The tracklist lives in a fan top-level comment with 12,000 likes**, with a reply correction (`The track at 21:49 is actually 'Skrillex with Bobby Raps - Leave Me Like This'`, 24 likes) that was right. Timestamps are plain text in `text` (`23:39`, `@23:40`, `1:11:44`). Noise: `from 0:00 to 1:11:44`, `he is touching it again at 48:20`.

## 4. Mixcloud, MixesDB, 1001tracklists

**Mixcloud REST** `api.mixcloud.com/{user}/{slug}/` — `sections` was `[]` on every cloudcast tested (licensing hides tracklists from free users). **Working alternative:** `POST https://app.mixcloud.com/graphql`:
```graphql
query { cloudcastLookup(lookup:{username:"spartacus", slug:"party-time"}) {
  name sections { __typename
    ... on TrackSection { startSeconds songName artistName }
    ... on ChapterSection { chapter startSeconds } } } }
```
Returned 9–12 sections live per show.

**MixesDB — best URL→tracklist lookup.**
`GET https://www.mixesdb.com/w/api.php?action=mixesdb_player_search&url=<player url>&format=json` → `{"mixesdb_player_search":[{"pageid":149382,"title":"2020-01-27 - Djrum - Dekmantel Podcast 267",…}]}` (verified with SoundCloud URLs). Wikitext via `action=query&pageids=…&prop=revisions&rvprop=content&rvslots=main`: `== Tracklist ==` then `# [05] Kimyan Law - Kin` (minute cues) or `# Artist - Title`. `{{Player|mode=mirrors |https://soundcloud.com/… |https://youtu.be/…}}` gives **cross-platform mirrors of the same mix** — useful for pulling YouTube comments on a SoundCloud set and vice versa. Categories `Tracklist: complete|incomplete`. Bonus: `action=mixesdbtrackid&url=…` returns trackid.net status for that URL.

**1001tracklists.** No URL search. Keyword endpoint: `GET https://www.1001tracklists.com/ajax/search_tracklist.php?p=<query>&noIDFieldCheck=true&fixedMode=true&sf=p` → `{"success":true,"data":[{"object":"tl","properties":{"tracklistname","id_tracklist","id_unique","url_name"}}]}`. Tracklist URL `https://1001.tl/<id_unique>`. Pages are Cloudflare-challenged (scrapers use Scrapling stealth). Structure: `div.tlpItem`, `.cueValueField`, `span.trackValue` "Artist - Title", schema.org `MusicRecording` meta; per-track media via `ajax/get_medialink.php?idObject=5&idItem=<id>` → `{source: 1 beatport | 10 soundcloud | 13 youtube | 36 spotify}`.

## 5. Prior-art parsers and their regexes

- **gieseladev/tracklist** (Go): `TimestampMatcher = \d+(?::\d+)+`; formats `^(TS)\s*(.+?)\s*$` (time-first), `^(\d+)\s*\p{Pd}\s*(.+?)\s*(TS)\s*$` (numbered, time-last), `^(TS)\s*\p{Pd}\s*(TS)\s*(.+?)\s*$` (range), `^(\d+)\s*\[(TS)]\s*(.+?)\s*$`.
- **XavierDuthil/youtube-tracklist-control:** `/(\d+:)?(\d?\d):(\d\d)/`; description first, then comments; accept first with ≥2 lines.
- **simple-youtube-chapter-extractor:** `(?:[^a-zA-Z0-9_=:])((?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}))(?:[^a-zA-Z0-9_=:])?` + title cleanup.
- **get-artist-title / youtube_title_parse:** separator priority `' -- ','--',' - ',' – ',' — ',' _ ','-','–','—',':','|','///',' / ','_','/','~'`; fluff removal `\s*\[[^\]]+]$`, `\(\s*(HD|HQ|[0-9]{3,4}p|4K)\s*\)$`, `\s*(of+icial\s*)?(music\s*)?video`, `\s*\(\s*[0-9]{4}\s*\)`.
- **conorbronsdon/track-finder:** separator requires whitespace on at least one side to protect `D-Nox`.
- **yt-dlp** `_extract_chapters_from_description` + `_extract_chapters_helper` monotonic/duration validation.

## Recommendations

### (a) Hint data model
```
Hint {
  id, source: sc_comment|sc_description|yt_comment|yt_description|yt_chapters|mixcloud_sections|mixesdb|1001tl
  kind: question|answer|tracklist_line|correction|pointer|keyword
  position_ms: int|null, position_source: comment_timestamp|inline_text|chapter|cue|none
  position_window_ms: [lo, hi]        # comment ts → [ts−120000, ts+15000]
  raw_text, artist|null, title|null, remix|null, label|null
  flags {unreleased, id_unknown, mashup_with, edit, bootleg, question}
  author {id, handle, is_uploader, is_verified, follower_count}
  reply_to_hint_id|null, like_count|null, is_pinned, created_at
  confidence: float, evidence: [why]
}
```

### (b) Parsing strategy
1. Normalise (NFKC, collapse `\r\n`); split mega-comments on `\s+(?=(\d{1,2}:)?\d{1,2}:\d{2}\s)` and on Bigos-style `\((\d{1,3})\)` cues.
2. Classify each unit:
   - **Tracklist line (time-first):** `^\s*[\[\(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\]\)]?\s*[-–—:.)]?\s*(.+?)\s*$`; minute-only `^\s*[\[\(](\d{1,3})[\]\)]\s*(.+)$`; time-last variant. Accept a block only if ≥2 lines, non-decreasing, ≤ duration. Strip `^(Tracklist|Track ?list|TL)( so far)?\s*:?\s*`.
   - **Question:** `\b(track\s*)?id\b[\s?!.]*$|\?\s*$` with `\b(id|track|tune|song|name|this one|anyone|what'?s)\b`; inline time `(?:at|around|@)?\s*((?:\d{1,2}:)?\d{1,2}[:.]\d{2})` overrides `position_ms`.
   - **Answer/correction:** `^(?:@[\w.-]+:?\s*)?(?:(?:this|it|that|track|song|tune)\s*(?:is|=|:)|id\s*[:=-]|the track at\s*TS\s*is(?: actually)?)\s*(.+)`; inverted `(.+?) by (.+)`.
   - **Artist/title split** on first separator with whitespace on ≥1 side, priority `' - ',' – ',' — ',' ~ ',' : ',' | ','-'`; extract `\((.*?(remix|edit|rework|bootleg|mix|vip|dub|version).*?)\)`, `\[(.*?)\]` → label/edit; `\b(w/|vs\.?|x)\b` → mashup; `\b(ID(?:\s*-\s*ID)?|\?|unreleased\*?|forthcoming|dubplate)\b` → flags. Reject "titles" that are reactions (`^(this is|so good|fire)`).
   - **Pointer:** URLs to boilerroom.tv, 1001tracklists, mixesdb, youtube.
3. Resolve SC `@permalink` to nearest earlier comment by that `user.permalink`; else standalone.

### (c) Weighting — starting-point features, not probabilities
*Review round 1 note: these numbers are unvalidated hand priors. The plan treats source type, parse quality, author authority, identity specificity, temporal precision and independence as separate features, collapses copied text across sources into one vote, and uses question clusters only to queue rescans. Calibrate on the dev split before trusting any of them.*
- 1001tracklists / MixesDB cue lines **1.0**; Mixcloud GraphQL sections **0.95**; YouTube chapters / description tracklist **0.9**
- Uploader-authored text **0.9**; pinned **0.8** only if it parses as a tracklist
- Comment tracklist block: **0.6 + 0.2·log10(likes+1)/5** on YouTube; on SoundCloud use follower_count/verified and block length (≥5 valid lines → 0.7)
- Reply/correction to a tracklist line **0.6**; `@`-reply answer **0.5**; standalone `Artist - Title` comment **0.35**
- Question alone: **0** for identity, **0.3 as boundary signal** — ≥3 questions within 90s marks an interesting track starting ~30–120s before the median
- Position: inline `at 22:48` beats the comment's own timestamp; comment timestamp → window `[ts−120s, ts+15s]`; tracklist blocks posted before 2:00 contribute inline times only
- `unreleased` / `ID - ID` lines: penalise for identity, keep as segment boundaries

### (d) Fetch order
**SoundCloud URL:** (1) `api-v2 /resolve?url=` → track (duration, `user_id`, description; look for tracklist lines + pointers); (2) **MixesDB `mixesdb_player_search&url=`** → wikitext tracklist + `{{Player}}` mirrors; (3) `api-v2 /tracks/{id}/comments?threaded=0&limit=200` all pages; (4) if YouTube mirror exists, yt-dlp comments/chapters on it; (5) 1001tracklists title search; (6) Mixcloud GraphQL if mirror exists.

**YouTube URL:** (1) yt-dlp `-j` description + `chapters` + top comments; (2) follow "tracklist" links in description (blrrm.tv, 1001.tl); (3) MixesDB `mixesdb_player_search` (try `youtu.be/<id>` and full form); (4) 1001tracklists title search; (5) if MixesDB lists a SoundCloud mirror, pull its timed comments for boundary signals.

## Fixtures
Raw corpora are kept locally in `data/raw/comments/` (git-ignored; see its README for the per-file index) and minimised into committed fixtures by `scripts/derive_fixtures.py` — see [data/fixtures/README.md](../../data/fixtures/README.md). Six SoundCloud comment dumps (~7,000 comments, compact `{ts, body, u, pl, c}` per line), one raw api-v2 comments page and one raw track-search response (full field shapes), yt-dlp dumps for two YouTube sets (description, chapters, top comments incl. the 12k-like fan tracklist), and a Mixcloud REST cloudcast showing empty `sections`. Ground-truth tracklists for these sets still need assembling in Stage 2.
