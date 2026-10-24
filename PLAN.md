# BCI Voice — Voice + UI Implementation Plan (Claude Code)

`PLAN.md` and `INTERFACE.md` are committed so the teammate can read them; `CLAUDE.md` is local
only (in `.git/info/exclude`) and loads both:

```
@PLAN.md
@INTERFACE.md
```

This plan covers **only the voice and UI side**, built by Elijah. His teammate builds the SSVEP
decoder and the flicker display in a separate repo, `1thing2say/ssvep` (cloned at `../ssvep`,
branch `feat/spectrum-check`). The two sides meet only at the message contract in `INTERFACE.md`.
The partner page runs on the laptop, an iPhone (Safari or the native app in `ios/`) or Android
Chrome (section 10).

At the start of each session, ask Elijah whether he wants code written or only the approach
explained. All work happens on `dev` (or another non-`main` branch); `main` changes only through
pull requests.

---

## Status (2026-10-23)

- Done on `dev`: V0 (schemas, config, event log, fake decoder) and V1 (partner page with screen
  mirror), plus the V2 server, scan-mode orchestrator and speech out. Try it with
  `python -m app.server --fake-decoder` and open `http://127.0.0.1:8000`.
- The server and fake decoder speak only protocol 2 (`INTERFACE-v2-draft.md`) since `d4c1af9`; v1
  is gone from the code. The server sends nothing to the bridge before `hello_ack`, waits for
  `baseline_done` (pausing on a failure or after `baseline.timeout_s`), pauses on
  `display_ok: false` without restarting by itself, and shows `board_ok: false` as a badge.
- **For the teammate:** `python tools/contract_check.py` (our venv) is a stand-in server on
  `:8000/ws/decoder`. Start it, then the bridge; it runs a handshake, two baselines, three trials
  and a paused one, prints PASS/FAIL per check and exits 0 only if everything passed. Our fake
  decoder passes it (`python tools/fake_decoder.py` in a second terminal shows what a passing run
  looks like).
