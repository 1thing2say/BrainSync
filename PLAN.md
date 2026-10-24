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
**Laptop only for now**; phone support comes later (section 10).

At the start of each session, ask Elijah whether he wants code written or only the approach
explained. All work happens on `dev` (or another non-`main` branch); `main` changes only through
pull requests.

---

## Status (2026-10-23)

- Done on `dev`: V0 (schemas, config, event log, fake decoder) and V1 (partner page with screen
  mirror), plus the V2 server, scan-mode orchestrator and speech out. Try it with
  `python -m app.server --fake-decoder` and open `http://127.0.0.1:8000`.
- Read the teammate's `ssvep` code (section 12). Three facts shape this plan:
  1. **2 targets, not 4.** On his dry-electrode Cerelog X8, 15 vs 17 Hz decodes at 83 % on 4 s
     windows (FBCCA 14/15 at 2 s with the cue held); 3 targets (12/15/17) only reach 66 %; 10 Hz
     is unusable (alpha).
  2. **His decoder is continuous, not per trial.** `SlidingDecoder` classifies a 4 s window every
     0.5 s and commits after 3 wins in a row, so a selection takes about 5 s from the start of the
     flicker. It already produces per-target scores.
  3. **The flicker is his PsychoPy display** (`ssvep.stimulus`), already validated on his EEG.
     The browser draws no flicker. Nothing speaks `INTERFACE.md` yet, so a bridge on his side runs
     the decoder and the display and talks to our server.
- **Next up:** SYNC 1 to settle section 13 (teammate: please read sections 12 and 13), then V3
  (LLM option ranking) and V4 (partner speech input).

## 0. Event constraints

- Built at **Cal Hacks 13.0** (Oct 23–25, 2026), 36 hours, **2-person team**.
- Commit small and often with clear messages; organizers may review commit history.
- Time beats polish. Build the simplest version that passes each phase's acceptance check. Anything
  marked *stretch* waits until the demo works end to end.
- Never block on the decoder. Until it is ready, everything runs against `tools/fake_decoder.py`,
  which speaks the same contract.
- Feature freeze at **hour 28** from kickoff; after that only bug fixes, write-up and rehearsals.

## 1. What this side does

The decoder tells us which of **2** targets the user looked at. This side does everything else:

1. Decides what the user's screen shows: the current reply option in large text with two
   flickering targets, **SAY** (speak this option) and **NEXT** (show the next option). His PsychoPy
   display draws it; we send it the text and when to flicker, through the bridge.
2. Tells the decoder when each selection period (trial) starts and stops, and when the screen must
   be still for its baseline.
3. Receives the selected target; on SAY, speaks that option's sentence aloud.
4. Listens to the conversation partner (speech-to-text or typed) and asks the LLM for a **ranked**
   list of replies that fit what was just said, most likely first.
5. Runs the **partner page** in Chrome: conversation, typed input, push-to-talk, status, STOP,
   speech output, and a mirror of the user's screen (no flicker).

**Scan mode (default).** The LLM returns 4 options ranked by likelihood. The screen shows option 1.
SAY speaks it; NEXT moves to option 2, and so on. NEXT on the last option asks for new options
("More options"). With good ranking, most replies take one selection (about 5 s).

Demo story: a partner asks "Do you want to go outside?", the top option becomes "Yes, let's go
outside", the user looks at SAY, and the laptop speaks it.

**Pitch claim:** an LLM that ranks context-aware replies turns a 2-target SSVEP BCI, the most a
dry-electrode headset separates reliably, into a conversation. If the decoder sends scores
(section 13, item 6), low-confidence picks can be confirmed first.

## 2. Non-negotiable rules

1. **Only the decoder (or keyboard mode) decides what was selected.** The LLM never picks, guesses
   or changes a selection.
2. **Speak exactly the selected option's `utterance`.** No rewriting after selection. The sentence
   is on the user's screen during the cue, and during the flicker unless that drops frames
   (SYNC 1.5), before it can be spoken.
3. **The shown option never changes during a trial.** It changes only in `MENU_CUE`. Enforce with
   an assertion and a test.
4. **One target is always NEXT.** The LLM only fills the option list; it never controls navigation.
5. **Every LLM response is validated** (pydantic). On timeout, error or invalid output, use a
   fallback list from `config/intents.yaml`. The app must keep working with the LLM or internet down.
