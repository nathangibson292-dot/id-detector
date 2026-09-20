# release-1 — where the missed tracks go (Free recipe, DRAFT truth)

*Analysed 2026-09-20 at commit `0915f85`. Read-only: no code, truth file, cached mix or commit was
touched. Draft truth, so these are working numbers, not a release claim.*

## 1. Method, and what "144 of 218" really is

- Every number here comes from **cached Shazam recognition data**. The small cached inputs of each
  mix (observations, windows, hints, the cached hint-connector results, `pcm.json`, `source.json`)
  were COPIED to a scratch directory outside the repo; hints were re-parsed and fusion re-run in
  memory from the copies with the current code, then scored with `scripts/score_corpus.py`.
  Recognition was never triggered, no provider was called and nothing was fetched
  (`IDEA_TEST_MODE=1`, `AUDD_API_TOKEN=` empty, `IDEA_ENGINE_SHAZAM=off`).
- The commit's headline reproduces **exactly**: 615 found, 125 buried / 28 contradicted / 30
  scatter / 222 short hidden, 210 listed, recall **144/218**, work precision 145/195, likely 62/68.
- One thing the owner should know: that 144 is only reached when `release1-boomtown-mix` is fused
  with its cached **rescan** generations (invocation `09aad8cd…`, gen 0-2). With the inputs the
  shipped default uses (rescans off, the run the cached `episodes.json` names) the same code gives
  **143/218**: "Effy - Pitched" is then hidden too (section 4, burial). All tables below use the
  144 baseline; the one-track difference is called out where it matters.
- **Other engines:** no cached AudD, ACRCloud or Panako data exists for any of the seven mixes
  (the only such caches in `work/` belong to two other mixes). "Never heard" below therefore means
  "Shazam never heard it"; what a paid engine would add is **unmeasured** here.
- Each missed track was checked against (a) every raw window label in the whole mix, (b) every
  fused episode including hidden ones, and (c) everything heard in its time slot (its own
  timestamp for timed mixes; between its found neighbours for order-only mixes).

Causes: **A** never heard · **B** heard but lost in fusion · **C** heard and fused but hidden by
a filter · **D** listed, but the scorer did not match it to the draft truth (not a tool error) ·
**E** anything else.

## 2. Summary

| Cause | Tracks | Share of the 74 |
|---|---|---|
| A. Never heard by Shazam | **45** | 61 % |
| B. Heard but lost in fusion | **0** | 0 % |
| C. Heard and fused but hidden | **11** (short floor 7, buried 3, contradicted 1) | 15 % |
| D. Listed, scorer/truth label mismatch | **16** | 22 % |
| E. Other (engine named a related but different work) | **2** | 3 % |
| **Missed** | **74** | |

| Mix | Truth | Found | Missed | A | B | C | D | E |
|---|---|---|---|---|---|---|---|---|
| `release1-boomtown-mix` (`d3252340303c`) | 40 | 36 | 4 | 1 | 0 | 3 | 0 | 0 |
| `release1-redo-of-best-set` (`99b8ceebb852`) | 18 | 16 | 2 | 0 | 0 | 1 | 1 | 0 |
| `release1-new-mix-jan-24th` (`a53c5eed4b59`) | 30 | 25 | 5 | 2 | 0 | 1 | 2 | 0 |
| `release1-final-new-set-christmas` (`6c00a79d755e`) | 23 | 19 | 4 | 2 | 0 | 0 | 2 | 0 |
| `release1-mall-grab-boiler-room-melbourne-22` (`6651118675bd`) | 24 | 9 | 15 | 8 | 0 | 0 | 7 | 0 |
| `release1-mph-youtube-set` (`ed4ca55359f9`) | 62 | 29 | 33 | 23 | 0 | 6 | 2 | 2 |
| `release1-dj-heartstring-youtube-set` (`84232b822c6a`) | 21 | 10 | 11 | 9 | 0 | 0 | 2 | 0 |
| **All seven** | **218** | **144** | **74** | **45** | **0** | **11** | **16** | **2** |

The owner's own four mixes taken alone: 111 tracks, 96 found, 15 missed — A 5, C 5, D 5.