- Read the teammate's `ssvep` code (section 12). Three facts shape this plan:
  1. **Accuracy drops with every target.** On his dry-electrode Cerelog X8, 15 vs 17 Hz decodes at
     78–86 % single-target, and 64–93 % with both squares flickering (section 14); 3 targets
     (12/15/17) only reach 61–66 %; 4 targets were never recorded; 10 Hz is unusable (alpha).
     **Decision (2026-10-24): try 4 targets first** (3 options + NEXT at 13/15/17/11 Hz, the teammate's `four_diamond.toml`), so the
     user sees 3 options at once. A cued 4-target recording decides whether it stays; if not, fall
     back to a list with a moving highlight decoded with 2 targets (section 14, "4-target gate").
  2. **His decoder is continuous, not per trial.** `SlidingDecoder` classifies a 4 s window every
     0.5 s and commits after 3 wins in a row, so a selection takes about 5 s from the start of the
     flicker at best. In the only live run most windows abstained (section 12), so expect longer.
     It already produces per-target scores.
  3. **The flicker is his PsychoPy display** (`ssvep.stimulus`), already validated on his EEG.
     The browser draws no flicker. Nothing speaks `INTERFACE.md` yet, so a bridge on his side runs
     the decoder and the display and talks to our server.
- Other work in this repo (local worktrees, 2026-10-23):
  - `check-implemtation-calhacks-desktop`: a clean-room rebuild of the `ssvep` decoder side as a
    `brainsync/` package (board, recording, CCA/FBCCA, PsychoPy stimulus, `select_freqs`,
    `score_cued`). Mostly uncommitted; no online decoder or bridge yet. Its re-analysis of the
    recordings is what section 14 reports.
  - `custom-operating-brain-seperate`: BrainSyncOS, a Jetson appliance OS (phase 1: base ISO
    extract/repack only). See section 10.
- **Next up:** the 4-target gate recording (section 14) and SYNC 1 to settle section 13 and agree
  `INTERFACE-v2-draft.md` (teammate: please read System design, sections 12, 13 and 14, and the
  draft), then Cartesia TTS (V2 items 3–4), V3 (LLM option ranking) and V4 (partner speech
  input).
- **Cartesia TTS built (2026-10-24):** `app/tts.py` pre-generates each page's 3 clips, the server
  serves them at `/audio/{id}`, `speech.js` plays them with the `speechSynthesis` fallback. Key from
  `.env`; without it the server prints "browser speechSynthesis only". Tested live on the venue
  Wi-Fi: MP3, at most 2 requests at once (plan limit), retries, 8 s per attempt.
- **V3 built (2026-10-24), not yet tried live:** `app/menus.py` asks `claude-haiku-5-5` for each
  page after a partner line and for "More options" (one forced strict tool call, validated, one
  retry); `app/context.py` keeps the conversation. The next cue waits in `PREPARING` up to
  `llm.timeout_s` (4 s), then shows the fixed list; a later page is dropped. The page after a
  spoken reply is the fixed home list. A failed "More options" falls back to the fixed "more"
  list. Needs `ANTHROPIC_API_KEY` in `.env`; the log summary (latency, option-1 rate) is V6.
  The server opens the Claude connection at start and keeps it open (a free `models.retrieve`
  every `llm.keepalive_s` while idle, and right after a failed page), so a page does not pay
  DNS + TCP + TLS inside the 4 s; `llm_failed` logs `ms`. On 2026-10-24 every page timed out on
  congested venue Wi-Fi (1-4 s ping, TLS setup 3-9 s): no code change fixes that, see V6 preflight.
- **Active listening (2026-10-24, branch `feat/partner-listening`):** `stt_mode: always` (default):
  the mic listens the whole session, hands-free, and is off only while a reply plays (plus a
  0.5 s tail), paused, idle or muted. The server owns the mic state (it follows the orchestrator's
  state, a server-held Mute, re-sent to a reconnected page); `ui/listen_core.js` keeps Chrome's
  recognition running (restart loop with backoff and a watchdog, node-tested) and reports its
  state (MIC badge). Fixed on the way: STOP during a clip left the mic off for good, the echo
  filter dropped the partner's "yes", a decoder-reconnect resume never reopened the mic, a spoken
  remark on the confirm screen dropped the pick. The turn mode below remains as `stt_mode: turn`.
- **V4 partner listening (2026-10-24, branch `feat/partner-listening`):** hands-free, no keys. The
  server sends `listen` when it is the partner's turn (the session starts, a reply was spoken) and
  closes the mic while a reply plays; Chrome's recognition joins the partner's sentences into one
  line, drops filler and echoes of the reply. A spoken remark during a selection waits for the
  next cue; a spoken question (detected by its first word, since speech-to-text adds no "?") or
  typed text interrupts. A big Listen button stays for other moments. Needs a manual check in
  Chrome (recognition needs internet and mic permission).
- **User profile and plain replies (2026-10-24, branch `feat/personality-profile`):** replies say
  only basic things (the main answers to a question; profile facts only when asked about) in the
  user's own voice. `/profile.html` edits the user's facts and a personality map (formality,
  length, humour, warmth, directness, phrases), saved to the gitignored `profiles/user.yaml`;
  the fictional demo persona is the default. `python tools/llm_eval.py [--profile file]` runs 12
  fixed partner lines through Claude and flags unasked profile details, claimed events, long
  sentences and questions without different answers.
- **Page mode built (2026-10-24):** server, mirror, fake decoder and `contract_check` run page
  mode (3 options + NEXT, confirm step on s1 / s4) with list mode as the config fallback; the v2
  draft describes 4 targets and 2-slot confirm trials.
- **V6 hardening (2026-10-24, branch `feat/v6-hardening`):** `tools/preflight.py` prints PASS /
  WARN / FAIL per demo check and a go / no-go line (`--url`, `--skip-network`, `--json`);
  `tools/log_summary.py` reads the session logs (`--all`, `--json`, `--show-text`) for trials, the
  C / W correct rate, seconds and selections per spoken reply, the option-1 rate and median / p95
  Claude page latency (the V3 "Done when" numbers); `docs/DEMO.md` is the operator's runbook
  (setup, start, preflight actions, headset checklist, demo script, fallbacks, honest claim).
  Still to do: rehearse the script with the live decoder, and fill in the claim's numbers.
- **From Lucid-Voice (2026-10-24, `feat/v6-hardening`):** Cartesia clips are also kept on disk
  (`speech.cache_dir`, gitignored `cache/tts`, keyed by voice, model and format), so they survive a
  restart and play with the network down; the server pre-generates every fixed-list sentence at
  start, `tools/prerender_tts.py` does it ahead of time and preflight's `clips` line counts them.
  `fallback_match` in `config/intents.yaml` picks the fixed page by keyword when Claude is slow
  or down ("How are you?" → feeling, "How was your weekend?" → how_was, "Hi" → greeting,
  "What…/Where…" → open_question; otherwise yes_no for a question, home for the rest). The
  browser fallback voice (`ui/voice_core.js`) prefers local Premium / Enhanced English voices
  and never a network voice.

- **Quality fixes (2026-10-24, branch `feat/quality-fixes`):** fewer Claude pages lost to the
  fallback: one spare candidate (`menus.spare_options`), repeats and duplicates compared without
  case or punctuation, and a hedged second call after `llm.hedge_after_s` (live `llm_eval`: 11/12
  pages valid, the miss was the cold first connection). A decoder whose hello name starts with
  "fake" counts as fake even when started by hand: FAKE DECODER badge, preflight WARN,
  `decoder_acked` in the log. `/api/health` uses public properties only.

- **Partner page refresh (2026-10-24, branch `feat/ui-refresh`):** the mirror shows each option's
  sentence as the big text (its label is a tag), a coloured status banner, a selection timer that
  fills to `stim.max_trial_s`, a stronger picked box and the spoken sentence while it plays. The
  panel puts Stop on top, then a mic card with the partner's words as they talk, the
  conversation (the user's lines under their first name), and an operator footer: one health line
  ("Ready" or what is wrong, details on a click), Correct / Wrong, the profile link. FAKE DECODER /
  KEYBOARD stay on the mirror. Phone layout (≤ 600 px): partner's part first, Stop fixed at the
  bottom. Logic in `ui/view_core.js` (node-tested); `python tools/ui_shots.py` screenshots every
  state with headless Chrome into `cache/ui_shots/`. Colours are variables in `:root`, palette
  still to choose.

- **Phone version (2026-10-24, branch `feat/mobile`):** `python -m app.server --tunnel` opens a
  Cloudflare quick tunnel (`app/tunnel.py`, `cloudflared`, no account) and prints a phone link
  with a per-start access key; the laptop page offers **Copy phone link**. `app/remote.py` gates
  every request from outside this computer: the key (link once, then an HttpOnly cookie), else
  403 / 4403, and `/ws/decoder` is local only (rule 1). iPhone Safari: the start tap unlocks audio
  (one reused audio element), the screen stays awake (Wake Lock), safe-area insets, 16 px inputs.
  Checked through a real tunnel; the iPhone test itself is still to do. Server shutdown now runs
  `app.state.on_shutdown` (tunnel, fake decoder) from the lifespan. The native app below is now
  the main phone client; this Safari page stays its fallback.
- **iOS partner app (2026-10-24, branch `feat/mobile`):** `ios/BrainSyncPartner`, a SwiftUI app
  (iOS 17+, no dependencies) that replaces the Safari page. It is a `/ws/ui` client like the
  browser (`?client=ios`): mirror, Stop, mic card, typing, conversation, Correct / Wrong. Pairing:
  the server prints an app link, `brainsync://pair?url=…&key=…` (`--tunnel`, or `server.host:
  0.0.0.0` for plain LAN http, where `main()` finds the LAN address), and the laptop page offers
  **Copy iPhone app link**; the web phone link pasted into the app works too. Native parts:
  on-device speech recognition (`SFSpeechRecognizer`, echo cancellation, the `listen_core.js`
  rules ported), Cartesia clips through `AVPlayer` with the built-in `AVSpeechSynthesizer` voice
  as fallback, screen kept awake. While it is connected it is the one partner page: the laptop's
  page shows "Open on another page" (close code 4000), and `/api/health` / preflight say which
  client is connected (`ui_client`). `ios/BrainSyncKit` (Swift package: links, messages, socket,
  `ListenCore`) is tested against `ui_messages.json`, written from `app/schemas.py` by
  `tools/ui_fixture.py` (`--check` fails when stale). Built on the teammate's Mac with Xcode 16
  (`ios/README.md`); not yet compiled or run on a device.
- **Slot frequencies (2026-10-24):** `stim.slots` now matches the teammate's `four_diamond.toml`:
  s1 13, s2 15, s3 17, s4 11 Hz (was 15/17/20/12), so his bridge's `hello` passes our check.

## System design

```
 Demo Mac (the teammate's; fixed 60 Hz, ProMotion off)
 ┌───────────────────────────────────────────────────────────────────────┐
 │  BrainSync server (our venv)         Bridge (their venv)                │
 │  FastAPI + Orchestrator     ◀─ws─▶   connects board once (~16 s)        │
 │  (owns all state)  /ws/decoder       SlidingDecoder (4 s / 0.5 s / 3)   │
 │  menus.py ──https──▶ Claude API      │ stdin: int box + JSON commands   │
 │  log.py (JSONL)                      ▼          ▲ stderr: onset markers │
 │        ▲ /ws/ui                     PsychoPy display (full screen)      │
 │        ▼                             flicker, option text, box, Esc     │
 │  Chrome partner page                                                    │
 │  mirror, STOP, TTS out, push-to-talk                                    │
 └───────────────────────────────────────────────────────────────────────┘
        Cerelog X8 ──USB serial──▶ Bridge (the only reader of the port)
```

| Process | Owner | Owns | Talks to |
| --- | --- | --- | --- |
| BrainSync server (`app/`) | Elijah | All state: trials, options, what is shown and spoken, the log | Bridge (`/ws/decoder`), partner page (`/ws/ui`), Claude API |
| Bridge (`ssvep/online/bridge.py`) | Teammate | The board connection, the decoder, the display process | Server (websocket client), display (stdin/stderr) |
| PsychoPy display (`ssvep.stimulus`) | Teammate | Frame-locked flicker, option text, the box, `Esc` | Bridge only |
| Chrome partner page (`ui/`) | Elijah | Rendering, TTS, STT, STOP | Server only |
| iPhone partner app (`ios/`), optional | Elijah | The same as the Chrome page, natively; replaces it while connected | Server only (`/ws/ui?client=ios`) |
| Claude API | — | Ranked reply options | Server only (`app/menus.py`) |

The server ↔ bridge contract is `INTERFACE-v2-draft.md` (protocol 2): the server speaks only
this, pending the teammate's confirmation at SYNC 1. `INTERFACE.md` (v1) is superseded and stays
only until the draft replaces it. The server ↔ browser messages are in section 6.

Design risks to settle:

1. **One screen cannot show both windows.** The flicker must be full screen (the teammate's rule,
   enforced by PsychoPy), but the partner page needs a visible Chrome window for push-to-talk and
   STOP. Options: an external monitor for Chrome, with PsychoPy pinned to the built-in panel
   (`screen = 0`, locked at 60 Hz); or the partner page on the partner's iPhone (section 10:
   the native app, Safari as fallback; speech recognition still to check on a device). With Chrome, PsychoPy and
   the decoder on one Mac, SYNC 1.5 measures dropped frames with Chrome open.
2. **The contract is not confirmed yet.** The server speaks only protocol 2
   (`INTERFACE-v2-draft.md`), but the teammate has not agreed it, and the bridge cannot be built
   until they do, so it gates SYNC 2. The server side of v2 is built, including
   `tools/contract_check.py` for testing the bridge.
3. **The bridge is a single point of failure.** It holds the board, the decoder and the display;
   a crash costs the ~16 s board connect plus a baseline. Keyboard mode is the fallback, and the
   demo script rehearses switching to it.
4. **Time per reply.** One pick is cue 1 s + decode ≥ 5 s + highlight 0.6 s, then the confirm
   step (decode ≥ 5 s), then speech + rest 1 s: about 13–14 s at best. Each NEXT (new page of 3)
   adds about 7 s, and each abstained trial repeats. Ranking quality (V3) and the decoder's abstain
   rate (section 13, item 10) matter more than any other choice. Showing the decoder's scores on
   the partner page makes waiting look like progress.
5. **STOP takes three hops** (Chrome → server → bridge → display stdin). `Esc` on the display is
   local and immediate, so it is the primary stop, and the bridge turns the flicker off whenever it
   loses the server (v2 draft, rule 3).
6. **4 targets may not decode well enough.** Untested on this headset; 3 targets already fall to
   61–66 %. The 4-target gate (section 14) runs before the teammate builds per-target text, and
   the fallback keeps all options visible.

## 0. Event constraints

- Built at **Cal Hacks 13.0** (Oct 23–25, 2026), 36 hours, **2-person team**.
- Commit small and often with clear messages; organizers may review commit history.
- Time beats polish. Build the simplest version that passes each phase's acceptance check. Anything
  marked *stretch* waits until the demo works end to end.
- Never block on the decoder. Until it is ready, everything runs against `tools/fake_decoder.py`,
  which speaks the same contract.
- Feature freeze at **hour 28** from kickoff; after that only bug fixes, write-up and rehearsals.

## 1. What this side does

The decoder tells us which of **4** targets the user looked at. This side does everything else:

1. Decides what the user's screen shows: **3 reply options at once**, each next to its own
   flickering target, plus a fourth target, **NEXT** (show the next page of options). His PsychoPy
   display draws it; we send it the text and when to flicker, through the bridge.
2. Tells the decoder when each selection period (trial) starts and stops, and when the screen must
   be still for its baseline.
3. Receives the selected target; on an option, asks the user to confirm it ("Say it" / "Back"),
   then speaks that option's sentence aloud.
4. Listens to the conversation partner (speech-to-text or typed) and asks the LLM for a **ranked**
   list of replies that fit what was just said, most likely first.
5. Runs the **partner page** in Chrome: conversation, typed input, push-to-talk, status, STOP,
   speech output, and a mirror of the user's screen (no flicker).

**Page mode (default, decided 2026-10-24).** The LLM returns 3 options ranked by likelihood. All 3
are on screen at once, each beside its own flickering target, so the user never picks an option
just because they can't see what comes next. The fourth target is NEXT, which asks for a new page
of 3 ("More options"). Looking at an option opens a confirm step: the same screen shows the
picked sentence with two targets, "Say it" and "Back". Only "Say it" speaks. With good ranking,
most replies take two selections (pick + confirm). The live decoder abstains often today
(section 12), so some trials will end as "Didn't catch that" and repeat; the teammate's tuning
(section 13, item 10) should shorten this.

**Fallback if the 4-target gate fails (section 14): list mode.** Same page of options on screen,
one highlighted; 2 targets, SAY (speak the highlighted option) and NEXT (move the highlight; past
the last option, new page), at 15/17 Hz. The user still sees every option; it costs speed, not
visibility. No confirm step needed, since SAY is already a deliberate 2-class choice.

Demo story: a partner asks "Do you want to go outside?", the options become "Yes, let's go
outside", "Not right now" and "Maybe after lunch", the user looks at the first, confirms, and the
laptop speaks it.

**Pitch claim:** an LLM that writes context-aware replies turns a few-target SSVEP BCI on a
dry-electrode headset into a conversation, with every option visible and nothing spoken without a
confirmation. State the measured accuracy for the target count actually used.

## 2. Non-negotiable rules

1. **Only the decoder (or keyboard mode) decides what was selected.** The LLM never picks, guesses
   or changes a selection.
2. **Speak exactly the selected option's `utterance`, and only after "Say it".** No rewriting after
   selection. The full sentence (not just the label) is on the user's screen during the cue and the
   confirm step, and during the flicker unless that drops frames (SYNC 1.5), before it can be
   spoken.
3. **The shown options never change during a trial.** They change only in `MENU_CUE`. Enforce with
   an assertion and a test.
4. **One target is always NEXT, on the same slot (s4).** The LLM only fills the 3 option slots; it
   never controls navigation.
5. **Every LLM response is validated** (pydantic). On timeout, error or invalid output, use a
   fallback list from `config/intents.yaml`. The app must keep working with the LLM or internet down.
6. **The server owns all state.** His display only draws what it is told and reports onsets; the
   browser only renders, plays audio and reports events. This keeps the logic testable and makes a
   phone client possible later.
7. **Stimulus timing and layout belong to his display** (frame-locked, from frame counts).
   Frequencies, phases, waveform and sizes live in his stimulus config (`two_targets.toml` or a
   copy; a new `four_targets.toml` for page mode). Our config only names the slots and their roles, and is checked against what his `hello`
   reports.
8. **No hardware or decoder guessing.** Anything about the decoder's internals, window length or
   frequencies: ask, read `INTERFACE.md`, or read `../ssvep`.
9. **Secrets** only from environment variables (`ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`), loaded
   from the gitignored `.env` at the repo root if set there. Never commit, print or log them.
10. **Safety:** flicker warning before the first trial (partner page and his display's start
    screen), `Esc` in his display stops all flicker immediately, and the partner page's STOP does
    the same through the bridge. 11, 13, 15 and 17 Hz are all in the photosensitive range (15 and
    17 Hz in the 15–25 Hz band most likely to trigger seizures), and 4 flickering squares cover more of the screen than 2:
    nobody with photosensitive epilepsy or flicker sensitivity uses it.
11. Ask before adding dependencies outside section 4. All tunable numbers live in
    `config/default.yaml`.

## 3. Repository layout

Two repos. The decoder and display live in `1thing2say/ssvep` (his); this repo is
`1thing2say/BrainSync`.

```
BrainSync/
  CLAUDE.md             # local only
  PLAN.md
  INTERFACE.md          # v1, superseded; kept until the draft replaces it
  INTERFACE-v2-draft.md # protocol 2: what the server speaks; teammate to confirm at SYNC 1
  pyproject.toml
  config/
    default.yaml        # all tunables (section 5)
    intents.yaml        # allowed intents + fallback option lists
    profile.yaml        # fictional demo user persona
  app/
    server.py           # FastAPI: serves ui/, /ws/ui, /ws/decoder
    orchestrator.py     # state machine (server-side, authoritative)
    schemas.py          # pydantic models, incl. the INTERFACE.md messages
    menus.py            # LLM option ranking, validation, fallback
    context.py          # conversation history
    tts.py              # Cartesia client, audio cache, /audio route helper
    log.py              # JSONL event log
  ui/                   # partner page; no flicker
    index.html
    style.css
    app.js              # websocket client: conversation, status, STOP, mirror of the user's screen
    speech.js           # plays Cartesia audio, speechSynthesis fallback; STT (Web Speech API)
    voice_core.js       # picks the fallback voice: local Premium / Enhanced English first
    view_core.js        # banner, health line, mic card, selection timer (pure, node-tested)
  ios/                  # native iPhone partner app; ios/README.md (install, pair)
    BrainSyncPartner.xcodeproj   # Xcode 16+, iOS 17+
    BrainSyncPartner/            # SwiftUI app: pair screen, warning, partner view, mirror,
                                 #   Listener (on-device STT), Speaker (AVPlayer + system voice)
    BrainSyncKit/                # Swift package: ServerLink, messages, UIConnection, ListenCore
      Tests/BrainSyncKitTests/Fixtures/ui_messages.json   # every /ws/ui message, from schemas.py
  tools/
    fake_decoder.py     # stand-in bridge speaking protocol 2: keyboard / random / scripted modes
    contract_check.py   # stand-in server on :8000 for the bridge; PASS/FAIL per check, exit 0 = pass
    preflight.py        # demo go/no-go checks
    prerender_tts.py    # Cartesia clips for every fixed-list sentence, into speech.cache_dir
    ui_shots.py         # screenshots of the partner page's states (headless Chrome)
    ui_fixture.py       # writes ios/.../ui_messages.json from app/schemas.py (--check: stale?)
    log_summary.py      # V6 numbers from logs/*.jsonl (C/W rate, s per reply, option-1 rate, latency)
  docs/
    DEMO.md             # operator's demo runbook (V6 items 2-3)
  logs/                 # gitignored
  tests/

../ssvep/               # teammate's repo, separate venv (Python 3.10+, numpy/scipy, PsychoPy)
  ssvep/online/decoder.py   # SlidingDecoder (what the bridge wraps)
  ssvep/online/bridge.py    # NEW: protocol 2 client; runs decoder + display (section 13, item 3)
  ssvep/stimulus/           # PsychoPy display; NEW: text and flicker on/off commands (item 2)
```

## 4. Stack

- Python 3.11+: `fastapi`, `uvicorn[standard]`, `websockets` (fake decoder client), `pydantic` v2,
  `pyyaml`, `anthropic`, `pytest`, `pytest-asyncio`, `httpx`. Separate venv from `../ssvep`; we
  never import his package.
- Flicker: his PsychoPy display (`python -m ssvep.stimulus`), in his venv.
- Browser: **Chrome** (needed for the Web Speech API), for the partner page only. Plain
  HTML/JS/CSS, no framework, no build step.
- TTS: Cartesia sonic-3.5 via REST `/tts/bytes`, pre-generated per menu; browser `speechSynthesis`
  (local voice) as fallback. Called with `httpx`, not the `cartesia` SDK (older examples use
  outdated model names such as `sonic-english`).
- STT: Web Speech API (`webkitSpeechRecognition`). It needs internet in Chrome, so a typed text box
  is always available as fallback.
- LLM: Anthropic Messages API, default `claude-haiku-5-5` for speed; force a single tool call whose
  input schema is the ranked option list, so the output is structured JSON.
- **Demo machine: the teammate's Mac.** The vendored BrainFlow for the Cerelog X8 ships only macOS
  `.dylib` files. ProMotion / variable refresh must be off (fixed 60 Hz); with it on, he measured
  no SSVEP at all. Develop on Windows/WSL against the fake decoder; the partner page's mirror
  stands in for the PsychoPy screen.

## 5. Config defaults (`config/default.yaml`)

| Key | Default | Notes |
| --- | --- | --- |
| `server.host` / `server.port` | `127.0.0.1` / `8000` | `0.0.0.0` for the iPhone app on the LAN (or use `--tunnel`), later the Jetson |
| `stim.slots` | s1 13 Hz, s2 15 Hz, s3 17 Hz, s4 11 Hz | Mirrors his `four_diamond.toml` (changed 2026-10-24 from 15/17/20/12); checked against the decoder's `hello`. Pending the 4-target gate (section 14); list-mode fallback is s1 15 Hz, s2 17 Hz |
| `stim.roles` | s1–s3 `option`, s4 `next` | Which slot does what. NEXT on 11 Hz, the lowest and nearest the user's alpha, so the likeliest error is a harmless page turn. Fallback: s1 `say`, s2 `next` |
| `menus.mode` | `page` | `page` (all options on their own targets) or `list` (highlight + SAY/NEXT, the fallback) |
| `stim.cue_s` | 1.0 | Option shown, no flicker, before each trial |
| `stim.max_trial_s` | 10.0 | Decoder needs about 5 s (4 s window + 2 × 0.5 s votes) to commit; revisit with the decoder's tuned window, votes and margin (section 13, item 10) |
| `stim.rest_s` | 1.0 | Still screen after a trial |
| `baseline.seconds` | 10.0 | Still screen at start for the decoder's resting baseline |
| `baseline.timeout_s` | 15.0 | No `baseline_done` from the bridge by then → pause |
| `menus.dynamic` | true | false = fallback lists only |
| `menus.option_count` | 3 | Ranked options per page (one per option slot). List mode may use 3 or 4 |
| `menus.label_max_chars` | 18 | |
| `menus.label_max_words` | 3 | |
| `menus.utterance_max_words` | 14 | |
| `menus.spare_options` | 1 | Extra lower-ranked candidates Claude writes; a bad or repeated option is dropped and the next one fills the page, so one slip no longer loses it |
| `llm.model` | `claude-haiku-5-5` | |
| `llm.timeout_s` | 4.0 | The whole budget per page: at most 3 calls (first, hedge, one retry), then fallback |
| `llm.hedge_after_s` | 2.5 | No answer by then: the same call again, in parallel; the first usable answer wins. 0 = off; must be below `timeout_s` |
| `llm.keepalive_s` | 20.0 | While idle, a free API call this often keeps the connection open; 0 = off |
| `context.history_turns` | 8 | Partner + user turns sent to the LLM |
| `speech.provider` | `cartesia` | `cartesia` or `browser` (speechSynthesis only) |
| `speech.voice_id` | `47c38ca4-…` | Cartesia voice id |
| `speech.model` | `sonic-3.5` | Pinned; never `sonic-latest` |
| `speech.api_version` | `2026-03-01` | `Cartesia-Version` header. The API reference (checked 2026-10-24) lists `2026-08-14`; switch if a test synthesis is rejected |
| `speech.language` / `speech.format` | `en` / `mp3` | MP3 22050 Hz, 64 kbps: about 17 KB a sentence, 10x smaller than 44.1 kHz WAV, which the venue Wi-Fi needed; `wav` (`pcm_s16le`) still works |
| `speech.tts_timeout_s` | 8.0 | Per clip; slower or failed → that slot has no audio, the browser speaks it. Was 2.0: on the venue Wi-Fi every clip timed out (1–3 s per request); a clip is needed ≥ 13 s after its page appears |
| `speech.max_concurrent` | 2 | Cartesia plan limit; above it requests get HTTP 429 `concurrency_limited` |
| `speech.cache_dir` | `cache/tts` | Clips on disk (gitignored), reused across restarts and offline; `""` = memory only |
| `speech.tts_rate` | 1.0 | speechSynthesis fallback only |
| `speech.stt_mode` | `always` | Hands-free: the mic listens the whole session except while a reply plays, paused, idle or muted. `turn` = only at the start and after each reply (`listen_window_s`); `push_to_talk` = only the button |
| `speech.listen_window_s` / `speech.stt_tail_s` | 10.0 / 0.5 | Turn mode and the Listen button while paused; the wait after a reply ends before the mic reopens (≈0.8 for a Bluetooth speaker) |
| `speech.stt_interrupts` / `speech.stt_interrupt_cooldown_s` | `question` / 20.0 | A spoken question may stop a page trial, at most once per 20 s and never a pick in progress; `never` = spoken lines always wait for the next cue. Typed text always interrupts |
| `speech.stt_gap_s` / `speech.stt_max_line_s` / `speech.stt_echo_s` | 1.2 / 15.0 / 4.0 | Silence that ends a partner line; a monologue is sent after this long; lines this soon after the mic reopens are checked for the reply's echo |
| `speech.start_muted` | false | The partner's Mute (held by the server) at session start |
| `speech.max_speak_s` | 15.0 | Move on if the page never reports `tts_done` |
| `selection.confirm_mode` | `always` | `never` / `always` / `low_score` (needs decoder scores). `always` in page mode, so a wrong pick is never spoken; `never` in list mode |
| `selection.chosen_highlight_s` | 0.6 | Show which target was picked |

Layout, sizes, waveform and the refresh check live in his stimulus config (`two_targets.toml`:
sine, 0.4 screen-height squares one screen height apart; `four_targets.toml` for page mode). The
server supports any number of slots, so switching between page mode and list mode is a config
change on both sides.

Page-mode layout (his config, our mirror copies it): a diamond, squares as far apart as the screen
allows, a small fixation mark at the centre of each square (gaze drifts after about 2 s without
one, section 12), each option's full sentence right beside its square, NEXT at the bottom.

## 6. Shared messages

The UI ↔ decoder contract is protocol 2 in **`INTERFACE-v2-draft.md`** (it replaces `INTERFACE.md`
once the teammate confirms it at SYNC 1); `app/schemas.py` implements it exactly. Any change to it
must be agreed with the teammate and made in that file first.

Internal browser ↔ server messages (`/ws/ui`, JSON with a `type` field):

- Server → browser: `show_page` {list_id, page, mode: page|list|confirm, highlight_index,
  slots[{slot, role: option|next|say|back, label, utterance}]} (one entry per target; in list mode
  the options ride along as `options[{label, utterance}]`), `trial` {trial_id, phase:
  cue|flicker|rest|baseline}, `highlight` {slot},
  `speak` {text, utterance_id, audio_url?}, `listen` {seconds, delay_s, until_stopped, muted,
  reason} (what the partner's mic does; the server owns it), `state` {name, detail},
  `transcript` {who, text},
  `status` {decoder_connected, decoder_ok, display_ok, llm_ok, fake_decoder, keyboard_mode}.
- Browser → server: `tts_done` {utterance_id}, `partner_text` {text, source: stt|typed},
  `key` {key}, `stop` {}, `resume` {}, `judgment` {utterance_id, correct}, `mic_set` {muted},
  `mic_report` {state, restarts} (no transcript text).

Times are Unix seconds. Flicker onsets come from his display (the flip of the first flickering
frame, `--markers unix`), relayed by the bridge. His live decoder does not epoch on onsets (it
slides over the stream), so onset accuracy matters for logging and his offline analysis, not for
live decoding.

## 7. State machine (`app/orchestrator.py`)

```
IDLE --(warning accepted)--> BASELINE (still screen, baseline.seconds) --> MENU_CUE
MENU_CUE --(cue_s, page ready)--> STIMULATING
STIMULATING --selection(option k)--> SELECTED --> CONFIRMING (sentence k shown; targets "Say it" / "Back")
CONFIRMING --selection(say)-->  SPEAKING --(tts_done)--> REST --> MENU_CUE (new page)
CONFIRMING --selection(back) / no_selection / timeout--> REST --> MENU_CUE (same page)
STIMULATING --selection(next)--> SELECTED --> REST --> MENU_CUE (next page of 3)
STIMULATING --no_selection/timeout--> NOT_CAUGHT ("Didn't catch that") --> REST --> MENU_CUE (same page)
STIMULATING --partner_text--> stop trial (reason context_changed) --> MENU_CUE (new page)
any state --STOP / display Esc / decoder lost--> PAUSED (flicker off) --(resume)--> BASELINE
```

- `MENU_CUE` waits for the next page up to `llm.timeout_s`, then uses a fallback page.
- `CONFIRMING` is its own trial (new `trial_id`) with only two active slots: "Say it" on s1
  (13 Hz) and "Back" on s4 (11 Hz), so the confirm step is a 2-class decision. A timeout counts as
  Back: nothing is ever spoken by default.
- NEXT twice in a row on a "More options" page → home page.
- **List mode** (fallback, `menus.mode: list`): `STIMULATING --selection(say)--> SPEAKING`;
  `--selection(next)-->` moves the highlight, past the last option loads a new page. No
  `CONFIRMING` (`confirm_mode: never`).
- Each trial has a new `trial_id`. Selections with an old `trial_id` are logged and ignored.
- **Spoken partner lines never throw away the user's work:** during a pick in progress (the Say it
  / Back screen) they only change the screen after the pick; a remark during a page trial waits for
  the next cue; a question interrupts at most once per `stt_interrupt_cooldown_s`. Typed text
  always interrupts.
- Implement as a class driven by events plus an injectable clock, so tests run without timers.

## 8. Phases (voice/UI track)

Hours are from kickoff. Integration checkpoints with the teammate are marked **SYNC**.

### V0 — Setup and contract (h0–2)

1. Scaffold `pyproject.toml`, `.gitignore` (`logs/`, `.env`), `pytest`, folder layout.
2. `app/schemas.py`: all `INTERFACE.md` v1 messages + internal messages, with round-trip tests.
   Accept optional `score`/`scores`. (Built against v1; moved to protocol 2 on 2026-10-23.)
3. `config/*.yaml` and a loader with validation (unique slot ids, positive timings; page mode:
   exactly one `next` and `option_count` `option` slots; list mode: exactly one `say` and one
   `next`). *Update 2026-10-24: add page mode and its roles.*
4. `tools/fake_decoder.py`: connects to `/ws/decoder`, answers `stim_start` with a `selection` after a
   delay. Modes: `--keyboard` (type 1–4, or 1–2 when only two slots are active), `--random`,
   `--script file.yaml`, `--fail-rate 0.2`
   (sends `no_selection` sometimes), `--latency 5` (default; mimics the real decoder's commit
   time), `--scores` (sends fake `scores`). Logs any display commands it receives.
5. `app/log.py`: JSONL, one file per session, flush per event.

Done when: tests pass; `fake_decoder.py` connects to a stub server and exchanges `hello`.

**SYNC 1 (h2):** go through section 13 with the teammate: how the text reaches his display, scan
vs pair menus, the bridge, the `INTERFACE.md` changes. Update `INTERFACE.md` (he agrees, we
commit) and `config/default.yaml` to match.

### V1 — Partner page and screen mirror (h2–5)

1. `ui/app.js` partner page: conversation panel, typed input, push-to-talk button (wired in V4),
   status badges (decoder, display, LLM, FAKE DECODER, KEYBOARD), a big STOP button and resume.
2. Mirror of the user's screen: the 4 boxes in the diamond with each option's sentence and NEXT
   (or the confirm screen, or list mode's highlighted list), static (no flicker), with the trial
   phase and the picked box highlighted. Used for development without PsychoPy and so the audience
   can follow along. *Update 2026-10-24: built for SAY / NEXT; rework for page mode.*
3. Flicker warning in the partner page; the first trial waits until it is accepted.
4. Keyboard mode: keys 1–4 select a target (1 = Say it, 4 = Back on the confirm screen); demo
   fallback if the decoder fails.

Done when: the mirror follows the server through 20 fake-decoder trials.

**Teammate, in parallel (h2–6):** display commands in his PsychoPy display (section 13, item 2)
and the bridge (item 3). Elijah can take either as a PR to his repo if he prefers.

**Teammate, first (before the display work): the 4-target gate recording** (section 14). Its
result decides whether the display needs per-target text on 4 squares (page mode) or one list with
a highlight (list mode).

**SYNC 1.5 (h5): text on the PsychoPy screen.** Drive his display with the new commands through
60 s of cue/flicker/rest cycles with changing text (4 sentences in page mode); his session log
shows no more dropped frames than plain `--free`. If drawing text during the flicker drops frames, show it only during the cue
and rest (his `labels_during_stim = false` rule exists for this reason).

### V2 — Orchestrator and speech out (h4–8)

1. `app/server.py`: FastAPI serving `ui/`, websocket endpoints `/ws/ui` and `/ws/decoder`, reconnect
   handling, one active UI and one active decoder.
2. `app/orchestrator.py`: section 7 state machine with a static list from `intents.yaml`; sends
   display commands (text, flicker on/off, highlight) to the decoder connection. *Update
   2026-10-24: built as scan mode; add page mode with `CONFIRMING`, keep list mode as the
   fallback.*
3. `app/tts.py`: as soon as a page passes validation, request audio for its 3 option utterances
   concurrently (NEXT / "More options" needs none): `POST https://api.cartesia.ai/tts/bytes` with
   headers `Authorization: Bearer $CARTESIA_API_KEY`, `Cartesia-Version: 2026-03-01`,
   `Content-Type: application/json`, and body `model_id` (`speech.model`), `transcript` (the exact
   utterance, rule 2), `voice` {`id`: `speech.voice_id`} and an `output_format` of a WAV container
   with `pcm_s16le`, so a plain `<audio>` element plays it (check the current docs for the exact
   `output_format` field names before implementing). Cache the bytes keyed by a hash of utterance
   text + voice_id and serve them at `/audio/{id}`; the clip is cached before the selection arrives,
   so playback is instant. A request that fails or takes longer than `speech.tts_timeout_s` leaves
   that slot without audio.
4. `ui/speech.js`: when `speak` has an `audio_url`, play it with an `<audio>` element and send
   `tts_done` on `ended`; with no URL, or if playback errors, speak with `speechSynthesis` (in
   Chrome a voice with `localService === true`; the "Google" voices are network voices and fail
   offline) and send `tts_done` when it finishes. Show the sentence on screen while speaking.
5. Log every event.
6. *Stretch (after the demo works end to end):* voice cloning for a "voice banking" story, with the
   person's consent only, noted in the README as a demo voice.

Tests: table-driven transition tests (option → confirm → say, option → back, confirm timeout
speaks nothing, next, next twice → home page, timeout, stale trial_id, context change, STOP →
baseline, and the list-mode set); `show_page` and display text are never sent while
`STIMULATING`; nothing reaches `SPEAKING` in page mode without passing through `CONFIRMING`.
TTS: a mocked Cartesia success caches the audio; a timeout or error leaves `audio_url` empty; the
cache is reused for identical utterances.

Done when: with `fake_decoder.py --random`, the app runs 20 trials in a row unattended in each
mode, speaking only confirmed picks, with no stuck states.

**SYNC 2 (h6):** his bridge connects to `/ws/decoder` (with `FakeSource` if the headset is not on),
shows our text on his display and completes 5 trials, and passes `tools/contract_check.py`, the
stand-in server (`INTERFACE-v2-draft.md` section 8).

### V3 — LLM option ranking (h8–12)

1. `app/context.py`: last `history_turns` turns (partner and user), time of day, profile.
2. `app/menus.py`: one LLM call per page. Input: what the partner last said, recent turns, time,
   profile, allowed intents, utterances already shown (for "More options"). Output: exactly
   `option_count` (3) options **ranked most likely first**, each `{label, utterance, intent_id}`.
   Option 1 goes on s1 (15 Hz), which also carries "Say it" on the confirm screen.
3. Prompt rules: first person, as the user; utterance up to 14 words; label up to 3 words and 18
   characters; options clearly different from each other; answer what the partner just said when
   there is a question; for yes/no questions put the likelier answer first and include a "no / not
   now" option in the top 2; never invent facts not in the profile or conversation; no medical
   advice.
4. Validate; on failure retry once, then fallback (a yes/no/maybe list if the partner's last line
   ends with "?", otherwise the home list).
5. Prefetch: start the next list request as soon as the partner speaks or a sentence is spoken.
   *Stretch:* prefetch the "More options" list while the user scans the current one.

Tests: mocked LLM returning valid, invalid, slow and duplicate-option responses; fallback always
yields a full list.

Done when: typed partner questions produce fitting options within `llm.timeout_s` on venue Wi-Fi;
logged median and p95 list latency, and how often the spoken reply was option 1, are reported in
the log summary.

### V4 — Partner input (h11–14)

1. Push-to-talk (button or held Space) using `webkitSpeechRecognition`; final transcript is sent as
   `partner_text`. Mic stays off while TTS is speaking so the app never hears itself.
2. Typed input box always works (Enter to send).
3. Conversation panel showing partner lines and spoken user lines.
4. New partner text during a trial stops the trial (`context_changed`) and loads the new list.

Done when: a spoken question changes the options, and the spoken reply appears in the conversation
panel.

### V5 — Live integration (h14–19)

1. Run on his Mac with the real headset, his display and his bridge. Check that the display's onset
   times line up with board timestamps (both Unix time on the same machine).
2. Agree on final `max_trial_s`, `rest_s` and the decoder's `window_s` (4 s default; 2 s scored as
   well or better in 2 of 3 cued recordings, and would cut each selection to about 3 s), and on
   the frequency set and mode (section 14, 4-target gate).
3. Handle decoder or display loss mid-trial: pause, show "Decoder disconnected", resume on
   reconnect (through `BASELINE`).
4. If the decoder sends scores, enable `confirm_mode: low_score` (*stretch*).

**SYNC 3 (h16):** first live run: a real person selects with their eyes and the laptop speaks.

Done when: 10 live trials complete; outcomes are logged with the user's correct/wrong mark (`C`/`W`
keys).