6. **The server owns all state.** His display only draws what it is told and reports onsets; the
   browser only renders, plays audio and reports events. This keeps the logic testable and makes a
   phone client possible later.
7. **Stimulus timing and layout belong to his display** (frame-locked, from frame counts).
   Frequencies, phases, waveform and sizes live in his stimulus config (`two_targets.toml` or a
   copy). Our config only names the slots and their roles, and is checked against what his `hello`
   reports.
8. **No hardware or decoder guessing.** Anything about the decoder's internals, window length or
   frequencies: ask, read `INTERFACE.md`, or read `../ssvep`.
9. **Secrets** only from environment variables (`ANTHROPIC_API_KEY`). Never commit them.
10. **Safety:** flicker warning before the first trial (partner page and his display's start
    screen), `Esc` in his display stops all flicker immediately, and the partner page's STOP does
    the same through the bridge. 15 and 17 Hz are in the 15–25 Hz range most likely to trigger
    photosensitive seizures: nobody with photosensitive epilepsy or flicker sensitivity uses it.
11. Ask before adding dependencies outside section 4. All tunable numbers live in
    `config/default.yaml`.

## 3. Repository layout

Two repos. The decoder and display live in `1thing2say/ssvep` (his); this repo is
`1thing2say/BrainSync`.

```
BrainSync/
  CLAUDE.md             # local only
  PLAN.md
  INTERFACE.md
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
    log.py              # JSONL event log
  ui/                   # partner page; no flicker
    index.html
    style.css
    app.js              # websocket client: conversation, status, STOP, mirror of the user's screen
    speech.js           # TTS (speechSynthesis) + STT (Web Speech API)
  tools/
    fake_decoder.py     # speaks INTERFACE.md: keyboard / random / scripted modes
    contract_check.py   # validates decoder messages against schemas
    preflight.py        # demo go/no-go checks
  logs/                 # gitignored
  tests/

../ssvep/               # teammate's repo, separate venv (Python 3.10+, numpy/scipy, PsychoPy)
  ssvep/online/decoder.py   # SlidingDecoder (what the bridge wraps)
  ssvep/online/bridge.py    # NEW: INTERFACE.md client; runs decoder + display (section 13, item 3)
  ssvep/stimulus/           # PsychoPy display; NEW: text and flicker on/off commands (item 2)
```

## 4. Stack

- Python 3.11+: `fastapi`, `uvicorn[standard]`, `websockets` (fake decoder client), `pydantic` v2,
  `pyyaml`, `anthropic`, `pytest`, `pytest-asyncio`, `httpx`. Separate venv from `../ssvep`; we
  never import his package.
- Flicker: his PsychoPy display (`python -m ssvep.stimulus`), in his venv.
- Browser: **Chrome** (needed for the Web Speech API), for the partner page only. Plain
  HTML/JS/CSS, no framework, no build step.
- TTS: browser `speechSynthesis` (works offline, zero setup). *Stretch:* a hosted voice.
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
| `server.host` / `server.port` | `127.0.0.1` / `8000` | `0.0.0.0` later for phone or Jetson |
| `stim.slots` | s1 15 Hz, s2 17 Hz | Mirrors his stimulus config; checked against the decoder's `hello` |
| `stim.roles` | s1 `say`, s2 `next` | Which slot does what |
| `stim.cue_s` | 1.0 | Option shown, no flicker, before each trial |
| `stim.max_trial_s` | 10.0 | Decoder needs about 5 s (4 s window + 2 × 0.5 s votes) to commit |
| `stim.rest_s` | 1.0 | Still screen after a trial |
| `baseline.seconds` | 10.0 | Still screen at start for the decoder's resting baseline |
| `menus.dynamic` | true | false = fallback lists only |
| `menus.option_count` | 4 | Ranked options per list |
| `menus.label_max_chars` | 18 | |
| `menus.label_max_words` | 3 | |
| `menus.utterance_max_words` | 14 | |
| `llm.model` | `claude-haiku-5-5` | |
| `llm.timeout_s` | 4.0 | One retry, then fallback |
| `context.history_turns` | 8 | Partner + user turns sent to the LLM |
| `speech.tts_rate` | 1.0 | |
| `speech.stt_mode` | `push_to_talk` | Mic is off while TTS is speaking |
| `speech.max_speak_s` | 15.0 | Move on if the page never reports `tts_done` |
| `selection.confirm_mode` | `never` | `never` / `always` / `low_score` (needs decoder scores) |
| `selection.chosen_highlight_s` | 0.6 | Show which target was picked |