**Why B is zero.** Shazam returns one label per 12-second window and fusion builds one episode
per label, so there is no in-window competition to lose. Every missed track that any raw window
named does have an episode; it was then hidden (C) or listed under a label the scorer could not
pair (D). No case of a track's windows being split into, or merged into, another track was found.

## 3. The ceiling of the free engine

- Shazam **never heard 50 of the 218 tracks (23 %) at all**: the 45 cause-A misses, plus 5 that
  listeners' comments rescued without any audio match (two counted as found: "DAVE ERA - I Like
  The Way You Talk", "Prozak - Bump N Grind"; three under cause D: "4raws", "I'm ready", "Long
  Season").
- So Shazam heard **168/218 (77 %)** in some form. That, plus the 5 comment rescues, is the most
  the free recipe could show on these mixes: **about 173/218 (79 %)** if every filter and every
  label were perfect. Today it shows 144 (66 %).
- 25 of the 45 never-heard tracks carry an unreleased marker in the truth itself ("UR", "ID",
  an edit, bootleg, refix, dub, "Soundcloud"); no commercial engine is likely to know those. The
  other 20 look like released tracks, which is the most a paid engine could plausibly add
  (unmeasured; two of the three outside sets are live recordings where a third or more of all
  windows return nothing at all: 205/602 and 269/605).
- 7 truth keys are placeholders ("Unknown - ?", "… - ID"). Five of them sit in cause A and can
  never count as found under any engine; the tool did name the other two (see D).
- Caveat on A: in three slots Shazam confidently heard *something else* for minutes where a
  never-heard truth track should be ("Eatmyflesh - Gorejust Summer Mix" 14 windows where "Bushbaby
  - DESIRE96" sits; "Heparyna - WISH YOU WERE MINE REMIX" 10 windows where "Faster Horses - Get On
  Ya Knees" sits; "Brandon Allan - HevE Tac" 27 windows across the "1ofthozedaze"/"Metaphysical"
  slot). These may be the same audio under another Shazam label. Only the owner's ears can say.