### Sleep (h19–24)

Take shifts with the teammate. Commit a working state before sleeping.

### V6 — Hardening and demo mode (h24–28)

1. `tools/preflight.py`: decoder connected and `status.ok`, display up (refresh check passed, fixed
   60 Hz, ProMotion off), Chrome partner page open, API key, internet, audio out, mic,
   `CARTESIA_API_KEY` set and one test synthesis succeeding (if not, report fallback-only mode:
   browser speech); prints go/no-go. Headset checklist, from the teammate's sessions:
   - Headset on at least 20 minutes before recording or demoing; unsettled contacts ruined the
     first trials of the first two-target recording.
   - One eyes-closed alpha check (`spectrum_check -t 10`): a posterior 9–10 Hz peak and RMS under
     20 µV on O1 and O2.
   - Nothing else reading the board's serial port (a live plot splits the stream).
   - Allow about 16 s for the board to connect; the "Handshake verification failed" line it logs
     first is harmless.
2. One command to start: `python -m app.server` (+ `--fake-decoder`, `--keyboard`).
3. Demo script: 3–4 partner questions where the right reply is on the first page, plus one that
   needs NEXT, and one deliberate "Back" to show that nothing is spoken without confirmation.
   Rehearse with the live decoder.
4. Log summary: trials, correct/wrong rate from `C`/`W` marks, seconds per spoken reply, selections
   per spoken reply, median list latency.