Layout, sizes, waveform and the refresh check live in his stimulus config (`two_targets.toml`:
sine, 0.4 screen-height squares one screen height apart). The server supports any number of slots,
so 3 or 4 targets is a config change on both sides if the decoder improves.

## 6. Shared messages

The UI ↔ decoder contract is in **`INTERFACE.md`**; implement it in `app/schemas.py` exactly. Any
change to it must be agreed with the teammate and made in that file first. Proposed changes are
listed in section 13; until agreed, build against v1 and keep the proposals behind config flags.

Internal browser ↔ server messages (`/ws/ui`, JSON with a `type` field):

- Server → browser: `show_option` {list_id, index, count, label, utterance, slots[{slot, role,
  label}]}, `trial` {trial_id, phase: cue|flicker|rest|baseline}, `highlight` {slot},
  `speak` {text, utterance_id}, `state` {name, detail}, `transcript` {who, text},
  `status` {decoder_connected, decoder_ok, display_ok, llm_ok, fake_decoder, keyboard_mode}.
- Browser → server: `tts_done` {utterance_id}, `partner_text` {text, source: stt|typed},
  `key` {key}, `stop` {}, `resume` {}, `judgment` {utterance_id, correct}.

Times are Unix seconds. Flicker onsets come from his display (the flip of the first flickering
frame, `--markers unix`), relayed by the bridge. His live decoder does not epoch on onsets (it
slides over the stream), so onset accuracy matters for logging and his offline analysis, not for
live decoding.

## 7. State machine (`app/orchestrator.py`)

```
IDLE --(warning accepted)--> BASELINE (still screen, baseline.seconds) --> MENU_CUE
MENU_CUE --(cue_s, list ready)--> STIMULATING
STIMULATING --selection(say)-->  SELECTED --> SPEAKING --(tts_done)--> REST --> MENU_CUE (new list)
STIMULATING --selection(next)--> SELECTED --> REST --> MENU_CUE (next option; past the last: new list)
STIMULATING --no_selection/timeout--> NOT_CAUGHT ("Didn't catch that") --> REST --> MENU_CUE (same option)
STIMULATING --partner_text--> stop trial (reason context_changed) --> MENU_CUE (new list, option 1)
any state --STOP / display Esc / decoder lost--> PAUSED (flicker off) --(resume)--> BASELINE
```

- `MENU_CUE` waits for the next list up to `llm.timeout_s`, then uses a fallback list.
- `confirm_mode: always` adds `CONFIRMING` after a SAY: the same two targets become "Say it" /
  "Back".
- NEXT twice past the end of a list (two "More options" in a row) → home list.
- Each trial has a new `trial_id`. Selections with an old `trial_id` are logged and ignored.
- Implement as a class driven by events plus an injectable clock, so tests run without timers.

## 8. Phases (voice/UI track)

Hours are from kickoff. Integration checkpoints with the teammate are marked **SYNC**.

### V0 — Setup and contract (h0–2)

1. Scaffold `pyproject.toml`, `.gitignore` (`logs/`, `.env`), `pytest`, folder layout.
2. `app/schemas.py`: all `INTERFACE.md` v1 messages + internal messages, with round-trip tests.
   Accept optional `score`/`scores`. Model the section 13 proposals too, off by default.
3. `config/*.yaml` and a loader with validation (unique slot ids, exactly one `say` and one `next`
   role, positive timings).
4. `tools/fake_decoder.py`: connects to `/ws/decoder`, answers `stim_start` with a `selection` after a
   delay. Modes: `--keyboard` (type 1–2), `--random`, `--script file.yaml`, `--fail-rate 0.2`
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
2. Mirror of the user's screen: the current option text and the SAY / NEXT boxes, static (no
   flicker), with the trial phase and the picked box highlighted. Used for development without
   PsychoPy and so the audience can follow along.
3. Flicker warning in the partner page; the first trial waits until it is accepted.
4. Keyboard mode: keys 1–2 select a target (demo fallback if the decoder fails).

Done when: the mirror follows the server through 20 fake-decoder trials.

**Teammate, in parallel (h2–6):** display commands in his PsychoPy display (section 13, item 2)
and the bridge (item 3). Elijah can take either as a PR to his repo if he prefers.