- 93 of the 3,348 first-pass windows (2.8 %) came back as provider **errors** and were never
  retried. They are spread thinly (1-4 per missed track's slot), so they explain no miss on their
  own, but a retry is free.

### Cause A, by mix (never named by any window in the whole mix)

- `boomtown-mix` (1): NOTION, Cameron Hayes - SECRETS.
- `new-mix-jan-24th` (2): Soul Mass Transit System - Feelin U (the slot is heard as "Rank 1 -
  Airwave", 7 windows); Faster Horses - Get On Ya Knees (see caveat above).
- `final-new-set-christmas` (2): Ollie Lishman - THA WAY (17 silent windows in its slot);
  Bushbaby - DESIRE96 (see caveat).
- `mall-grab-…` (8): Mirror Break (4/4 Mix), Inside, Bear Witness, Escape From Belanglo, BB MG,
  Sambaboys - ID, Madman, 1ofthozedaze (Edit).
- `mph-youtube-set` (23): Run Da Riddim; Smoothies; Gold Coast; Higher Ground (Higgo Edit); Zum
  Zum; Tokyo Dub; SMTS - Bomb; Bring In The Katz Dub; Calentura Vaginal (Hard Drum Edit); Kill
  Bill; Light It Up; Superstylin' (DJ Q Bootleg); Badderman Refix; Circles (Oppidan Bootleg);
  Sammy Virji - ID; Spend The Night; Headtop (Original Dub Mix); Swoon (MPH Edit); Add Suv;
  33 Below & MPH & NOTION - ID; Dutty; Nova; Home T Dub. (The second "MPH - ID" row at 1:06:15 was
  also never heard; it shares its key with the first one, which is under D.)
- `dj-heartstring-…` (9): Unknown - ?; DJ Heartstring - ID (three rows, one key); If U Want My
  Heart; Shugz - The Drums; Alive (DJ Heartstring Remix); DJ Seinfeld - Plush; Staring into the
  Sun; Wake Up The Funk (Ragel Mood Remix); A Thousand Lies.

## 4. Cause C — heard, fused, then hidden (the ones code can fix)

Times are the raw matching windows (each 12 s, 9 s apart). "On air" is the summed matched time.

### Buried (3; 4 with rescans off) — a famous-track phantom's *hull* covers them

Fusion hides an episode as `buried` when 60 % of its span lies under "confident" episodes, and it
measures a confident episode by its **hull**: first matched window to last, gaps included. A
`likely` badge also makes an episode immune to the `scatter` filter. Together: a phantom whose few
windows are smeared over many minutes gets `likely`, is listed, and buries real tracks under it.

| Mix | Track | Its evidence | What buried it |
|---|---|---|---|
| boomtown | Cotto - Murda Sound | 5 consecutive windows 0:18:09-0:18:45, 48 s, `possible` | "Darude - Sandstorm", `likely`, 12 windows, hull 0:15:30-0:19:48 |
| boomtown (rescans off only) | Effy - Pitched | 6 consecutive windows 0:17:15-0:18:00, 57 s, `possible` | the same Sandstorm hull |
| mph | MPH - Overrated (VIP) | 7 consecutive windows 1:10:12-1:11:06, 66 s, `possible` | "Darude - Sandstorm", `likely`, 5 windows = 57 s on air smeared over a hull of 1:02:09-1:12:27 |
| mph | MJ Cole & MPH - Hold On | 7 windows 1:11:33-1:13:03, 72 s, `possible` | the same hull (covers 60 % of it) |

The same mechanism is why the Mall Grab set hides 51 episodes as buried: "System F - Out of the
Blue" (`likely`, 138 s on air, hull 0:17:54-**1:22:48**), "Darude - Sandstorm" (`likely`, hull
0:19:51-1:22:39) and "Rank 1 - Airwave" (`likely`, hull 0:15:27-0:35:51) blanket the mix. There,
comment backing happened to rescue every real track, so no truth track is lost, but four phantom
rows are listed as `likely`.

### Contradicted (1) — the next track's comment lands on the blend

| Mix | Track | Its evidence | The filter |
|---|---|---|---|
| new-mix-jan-24th | Bushbaby - Pumpin Jumpin | 13 windows 0:08:42-0:10:51, 123 s, `possible` | a listener's answer naming the NEXT track ("tella soundboi - becking") sits at 0:10:49, two seconds before this episode's last window ends. The tool lists that next track from 0:11:00, so the comment was right and so was the audio; the rule reads any answer inside the hull as a contradiction. |

### Short floor (7) — under 30 s on air, no `likely` badge, no comment backing

| Mix | Track | Its evidence | Note |
|---|---|---|---|
| boomtown | Cesco - Move Too Slow | 2 windows 1:09:45 and 1:10:12, 24 s, `possible` | every other window 1:08:15-1:10:48 returns a different one-off label: Shazam barely knows it |
| boomtown | Chase & Status - 5am | 2 windows 1:13:48 and 1:14:06, 24 s, `possible` | followed by 6 straight windows of "2 Bad Mice - Bombscare" (listed; probably the sample inside this track) |
| redo-of-best-set | DJ SCHEMA - Self Control | 1 window 0:13:21, 12 s, `unclear` | its slot is then heard as "Ayla (DJ Taucher Remix)" 8 windows + "Jupiter 8000 - Inside" 3 (both listed, wrong) |
| mph | MPH - Raw | 2 windows 0:22:39, 0:22:48, 21 s, `unclear` | truth 0:21:40 |
| mph | MPH - Doubt | 2 windows 0:49:48, 0:49:57, 21 s, `unclear` | truth 0:49:34 |
| mph | MPH - Brainwashing | 1 window 0:32:15, 12 s, `unclear` | truth 0:31:41 |
| mph | The Bug - Jah War (feat. Flowdan) | 1 window 1:19:21, 12 s, `unclear` | truth 1:18:13, an overlay |

No missed track was hidden by `scatter` or by a tier cut-off.

## 5. Cause E (2)

- mph, "Oppidan - Move Your Feet (VIP)": one window at 0:40:03 names **"Junior Senior - Move Your
  Feet"**, the original it bootlegs; hidden as short. Even listed, the scorer would not pair it.
- mph, "Simula - Daddy Issues" (truth 0:47:58): the tool lists **"Simula - Descent"**, 3 windows
  0:49:03-0:49:21. Same artist, another title: either Shazam picked a sibling track or the public
  tracklist has the title wrong. Owner to check.

## 6. Cause D — truth rows for the owner to re-check (16)

These are on the tool's tracklist. The scorer did not pair them with the draft truth. Nothing in
the truth files was edited.

| Mix | Draft truth says | The tool lists | Why they did not pair |
|---|---|---|---|
| redo-of-best-set | Sammy Virji & Flowdan - Shella Verse | Flowdan - Shell a Verse (0:26:18, 168 s, 19 windows, comment-backed) | spelling: "Shella" / "Shell a" |
| new-mix-jan-24th | Beau James - 4 Raws Edit | 4raws - beau james (0:00:40, from a comment; no audio match) | "4raws" / "4 Raws" |
| new-mix-jan-24th | Beau James - 18Hunna | Headie One - 18HUNNA (feat. Dave) (0:48:12, 105 s, 11 windows) | version: the tool names the original the edit is built on |
| final-new-set-christmas | Upper90 - I Am Ready 2 | I'm ready - Upper90 (0:09:54, from a comment) | title: "I Am Ready 2" / "I'm ready". The audio there is heard as "NARÖ - TechTrance" (16 windows, hidden as contradicted) |
| final-new-set-christmas | Testpress - FORZ4 | t e s t p r e s s - Forz4 (0:13:51, 33 s) | the artist is spelled with spaced letters on Shazam |
| mall-grab | Fishmans - Long Season (MG Edit) (UR) | Fishmans - Long season MG Edit (0:02:32) and two more comment rows | "(MG Edit)" is dropped from the truth title as a mix descriptor but kept in the comment's |
| mall-grab | Mall Grab - Winter (UR) | Jordon Alexander - Winter (feat. Mall Grab) (0:04:21, 135 s, 19 windows) | the release credits his own name first |
| mall-grab | Mall Grab - Marathon (UR) | Jordon Alexander - Marathon (feat. Mall Grab) (0:23:00, 78 s) | same |
| mall-grab | Mall Grab & Flansie - Love Yourself | Flansie & Mall Grab - No One Else Will (0:25:24, 162 s, 19 windows, comment-backed) | same artists, same minute, different title: the public tracklist's title looks wrong |
| mall-grab | Surusinghe - ID (UR) | Surusinghe - Likshot (0:37:42, 24 s, comment-backed) | the truth row is a placeholder; the track has since been named |
| mall-grab | KETTAMA - Feel Emotion (UR) | KETTAMA - Feeling Emotions (Extended mix) (0:51:21, 222 s, 25 windows) | title wording |
| mall-grab | C.R.T.B. - Nothing Moves You | CRTB - It Never Ends (1:08:27, 135 s, 15 windows, comment-backed; MixesDB agrees) | different title at the same time: the draft truth looks wrong |
| mph | Boy Better Know - Too Many Man (NOTION Edit) | Mark Krupp - Too Many Man (0:02:33, 75 s, 7 windows) | right song, right time; Shazam's copy is another artist's version |
| mph | MPH - ID (0:26:51) | MPH - La Nyc (0:27:03, 123 s, 13 windows) | placeholder in the truth; the track is released as "LA NYC" |
| dj-heartstring | DJ Seinfeld - Rythm of the Night (x Biicla - SWAG) | Corona - Rhythm of the Night (Lee Marrow Space Mix) (0:06:03, 39 s) | a mash-up; the tool names the original. The truth also spells "Rythm" |
| dj-heartstring | DJ Heartstring - Another Year Alone (UR) | DJ HEARTSTRING - Another Year Alone (i love you) (1:03:12, 261 s, 29 windows) | the released title has a subtitle |

Also worth a look while verifying: the five remaining placeholder rows (section 3); the MPH row
"Simula - Daddy Issues" (section 5); and the three "something else was heard for minutes" slots in
the section 3 caveat. Tracklist clock offsets (tool start minus tracklist start, median) are now
+84 s on the Mall Grab set, +51 s on the DJ Heartstring set and +17 s on the MPH set; they affect
the by-time numbers only, never the 144.

Two or three of these are arguably the scorer's to fix rather than the truth's ("4raws" against
"4 Raws", the spaced-letter artist, a kept "(MG Edit)"): fusion's label folding already handles
joined words for hints, the scorer's matcher does not.

## 7. Wrong listed rows (57 rows, 50 distinct labels, of 210 listed rows)

| Kind | Labels | Which |
|---|---|---|
| A real track the draft truth omits or spells differently | **18** | the 16 cause-D labels of section 6, plus two more: "CHRYSTAL & NOTION - The Days (NOTION Remix)" on redo-of-best-set (0:04:24, **237 s, 26 windows** — certainly played; either missing from the playlist or the same track as the truth's "DAVE ERA - I Like The Way You Talk", which sits right before it and was found only through a comment); "Armand Van Helden - You Don't Know Me" on mph (6 straight windows in the recording's last minute, after the tracklist ends) |
| A duplicate of a listed label | **2** labels (+7 extra rows) | two more comment spellings of "Long Season" (mall-grab). Row-level repeats of an already-wrong label: "Mears - Be My Lover" ×2 (redo), "Rank 1 - Airwave" ×3 and "Torres De Lara - De Barrio" ×5 (dj-heartstring) |
| A short false fragment (3 windows, 30-36 s, just over the floor) | **8** | redo: Mass Medium - Gotta Have It; Jupiter 8000 - Inside; Rollercoaster NL - Come With Me (heard inside "Rok da House" on two different mixes: a sample). jan: Architechs - Body Groove. mph: Me & My Toothbrush - Everybody; Energy Reflect - Ascend; Simula - Descent (section 5). dj-heartstring: DJ Hazel & svdst - Dropsik |
| A genuinely wrong identification | **22** | see below |

The 22 genuinely wrong labels:

- **Recurring phantoms (11).** The same few famous tracks are "heard" across unrelated mixes by
  four different DJs and appear in no truth: Darude - Sandstorm (raw windows in **7 of 7** mixes),
  Rank 1 - Airwave (6), Tiësto - Adagio for Strings (6), Alice Deejay - Better Off Alone (5),
  Zombie Nation - Kernkraft 400 (4), Torres De Lara - De Barrio (4). Listed instances: boomtown
  Sandstorm (`likely`) and Kernkraft 400; redo De Barrio; jan Adagio; mall-grab Airwave, Sandstorm,
  Out of the Blue and Out of the Blue 2010 (all four `likely`); mph Sandstorm (`likely`);
  dj-heartstring Airwave and De Barrio. These are **all 6 of the wrong `likely` rows** and the
  source of every burial in section 4.
- **Heard in a truth track's slot, probably its sample or source (6).** boomtown: Fragma - Toca Me
  (over "Henney & Coke"), 2 Bad Mice - Bombscare (right after "5am"), NCKNAME - Tryna Dance
  (between the two halves of "Love Sensation"). redo: Ayla (DJ Taucher Remix) (the "SELF CONTROL"
  slot), Mears - Be My Lover (around "Original Don"), Effy & Mall Grab - iluv (9 windows around
  "Effy - RUNNING"; could be a real blend — owner to check).
- **Confident, long, in a never-heard track's slot (3).** Eatmyflesh - Gorejust Summer Mix (xmas,
  129 s); Heparyna - WISH YOU WERE MINE REMIX (jan, 93 s); Brandon Allan - HevE Tac (mall-grab,
  258 s). See the section 3 caveat: possibly the right audio under another label.
- **Other (2).** mall-grab: a chat comment read as a track (a comment-only row at 0:02:16). mph:
  Riky López - Canary Sound (48 s, after the tracklist ends).

## 8. What to do next, ranked by tracks recovered

Each code change was simulated on scratch copies of the fused episodes and re-scored with
`scripts/score_corpus.py`; the numbers are measured, not guessed. None needs the paid engine.

| # | Change | Tracks recovered | Risk: does it let wrong tracks in? | Paid engine? |
|---|---|---|---|---|
| 1 | **Owner re-checks the 16 cause-D truth rows** (section 6) while verifying. Not a code change. | **+16** (144 → 160, 73.4 %) and work precision 145/195 → about 161/195 | None: these rows are already listed; only the score changes. Four of them are title disputes the owner must settle by ear. | No |
| 2 | **Measure burial by matched runs, not by the hull, and stop a `likely` badge from overriding `scatter`.** (Simulated: a confident episode covers only its runs of windows less than 60 s apart; a `likely` episode with no comment or second-engine backing is hidden when its hull is ≥ 120 s and ≥ 3× its on-air time.) | **+3** (Cotto - Murda Sound, MPH - Overrated, Hold On); **+4 with the shipped default** (rescans off; measured 143 → 147), which also loses Effy - Pitched today | It *removes* 5 wrong labels, all `likely` (Sandstorm ×2, Airwave, Out of the Blue ×2): likely precision 62/68 → **61/62**, work precision 145/195 → 148/194. One wrong row returns (an "Adagio for Strings" fragment on redo-of-best-set that the hull used to bury). One correct `likely` row ("Skin On Skin - Burn Dem Bridges") trips the scatter test and needs a guard, e.g. exempt an episode whose longest run alone is ≥ 45 s. | No |
| 3 | **Let a 2-window short row through when its artist already has a solid listed row in the same mix** (≥ 45 s on air). DJs play several tracks by the same artist; phantoms rarely share one. | **+3** (Chase & Status - 5am, MPH - Raw, MPH - Doubt) | Measured: no new wrong label. It does add 2 repeat rows of an already-wrong phantom ("De Barrio"), which change 2 or a same-work de-dupe removes. Extending it to 1-window rows gains 1 more (Brainwashing) but lets in 1 wrong label and 9 repeat rows: not recommended. A flat 20 s floor instead gains +4 but adds **23** wrong labels: rejected. | No |
| 4 | **A comment at the very edge of a long episode is about the next track, not a contradiction.** (Simulated: hull ≥ 60 s and every contradicting answer within 15 s of either end.) | **+1** (Bushbaby - Pumpin Jumpin, 13 windows) | Measured: nothing else changes; the three long episodes contradicted from the *inside* stay hidden. Dropping the rule for all long episodes instead gains the same +1 and lets in 3 wrong labels: rejected. | No |
| 5 | **The 45 never-heard tracks need a different source, not a fusion change.** For the owner's own mixes the cheap one is already built: index his own rekordbox library with the local index (`--local-index`), since he owns every file he played. | Up to **+5 never-heard and +3 barely-heard** on his four mixes (SECRETS, Feelin U, Get On Ya Knees, THA WAY, DESIRE96; Cesco, SELF CONTROL, plus 5am if change 3 is not made) — **unmeasured**, no index data is cached for these mixes. For the three outside sets it cannot help (he does not own those unreleased tracks). | Low: a local-index hit is an exact recording match. | No. The paid engine is a separate, unmeasured option: at most the ~20 released-looking tracks of the 45, and earlier tests found it shares Shazam's underground blind spots. |

All four code-free-engine changes together (2 + 3 + 4, simulated in one pass): recall **151/218
(69.3 %)**, work precision 152/198 (76.8 %), likely 61/62 (98.4 %), 225 rows listed. With the
truth re-check on top: about **167/218 (76.6 %)**, against a free-engine ceiling of about 173.
On the owner's own four mixes: 96/111 today → 104/111 (93.7 %) after 1-4, leaving the five
tracks Shazam does not know and two it hears for one or two windows.

Also cheap, unmeasured: retry the 2.8 % of windows that returned a provider error; and a
"recurring phantom" check (a label heard in many unrelated mixes of the library and never backed
by a comment is demoted) would remove the remaining 6 phantom labels that change 2 leaves listed.

## 9. `scripts/audit_fixtures.py`

```text
$ uv run python scripts/audit_fixtures.py
audited 531 files
fixture audit passed
```