**Feature freeze at h28.**

### V7 — Submission (h28–36)

1. `README.md`: what it is, honest claim (target count and mode used, dry electrodes, measured
   accuracy from the gate recording and the live log), how to run,
   safety notes, numbers from the log summary.
2. Devpost write-up and a screen recording of a full live run (backup: keyboard mode, clearly labeled).
3. Two clean rehearsals with the teammate.

## 9. Testing

- Orchestrator: event-driven unit tests with an injected clock; no real timers.
- Schemas, config validation and option validation: table-driven tests.
- Integration: start the server, connect a fake browser client and `fake_decoder.py` in-process,
  run 20 trials headless.
- LLM calls are mocked in tests; one optional live smoke test behind `RUN_LLM_TESTS=1`.
- The flicker is his: dropped frames come from his display's session log (SYNC 1.5).

## 10. Phone (built) and Jetson (later)

- The partner page can run on the partner's phone (mic, transcript, conversation, Stop) while the
  laptop runs his display for the user. Only one partner page is active; a new one replaces the
  old (close code 4000).
- **iPhone: the native app** (`ios/`, `ios/README.md`), installed from Xcode on the teammate's
  Mac. It pairs with the app link the server prints. Two ways to reach the server:
  `python -m app.server --tunnel` (Cloudflare https, any network) or `server.host: 0.0.0.0`
  (plain http on the same Wi-Fi; the app allows local http, a phone browser gets no mic there).