**SYNC 1.5 (h5): text on the PsychoPy screen.** Drive his display with the new commands through
60 s of cue/flicker/rest cycles with changing text; his session log shows no more dropped frames
than plain `--free`. If drawing text during the flicker drops frames, show it only during the cue
and rest (his `labels_during_stim = false` rule exists for this reason).

### V2 — Orchestrator and speech out (h4–8)

1. `app/server.py`: FastAPI serving `ui/`, websocket endpoints `/ws/ui` and `/ws/decoder`, reconnect
   handling, one active UI and one active decoder.
2. `app/orchestrator.py`: section 7 state machine (scan mode) with a static list from `intents.yaml`;
   sends display commands (text, flicker on/off, highlight) to the decoder connection.
3. `ui/speech.js`: TTS with `speechSynthesis`; pick an English voice; report `tts_done`. Show the
   sentence on screen while speaking.
4. Log every event.

Tests: table-driven transition tests (say, next, next past the end, timeout, stale trial_id,
context change, STOP → baseline); `show_option` and display text are never sent while
`STIMULATING`.

Done when: with `fake_decoder.py --random`, the app runs 20 trials in a row unattended, speaking
each SAY, with no stuck states.

**SYNC 2 (h6):** his bridge connects to `/ws/decoder` (with `FakeSource` if the headset is not on),
shows our text on his display and completes 5 trials. `tools/contract_check.py` shows no schema
errors.

### V3 — LLM option ranking (h8–12)

1. `app/context.py`: last `history_turns` turns (partner and user), time of day, profile.
2. `app/menus.py`: one LLM call per list. Input: what the partner last said, recent turns, time,
   profile, allowed intents, utterances already shown (for "More options"). Output: exactly
   `option_count` options **ranked most likely first**, each `{label, utterance, intent_id}`.
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
2. Agree on final `max_trial_s`, `rest_s` and the decoder's `window_s` (4 s default; 2 s worked
   14/15 with the cue held, and would cut each selection to about 3 s).
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
   60 Hz, ProMotion off), Chrome partner page open, API key, internet, audio out, mic; prints
   go/no-go.
2. One command to start: `python -m app.server` (+ `--fake-decoder`, `--keyboard`).
3. Demo script: 3–4 partner questions where the right reply is option 1, plus one that needs NEXT.
   Rehearse with the live decoder.
4. Log summary: trials, correct/wrong rate from `C`/`W` marks, seconds per spoken reply, selections
   per spoken reply, median list latency.

**Feature freeze at h28.**

### V7 — Submission (h28–36)

1. `README.md`: what it is, honest claim (2 targets, dry electrodes, measured accuracy), how to run,
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

## 10. Later: phone and Jetson (not now)

- The partner page is already a web page served by the laptop, so a phone can join over the local
  network by setting `server.host: 0.0.0.0` and opening `http://<laptop-ip>:8000`.
- Planned split: the partner page on the partner's phone (mic, transcript, conversation), while the
  laptop runs his display for the user.
- The decoder could later run on the Jetson Nano and connect to `/ws/decoder` over the network; the
  contract does not change. The vendored BrainFlow would need Linux ARM builds first.

## 11. Commands

```
pip install -e ".[dev]"
pytest
python -m app.server                        # real decoder connects to /ws/decoder
python -m app.server --keyboard             # keys 1–2 select
python tools/fake_decoder.py --random       # in a second terminal
python tools/contract_check.py              # watch decoder messages for schema errors
python tools/preflight.py

# in ../ssvep (his venv), once he adds the bridge
python -m ssvep.online.bridge               # headset + PsychoPy display, connects to our server
python -m ssvep.online.bridge --fake        # FakeSource instead of the headset
```

Open `http://127.0.0.1:8000` in Chrome for the partner page; the bridge opens his display
fullscreen.

## 12. What the teammate's code does (`../ssvep`, read 2026-10-23)

- **Board:** `acquisition/board.py`, Cerelog X8, 8 channels at 250 Hz, `read() -> (X µV, t Unix)`.
  Montage used: Fp1, Fp2, C3, C4, P7, P8, O1, O2; decoding uses the posterior four.
- **Classifiers:** CCA and FBCCA work (calibration-free, resting baseline subtracted, `margin`
  abstains). PSD, TRCA, eTRCA, itCCA and eCCA are empty stubs.
