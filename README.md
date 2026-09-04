# id-detector

**Find out what tracks are in a DJ set.** You give it a link to a mix (SoundCloud, YouTube, or
Mixcloud); it downloads the audio, listens to it in short overlapping windows, asks music-recognition
engines "what is this?", reads any tracklist hints people left in the comments, and builds an honest
**timeline of track episodes** — each with its own confidence and a *click-to-jump* web page. It
tells you where a track *starts* only as far as the evidence proves, marks stretches it could not
identify as "no evidence" (never "no track"), and shows you where you can get each track.

The guiding rule is **accuracy over cost or speed**, and **honesty over completeness**: it will say
"unclear" rather than guess.

---

## Quick start (for the owner) — the browser way

You need two things installed first: **[uv](https://docs.astral.sh/uv/)** (the Python runner) and
**ffmpeg** (audio decoding). Everything else `uv` installs for you. Run `uv sync` once to install.

Then the whole tool is a **browser page** — no commands after the first setup:

1. **Double-click `id-detector.cmd`** in this folder. A terminal opens the local server and your
   **browser opens automatically** at `http://127.0.0.1:8765`.
2. **Paste a mix URL** (SoundCloud / YouTube / Mixcloud) into the box, pick a profile, and click
   **Analyse**.
3. Watch the **live progress** (which phase, how many windows done, an estimated time). When it
   finishes you get the **result page**: a player with a timeline where **clicking any track row
   jumps the player to that moment**, plus "where to get it" links.

Everything runs **only on your machine** (`127.0.0.1`); nothing is exposed to the network. The page
also lists your recent and finished analyses, so you can leave it open and start more. Tick **also
fetch acquire links** to add buy/download links, or **build reference index first** to fingerprint an
uploader's own (possibly unreleased) tracks before analysing.

### The command line (optional)

The same steps are available as commands if you prefer them:

```powershell
uv run id-detector doctor                # check your machine is ready (ffmpeg, Python, etc.)
uv run id-detector serve                 # start the browser app yourself (what id-detector.cmd runs)
uv run id-detector analyse "<mix-url>"   # analyse a set from the terminal instead
uv run id-detector acquire "<mix-url>"   # add "where to buy / download" links to a result
uv run id-detector config show           # see every setting and its current value
```

`serve` opens the browser by default; add `--no-open` to skip that or `--no-analyse` for a
read-only viewer of already-analysed sets.

### What each run produces

Under `work/<...>/present/` you get, for every set:

| File | What it is |
|---|---|
| `index.html` | the interactive page `serve` shows (player + clickable timeline) |
| `tracklist.md` | a human-readable table (time, badge, track, where-to-get) |
| `tracklist.cue` | a **CUE sheet** for CD/DJ software; overlapping tracks are noted on `REM` lines |
| `tracklist.m3u` | a **playlist** — open it in VLC and each entry jumps to that track's moment |
| `tracklist.json` | the same data for other tools |

---

## Reading the results: badges, version status, and "provisional"

Every identified track shows a **badge** and a **version status**. They answer two *different*
questions, so read them together.

**Badge — "is this the right track (the work)?"**

| Badge | Meaning |
|---|---|
| `LIKELY` | strong, agreeing evidence across several independent windows |
| `POSSIBLE` | some agreeing evidence, but less of it |
| `UNCLEAR` | a little evidence, not enough to stand behind |

**Version status — "is it exactly this recording (original vs. remix vs. edit)?"**

| Version status | Meaning |
|---|---|
| `VERIFIED` | the exact recording is corroborated by recording-specific IDs from ≥ 2 independent sources |
| `UNVERIFIED` | the track is probably right, but *which version* is not proven |
| `CONTESTED` | sources disagree about the recording |

In the **free (Shazam-only) profile**, a single engine can identify the *work* well but can almost
never *verify the exact version*, so you will normally see e.g. `LIKELY / UNVERIFIED`. That is
expected and honest — not a bug. The badge is never a full `VERIFIED` unless the version is verified
too.

**What "provisional" means.** The badges are computed by **sensible rules, not by a certified
benchmark.** To *certify* a tier ("when we say LIKELY, we are right ≥ 95% of the time") we would need
a funded, human-verified test corpus (see the owner questions below). None exists yet, so every
real-mix tier is shown as **provisional**. Treat the badges as well-reasoned guidance, not a
guarantee.

**"No evidence" is not "no track."** Grey gaps on the timeline mean the engines heard something they
could not identify — never that nothing was playing.

---

## What is *excluded* from v1, and why

- **Reference-pool recognition (Panako).** Panako can match a mix against your own library of
  reference files, and reportedly does best on electronic music with pitch/tempo changes. It needs a
  **Java runtime (JDK ≥ 11)**, which is not installed here. Until you decide to add a JDK, Panako is
  **excluded** and `doctor` reports it as disabled. Nothing else depends on it.
- **Certified accuracy tiers.** Without a funded, second-pass-annotated test corpus the tiers stay
  **provisional** (see above). The one small development set (`dev-1`) is enough to *build* and
  *sanity-check* the tool, not to *certify* it.

Neither exclusion stops the tool from working; they only limit what it is allowed to *claim*.

---

## Adding paid engines (optional)

By default the tool uses **only Shazam** (free). Two paid engines can be added for harder sets:

- **AudD** — long-file native, good offsets. Roughly **$1.50 per hour** of audio (plan-dependent),
  with 300 free requests to start.
- **ACRCloud** — reportedly strong on electronic music. Gated pricing (**~$1.40 per hour**
  anecdotally); needs a Windows VC++ runtime.

To enable one, you must do **both** of these (this is a deliberate safety gate, because these engines
upload your audio to a third party):

1. Put the credentials in **environment variables** (never in a file) — see `.env.example` for the
   exact names.
2. In your `id-detector.toml`, set `allow_third_party_upload = true`, **and** pass
   `--i-own-this-audio-or-have-permission` on the command that uploads.

If either is missing, uploads are refused with a message telling you exactly what to add.

### Cost expectations

- **Shazam:** free. The tool self-limits to ≤ 18 requests/minute and caches every result, so re-runs
  cost nothing. A per-run ceiling (`max_requests`, default 2000) caps the work.
- **AudD / ACRCloud:** you pay per hour of audio as above. Budgets are enforced up front and every
  network attempt is counted against the ceiling, so a set cannot silently overspend.

---

## Configuration

All non-secret settings live in one file, **`id-detector.toml`** (git-ignored). Create and inspect it
with:

```powershell
uv run id-detector config init     # writes a fully-commented id-detector.toml
uv run id-detector config show     # prints the effective settings (file + defaults), no secrets
```

It covers: the default **profile**, the request **budget**, **transform** hypotheses, the window
**schedule** and **rescan** policy, recognition **cache** lifetimes, the seek **lead-in**, the
per-connector **hint** switches, and the `allow_third_party_upload` gate. Each key is documented in
the file itself.

**Precedence (highest wins):** command-line flags → a chosen `--profile` (fixes engines and
window geometry) → your `id-detector.toml` → built-in defaults.

**Secrets never go in the config file.** Provider credentials are read only from the environment
variables in `.env.example`, and the logger redacts them. `config show` never prints a secret.

---

## Verifying accuracy yourself, and freezing a corpus

If you want to move tiers from *provisional* to *certified*, you build a **truth corpus** — sets with
human-checked tracklists — and freeze it. The `truth` commands walk you through it:

```powershell
uv run id-detector truth seed --help          # start a truth file for a set
uv run id-detector truth verify --help         # first-pass verification
uv run id-detector truth second-pass --help    # independent blind second pass
uv run id-detector truth resolve --help         # resolve annotator disagreements
uv run id-detector truth freeze --help          # freeze a corpus version (immutable manifest)
```

Once a real-mix corpus is frozen you can run a single, pre-registered certification pass
(`benchmark certify`), which is the only thing allowed to change a tier from *provisional* to
*certified*. The controlled (synthetic) benchmark can be run today and is used to tune the engine
grid and rescan policy without any real-world audio.

See **[docs/STATUS.md](docs/STATUS.md)** for the honest, per-stage status of everything, and
**[docs/PLAN.md](docs/PLAN.md)** for the full specification.

---

## Legal and terms-of-service notes

This tool is built for **personal use**. Please keep it that way:

- It uses an **unofficial** Shazam path and reads SoundCloud/YouTube/Mixcloud metadata and comments.
  These are fine for personal research but are **not cleared for a commercial service** — a separate
  review is required first (see the *Commercial release checklist* in `docs/PLAN.md`).
- **"Where to get it" links never automate a purchase or a download gate.** They only take you to the
  page (Bandcamp, Beatport, a SoundCloud download button, etc.). You buy or download yourself.
- **Uploading your audio to AudD/ACRCloud is opt-in** and double-gated (see *Adding paid engines*).
  Only upload audio you own or have permission to share.
- Panako is **AGPL**; reference-pool indexing has its own licensing/patent considerations, which is
  part of why it is deferred.

---

## For developers

Full command reference, the schema-regeneration step, and the benchmark/calibration tooling live in
the stage reports under `docs/stage-reports/`. The essentials:

```powershell
uv run pytest -q                 # fast, offline test suite (slow + live tests deselected)
uv run pytest -m "not live"      # everything except the network tests (includes the slow ones)
uv run pytest -m live            # opt-in live/network tests
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run python scripts/audit_fixtures.py   # privacy/committed-data audit
```

- Python 3.12, a `uv` project (`pyproject.toml`, `uv.lock`), source under `src/id_detector/`, a
  `typer` CLI exposed as `id-detector`, tests under `tests/`.
- JSON Schemas are checked in under `docs/schemas/`; regenerate them after a contract change with
  `uv run python scripts/export_schemas.py`.
- Everything is content-addressed and deterministic: recognition evidence is immutable per invocation
  under `work/<source_key>/<media_key>/recognise/invocations/<key>/`, and `--refresh` opens a new
  evidence namespace rather than overwriting an old response.