- Fallback: the web page in iPhone Safari or Android Chrome through the `--tunnel` phone link.
- Remote requests always need the per-start access key (`app/remote.py`, 403 / 4403);
  `/ws/decoder` is never remote.
- The decoder could later run on a Jetson and connect to `/ws/decoder` over the network; the
  contract does not change. The vendored BrainFlow would need Linux ARM builds first.
- BrainSyncOS (worktree `custom-operating-brain-seperate`) remasters NVIDIA's JetPack 7.2.1 ISO
  so a Jetson boots straight into BrainSync. JetPack 7 supports AGX Thor, AGX Orin and Orin
  NX/Nano only, not the original Jetson Nano. Only phase 1 (verified base ISO, extract, no-op
  repack) exists; nothing on the demo path depends on it.

## 11. Commands

```
pip install -e ".[dev]"
pytest
python -m app.server                        # real decoder connects to /ws/decoder
python -m app.server --keyboard             # keys 1–4 select (confirm: 1 = Say it, 4 = Back)
python -m app.server --tunnel               # + a phone link (https, access key) via cloudflared
python tools/fake_decoder.py --random       # in a second terminal
python tools/contract_check.py              # stand-in server for the bridge (instead of app.server)
python tools/llm_eval.py                    # live check of Claude's replies (needs ANTHROPIC_API_KEY)
python tools/preflight.py                   # demo go/no-go (--url, --skip-network, --json)
python tools/prerender_tts.py               # fixed-list clips to disk while the network is good
python tools/ui_shots.py                    # partner page screenshots into cache/ui_shots/
python tools/ui_fixture.py                  # rewrite the iOS message fixture (--check: exit 1 if stale)
(cd ios/BrainSyncKit && swift test)         # BrainSyncKit tests (Mac or Linux Swift toolchain)
python tools/log_summary.py                 # numbers from the newest log (--all, --json, --show-text)

# demo runbook: docs/DEMO.md

# in ../ssvep (his venv), once he adds the bridge
python -m ssvep.online.bridge               # headset + PsychoPy display, connects to our server
python -m ssvep.online.bridge --fake        # FakeSource instead of the headset
```