- **Live decoder:** `online/decoder.py` `SlidingDecoder`: 4 s window every 0.5 s, filtered 1–100 Hz
  with a 60 Hz notch, commits a target after 3 wins in a row. `Decision` has `scores` per target,
  `raw`, `committed`, `changed`. `calibrate_baseline()` records 10 s of still screen.
  `reset()` clears the buffer and vote streak.
- **Live demo:** `online/demo.py`: baseline, then launches the display with `--free --feedback
  stdin` as a subprocess; each commit is written to its stdin (red box) and spoken with macOS
  `say`. No trials, no websocket. The bridge can follow the same pattern.
- **Display:** `stimulus/` (PsychoPy): frame-locked sine/square flicker, refresh check that refuses
  to run on a mismatch, photodiode patch, LSL or Unix-time markers, cued / free / sweep modes, `Esc`
  quits. In free mode, stdin takes only a target index (red box) or `-1`; labels are fixed per
  config. `two_targets.toml`: 15 and 17 Hz, sine, squares 0.4 screen heights, one screen height
  apart.
- **Findings:** 15 vs 17 Hz 83 % (4 s windows); 12/15/17 three-class 66 %; 10 Hz unusable (alpha);
  gaze drifts to the other target after about 2 s unless a cue box stays on the target; ProMotion
  on = no SSVEP; drawing text every frame can cost frames on slow machines.

## 13. Open decisions for SYNC 1

1. **Flicker: settled.** His PsychoPy display draws the targets; the browser draws no flicker.
2. **Getting the option text onto his display.** *Recommended:* extend his free-run feedback so
   stdin also takes JSON lines next to the existing target index: `{"text": "..."}` (option text
   above the targets), `{"labels": ["SAY", "NEXT"]}` and `{"flicker": false}` (hold frame 0 during
   cue, rest and baseline). Roughly 40 lines in `stimulus/feedback.py` and `display.py`. The display
   prints the Unix time of the first flickering frame after `{"flicker": true}` (`--markers unix`),
   and the bridge sends it to us. *Fallback:* his display in a window (`--windowed`) with our
   partner page's mirror beside it, so the user reads the text in the browser; no change to his
   display.
3. **The bridge** (`ssvep/online/bridge.py`, his repo, about 150 lines). Launches the display as a
   subprocess the way `demo.py` does, connects to `/ws/decoder` and sends `hello` with the slots
   and refresh rate from his config. Relays our display commands to the display's stdin. On
   `baseline` it holds the flicker off and calls `calibrate_baseline`. On `stim_start` it turns the
   flicker on, calls `decoder.reset()`, drops buffered samples and steps the decoder; on the first
   `committed` it sends `selection` with the slot. On `stim_stop` it stops stepping. Reports display
   `Esc` or exit as a `status` with `ok: false`. Reconnects every 1 s. `--fake` uses `FakeSource`.
4. **Menu style.** *Recommended: scan* (SAY / NEXT, section 1). *Alternative: pair*: both targets
   carry an option, the LLM's top two side by side, and looking at neither until `max_trial_s`
   shows the next pair. Faster when the answer is in the top two, but a false commit speaks
   something; scan only ever speaks after an explicit SAY. Pair needs per-target text on his
   display.
5. **`INTERFACE.md` changes.** Flicker ownership moves to his side, so these are more than additive;
   bump `protocol` to 2:
   - The decoder's `hello` carries `refresh_hz`, `waveform` and `slots` from his config; the server
     replies with an ack and the roles. (v1 had it the other way round.)
   - New server → decoder `display` {text, labels, highlight}, relayed to the display.
   - New server → decoder `baseline` {seconds}: hold the flicker off and record the resting
     baseline.
   - `stim_start` from the server means "start flickering now"; the decoder answers with
     `stim_onset` {trial_id, t_unix} from the display's first flickering frame.
   - `stim_stop` turns the flicker off; `reason` gains `no_selection`.
   - Section 6 table filled in: 2 targets, 15 / 17 Hz, sine, rest 1 s, max trial 10 s, baseline 10 s.
6. **Scores.** His `Decision.scores` exist already: send `scores` (and `score` for the chosen slot)
   with each `selection`, and `"score_kind": "fbcca_minus_baseline"` in `hello`. Not probabilities;
   `confirm_mode: low_score` would use a margin threshold he picks.
7. **Window length.** 4 s (83 %) or 2 s (14/15 with the cue held). Shorter windows make each
   selection about 3 s instead of 5 s.