Open `http://127.0.0.1:8000` in Chrome for the partner page; the bridge opens his display
fullscreen.

## 12. What the teammate's code does (`../ssvep`, read 2026-10-23)

- **Repo state:** the online decoder, demo, classifiers and cued-trial tool are on
  `feat/spectrum-check`, pushed to PR #6 (commits `afd4dd5`, `1c2081e`, `4f2b048`, `d546dc2`), which
  is stacked on PR #5 (vendored BrainFlow). The local clone at `../ssvep` predates them (no
  `online/`): pull before writing the bridge against it.
- **Board:** `acquisition/board.py`, Cerelog X8, 8 channels at 250 Hz, `read() -> (X µV, t Unix)`.
  Montage used: Fp1, Fp2, C3, C4, P7, P8, O1, O2; decoding uses the posterior four. C3 and C4 do
  not reach the scalp and float (200+ µV), which degrades the bias loop; tie them to CH1+ or move
  them to Oz/POz. Scalp electrodes are dry; reference (SRB1, mastoid) and bias are gel pads.
  Connecting takes about 16 s.
- **Classifiers:** CCA and FBCCA work (calibration-free, resting baseline subtracted, `margin`
  abstains). PSD, TRCA, eTRCA, itCCA and eCCA are empty stubs.
- **Live decoder:** `online/decoder.py` `SlidingDecoder`: 4 s window every 0.5 s, filtered 1–100 Hz
  with a 60 Hz notch, commits a target after 3 wins in a row. `Decision` has `scores` per target,
  `raw`, `committed`, `changed`. `calibrate_baseline()` records 10 s of still screen.
  `reset()` clears the buffer and vote streak. On fake data the first commit comes at exactly 5.0 s
  and a switch shows after about 2.5–3 s. The newest ~0.3 s of each window is bent by the
  zero-phase filter's edge (known, not fixed).
- **Live run (2026-10-22, 90 s):** 173 windows, 16 commits, 3–5 s per switch. Scores were
  ±0.02–0.09 against a 0.02 margin, so the decoder abstained more often than it committed. Not saved
  or scored; the teammate's verdict was "it worked i think".
- **Live demo:** `online/demo.py`: baseline, then launches the display with `--free --feedback
  stdin` as a subprocess; each commit is written to its stdin (red box) and spoken with macOS
  `say`. No trials, no websocket. The bridge can follow the same pattern.
- **Display:** `stimulus/` (PsychoPy): frame-locked sine/square flicker, refresh check that refuses
  to run on a mismatch, photodiode patch, LSL or Unix-time markers, cued / free / sweep modes, `Esc`
  quits. In free mode (`--feedback stdin`), stdin takes one integer per line: a target index (red
  box) or `-1` (clear); end of input stops the run. Non-integer and out-of-range lines are ignored
  with a note on stderr, so JSON lines (section 13, item 2) are harmless to an unmodified display.
  A box change logs no marker; the bridge must time-stamp it. Labels are fixed per config.
  `two_targets.toml`: 15 and 17 Hz, sine, squares 0.4 screen heights, centres one screen height
  apart. That spacing was never recorded: the 14/15 result used centres 0.7 apart. Always full
  screen (the teammate's rule: a small window is easy to look away from).
- **Findings:** 15 vs 17 Hz 83 % (4 s windows); 12/15/17 three-class 66 %; 10 Hz unusable (alpha);
  gaze drifts to the other target after about 2 s unless a cue box stays on the target; ProMotion
  on = no SSVEP; drawing text every frame can cost frames on slow machines. Re-analysed on
  2026-10-23 with stricter run selection: section 14. The held cue box fixed gaze drift in cued
  recordings (last 2 s: 7/20 → 13/20), but live use cannot hold a box on a target nobody knows in
  advance; a fixation mark inside each square, or the 2 s window, has to do that job. Gel or paste
  on O1/O2 is the biggest remaining gain, ahead of any software change. One verified run dropped 7
  frames in 160 s (the baseline for SYNC 1.5).

## 13. Open decisions for SYNC 1

**Open questions for the teammate:** listed at the top of `INTERFACE-v2-draft.md` ("Open
questions for the teammate"), with a note asking the teammate's Claude session to raise them.

**Elijah's decisions (2026-10-23), for the teammate to confirm at SYNC 1.** Already written into
`INTERFACE-v2-draft.md`; the server is being built to match.

- **Drop v1.** After SYNC 1 the server speaks only protocol 2; the fake decoder and tests move to v2
  in the same change.
- **Handshake error → pause.** A `hello` that fails the check gets `error`, and the session stays
  paused (no automatic keyboard fallback). The operator fixes the bridge or restarts with
  `--keyboard`.
- **Refresh or waveform mismatch is an error.** A `hello` whose `refresh_hz` is more than 1 Hz from
  `display.expected_refresh_hz` (for example 120 Hz with ProMotion on) or whose `waveform` differs
  from `stim.waveform` is rejected like a slot mismatch: nothing flickers, and the partner page shows
  why.
- **No calibration mode.** `mode`, `cued_slot`, `calibration_done` and the calibration rule are
  removed; the teammate records labelled data with his own `cued_trials` tool.
- **`board_ok: false` → badge only.** The partner page shows it; the session keeps running.
  (`display_ok: false` still pauses, because the flicker is then off.)
- **`tools/contract_check.py` is a stand-in server.** It listens on `:8000/ws/decoder`, runs a
  handshake, a baseline and a few trials against the bridge, checks every message against the v2
  schemas and the rules, and prints pass/fail per message. Passing it is the SYNC 2 gate.

1. **Flicker: settled.** His PsychoPy display draws the targets; the browser draws no flicker.
2. **Getting the option text onto his display.** *Recommended:* extend his free-run feedback so
   stdin also takes JSON lines next to the existing target index: `{"text": "..."}` (one line of
   text, for list mode and the confirm screen), `{"labels": ["...", "...", "...", "More options"]}`
   (one sentence per target, beside each square, for page mode), `{"active": [0, 3]}` (which
   squares flicker; the confirm screen uses two) and `{"flicker": false}` (hold frame 0 during
   cue, rest and baseline). Roughly 40 lines in `stimulus/feedback.py` and `display.py`. The display
   prints the Unix time of the first flickering frame after `{"flicker": true}` (`--markers unix`),
   and the bridge sends it to us. *Fallback:* his display in a window (`--windowed`) with our
   partner page's mirror beside it, so the user reads the text in the browser; no change to his
   display. This breaks the teammate's always-full-screen rule and weakens the response, so it is
   a last resort.
3. **The bridge** (`ssvep/online/bridge.py`, his repo, about 150 lines). Launches the display as a
   subprocess the way `demo.py` does, connects to `/ws/decoder` and sends `hello` with the slots
   and refresh rate from his config. Relays our display commands to the display's stdin. On
   `baseline` it holds the flicker off and calls `calibrate_baseline`. On `stim_start` it turns the
   flicker on, calls `decoder.reset()`, drops buffered samples and steps the decoder; on the first
   `committed` it sends `selection` with the slot. On `stim_stop` it stops stepping. Reports display
   `Esc` or exit as a `status` with `ok: false`. Reconnects to our server every 1 s; the board
   itself takes about 16 s to connect, so it connects once at startup, not per reconnect. `--fake`
   uses `FakeSource`.
4. **Menu style. Decided (Elijah, 2026-10-24): page mode**, 3 options + NEXT on 4 targets, with
   a confirm step before anything is spoken, so the user sees all options at once. Fallback: list
   mode (all options visible, moving highlight, SAY / NEXT on 2 targets) if the 4-target gate
   fails (section 14). Needs per-target text and a 4-square layout on his display.
5. **`INTERFACE.md` changes.** Flicker ownership moves to his side, so these are more than additive;
   bump `protocol` to 2. Written out in full in `INTERFACE-v2-draft.md` (adds `hello_ack`, `error`,
   `baseline_done` and `pause` to the list below); once agreed it replaces `INTERFACE.md`:
   - The decoder's `hello` carries `refresh_hz`, `waveform` and `slots` from his config; the server
     replies with an ack and the roles. (v1 had it the other way round.)
   - New server → decoder `display` {text, labels, highlight}, relayed to the display.
   - New server → decoder `baseline` {seconds}: hold the flicker off and record the resting
     baseline.
   - `stim_start` from the server means "start flickering now"; the decoder answers with
     `stim_onset` {trial_id, t_unix} from the display's first flickering frame.
   - `stim_stop` turns the flicker off; `reason` gains `no_selection`.
   - Section 6 table filled in: 4 targets (s1–s3 options, s4 NEXT), 13 / 15 / 17 / 11 Hz, sine,
     rest 1 s, max trial 10 s, baseline 10 s; `stim_start.active_slots` can be 2 of them (confirm
     screen). Fallback: 2 targets, 15 / 17 Hz. (Done in the draft, 2026-10-24.)
6. **Scores.** His `Decision.scores` exist already: send `scores` (and `score` for the chosen slot)
   with each `selection`, and `"score_kind": "fbcca_minus_baseline"` in `hello`. Not probabilities;
   `confirm_mode: low_score` would use a margin threshold he picks.
7. **Window length.** 4 s or 2 s. In the three cued 15/17 recordings the best 2 s cell was 13/14,
   17/19 and 12/17, against 10/14, 14/19 and 14/17 at 3.86 s (section 14). Shorter windows make
   each selection about 3 s instead of 5 s. *Recommended:* 2 s, if a live check agrees.
8. **Frequency set.** 13/15/17/11 Hz for page mode (his `four_diamond.toml`), NEXT on 11 Hz; 15/17 for the list-mode
   fallback. Settled by the 4-target gate (section 14).
9. **Where the decoder lives.** `../ssvep` (his repo) or the `brainsync/` rebuild on branch
   `check-implemtation-calhacks-desktop`. The bridge (item 3) has to be written against one of them.
10. **The teammate's demo-improvement plan** (`../ssvep/.claude/plans/demo-improvements.plan.md`,
    phases A–E, waiting for the teammate's go as of 2026-10-23). It overlaps this plan:
    - **A** (save and score each demo run, replay source) and **B** (tune window, votes and margin
      on replays of the three cued recordings) should set our `window_s` (item 7), `max_trial_s`
      and the margin. B's own risk note: windows of 2 s or less let alpha leak into 12–17 Hz scores.
    - **C** (TRCA, calibrated on the user's own trials) only if it beats tuned FBCCA.
    - **D** (confidence bars) extends the same display stdin as item 2: agree one line format.
    - **E** ("make the choice do something") is this app; skip it on their side.
    *Recommended order for them:* the 4-target gate recording (item 11) first, then A and B, then
    items 2 and 3 here, then D.
11. **4-target gate and a trained classifier (added 2026-10-24).** CCA/FBCCA need no training, so
    more data does not improve them; only window, votes and margin can be tuned. A classifier
    trained on the user's own trials (TRCA / eTRCA, stubs in his repo, his plan C) usually beats
    CCA clearly with more targets and short windows, which is this case. Plan:
    - Fix the hardware first: gel or paste on O1/O2, C3/C4 tied to CH1+ or moved to Oz/POz. A
      trained classifier learns noise if the signal is poor.
    - The gate recording (section 14) doubles as TRCA training data.
    - TRCA is about 100 lines of numpy; Elijah can write it as a PR to his repo if the teammate is
      on the bridge. Judge it only on held-out trials, never on its training trials.
    - Recalibrate every session (dry electrodes move each time the headset goes on): about
      6–8 cued trials per target, 2–3 minutes, with `cued_trials`.
    - *Stretch:* every confirmed "Say it" is a labelled trial; log its EEG window with the label
      and retrain between turns, so accuracy can improve during the demo.

## 14. Frequency choice (re-analysis 2026-10-23)

Source: `python -m brainsync.select_freqs` and `python -m brainsync.score_cued` (desktop worktree)
on the recordings in `/Users/school/Documents/ssvep/data/spectrum_checks`. Runs recorded with
ProMotion on (120 Hz) and the eyes-closed control are left out.

Single target, one frequency per run (CCA against controls at f0 ± 3 Hz):

| Hz | Runs usable / eligible | Verdict |
| --- | --- | --- |
| 10 | 0 / 1 | Rejected: the still screen also wins (alpha at rest) |
| 12 | 1 / 3 | Weak. Two runs phase-lock to the flicker but lose the CCA test to a 9 Hz resting rhythm |
| 15 | 2 / 2 | Usable |
| 17 | 1 / 1 | Strongest: 15/17 trials, 13 dB SNR. But only one run |

Pairs, 2-class CCA with a still-window baseline, one ~3.9 s window per trial:

| Pair | All eligible runs | Only runs that passed (optimistic) | Original 3 runs |
| --- | --- | --- | --- |
| 15/17 | **78 %** (n=50) | 78 % | 86 % |
| 12/17 | 75 % (n=73), 82 % balanced | **86 %** (n=35) | 77 % |
| 12/15 | 65 % | 80 % | 64 % |
| 12/15/17 | 61 % | 72 % | 66 % |

Both squares flickering (cued 15/17, cue box held, FBCCA / CCA, 14–19 clean trials each):
64–79 % at 3.86 s, 71–93 % at 2 s, with 30–70 % of decisions abstained at the default margin.

How 15/17 was picked (teammate's session, 2026-10-22), one 4 s window per trial:

| Pair | Accuracy | Still screen favours |
| --- | --- | --- |
| 15/17 | 83 % | neither (51 %) |
| 12/15 | 71 % | 12, 78 % raw |
| 12/17 | 70 % | 12, 80 % raw |

The 12 Hz template gets a head start from the background that the still-screen baseline only
partly removes. Frequency was never the cause of the early failures: every run with no response
was recorded with ProMotion on.

Guidance from the same sessions, for 3 or 4 targets later:

- 11–17 Hz is a plateau: each frequency is expected within 1 dB of 15 Hz (literature response
  curve over the teammate's measured noise floor). Below 9 Hz and above 20 Hz lose clearly.
- Avoid harmonic clashes such as 7.5/15 and 10/20.
- Candidate four-target sets: 12/15/17/20 (the first plan; 20 Hz never measured, though it was
  the most phase-locked response seen, as the 10 Hz flicker's harmonic), or 11/13/15/17 with sine
  flicker (no shared harmonics below 45 Hz).

What this means:

- **15/17 stays the default.** It is the best pair on the unbiased pool, and the only pair tested
  with both squares flickering at once, which is how the demo runs.
- **12/17 is the only alternative.** It looks better only when runs are picked by their own outcome,
  or by balanced accuracy on lopsided classes (56 vs 17 trials). 12 Hz sits next to the user's
  9–10 Hz alpha: before the baseline, the lower template wins about 80 % of still-screen windows.
  The upside is 5 Hz between targets instead of 2 Hz.
- **Nothing else has data.** 20 Hz (exactly 3 frames at 60 Hz) was never recorded.
- **The numbers are noisy.** With 14–19 trials per cued recording, a still-window control reaches
  79 % in one cell (label-permutation p = 0.18). Gaps under about 10 points are not evidence.
- **Safety does not change.** 12, 15 and 17 Hz are all in the photosensitive range (rule 10).

**4-target gate (decided 2026-10-24; teammate, as early as possible, before the display work).**
One cued recording with all 4 squares flickering at 13/15/17/11 Hz in the real page-mode layout
(diamond, fixation marks, text beside each square), using `cued_trials -c` with his
`four_diamond.toml`: about 6–8 trials per target, roughly 5 minutes, headset settled for 20
minutes first. Score it with FBCCA at 2 s and 4 s windows. Chance is 25 %.

| 4-class accuracy | Decision |
| --- | --- |
| About 70 % or more | Keep page mode (4 targets) |
| 55–70 % | Train TRCA on the same recording (section 13, item 11), test on held-out trials; keep page mode if it reaches 70 % |
| Below that | List mode: options visible with a moving highlight, SAY / NEXT on 15/17 Hz |

**Changed 2026-10-24 to 13/15/17/11 Hz**, the teammate's `four_diamond.toml`: 15 and 17 Hz keep
their places from `two_targets.toml` (left and right), 13 Hz goes on top and 11 Hz at the bottom,
so only one pair of neighbouring squares is 2 Hz apart. All four sit clear of the user's
eyes-closed alpha (9.25 Hz) and of the 18–21 Hz interference he measured at the venue, which
rules out 20 Hz; with sine flicker they share no harmonics below 45 Hz. NEXT stays at the bottom
on the lowest frequency, nearest alpha, so its errors turn a page instead of picking an option.
(The first plan was 12/15/17/20 for wider spacing and the strong 20 Hz response.)

The 12/17 two-target test above is now only relevant to the list-mode fallback: switch from 15/17
only if it beats it by more than 10 points. Any switch is a config change on both sides: his
stimulus config, and `stim.slots` here.
