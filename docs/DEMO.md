# BCI Voice: demo runbook

The operator's script for the Cal Hacks demo (`PLAN.md` V6 items 2-3). The partner talks, Claude
writes 3 reply options, the user picks one with their eyes on the flicker display, confirms it,
and the laptop speaks it.

**Safety first (PLAN rule 10).** The display flickers at 11-17 Hz, mostly in the range most likely
to trigger photosensitive seizures, with up to 4 squares at once. Nobody with photosensitive
epilepsy or flicker sensitivity uses it or stands facing it. Say so before the first trial, and
keep the audience out of the user's line of sight to the display where you can. `Esc` on the
display is the primary stop: it is local and immediate (it also closes the display). **Stop** on
the partner page (or `Esc` there) does the same through the server and the bridge, one hop
slower.

## 1. Setup checklist

- [ ] **Demo Mac** (the teammate's: the Cerelog X8's BrainFlow ships only macOS libraries).
- [ ] **ProMotion off, fixed 60 Hz** on the panel the flicker runs on (System Settings > Displays >
      Refresh rate: 60 Hz). With ProMotion on there is no SSVEP at all, and our server rejects any
      `hello` more than 1 Hz from 60 Hz.
- [ ] **Partner page on its own screen:** an external monitor for Chrome with PsychoPy on the
      built-in panel, or the partner's phone (set `server.host: 0.0.0.0` in
      `config/default.yaml`, open `http://<mac-ip>:8000`). Chrome allows the microphone only on
      https or localhost, so on the phone expect speech recognition to be blocked: test it first,
      and plan to type the partner's lines there. The flicker is always full screen, so it cannot
      share a screen with Chrome.
- [ ] **Lapel mic on the partner**, set as the macOS default input (System Settings > Sound >
      Input). Chrome uses the default input; plug it in before opening the page.
- [ ] **Speaker** set as the default output, loud enough for the room. A Bluetooth speaker lags:
      raise `speech.stt_tail_s` to about 0.8 so the mic never hears the reply.
- [ ] **Chrome** (the Web Speech API needs it, and needs internet). Allow the microphone when asked.
- [ ] **Keys in `.env`** at the repo root: `ANTHROPIC_API_KEY` (Claude replies) and
      `CARTESIA_API_KEY` (voice). Never show the file on screen.
- [ ] **Venue Wi-Fi check:** congested Wi-Fi made every Claude page time out on 2026-10-24
      (1-4 s ping, 3-9 s TLS setup). Preflight (section 3) tests it; a phone hotspot is the fix.
- [ ] **Fixed-list clips on disk:** `python tools/prerender_tts.py` while the network is good.
      Then the fixed pages (shown whenever Claude is slow) speak in the Cartesia voice even with
      the Wi-Fi down. Run it again after changing the voice, model or `config/intents.yaml`.
- [ ] Repo on the demo branch, `pip install -e ".[dev]"` done in our venv; the teammate's venv
      for `../ssvep`.

## 2. Start

Our venv, in the repo root:

```
python -m app.server                    # the real bridge connects to /ws/decoder
```

It prints which voice (`Cartesia sonic-3.5` or `browser speechSynthesis only`), which replies
(`Claude claude-haiku-5-5` or `fixed lists only`) and which profile it uses, then the partner
page URL. The demo script (section 5) assumes `profile: the demo persona`; if it says
`your profile (profiles/user.yaml)`, a saved profile is in use. Open `http://127.0.0.1:8000` in
Chrome, and leave the flicker warning up until the demo starts (section 5).

The teammate's venv, in `../ssvep`, once the bridge exists (it connects the board once, about
16 s, then opens the display full screen):

```
python -m ssvep.online.bridge           # headset + PsychoPy display
python -m ssvep.online.bridge --fake    # FakeSource instead of the headset
```

Fallbacks, instead of the bridge:

```
python -m app.server --fake-decoder     # starts tools/fake_decoder.py --random: picks at random
python -m app.server --keyboard         # keys 1-4 on the partner page select (no headset)
```

Both show a FAKE DECODER or KEYBOARD badge on the partner page. Say so out loud if you use them.
The fake decoder is for rehearsing only: its picks (and confirms) are random, so never present
them as the user's choices.

## 3. Preflight

With the server (and the bridge) running, in a second terminal:

```
python tools/preflight.py                         # checks http://127.0.0.1:8000
python tools/preflight.py --url http://127.0.0.1:8013   # a server on another port
python tools/preflight.py --skip-network          # no Claude / Cartesia test calls
```

It prints PASS / WARN / FAIL per check (SKIP when not run), with a `fix:` line under each one
that did not pass, a GO / NO-GO line, and the headset checklist (section 4) as a reminder. It
exits 0 only if nothing failed. `--json` prints one JSON object instead. What to do:

| Check | On FAIL or WARN |
| --- | --- |
| `server` | FAIL: start `python -m app.server` and check that `--url` matches the port it printed. "no /api/health" means an older server: restart it from this branch. |
| `decoder` | FAIL "no bridge connected": start the bridge and read its terminal. FAIL "not ok": the bridge's `hello` was rejected (slots, waveform or refresh rate; the reason is on the partner page): fix the bridge's config, not ours. WARN keyboard / fake decoder: accepted only if you say so in the pitch. Out of time: restart with `--keyboard`. |
| `display` | FAIL "display down": relaunch the display (restart the bridge), then **Resume**. A refresh-rate mismatch shows up as a rejected `hello` under `decoder`: ProMotion off, 60 Hz, restart the bridge. Never run the flicker at another rate. |
| `ANTHROPIC_API_KEY`, `claude`, `claude (server)` | FAIL: add the key to `.env` and restart the server; on a network error, see `net` below. To go without Claude, set `menus.dynamic: false` and restart: the pages are the fixed lists (`config/intents.yaml`, LLM OFF badge) and preflight skips these checks. WARN keep-alive: rerun in 20 s. |
| `CARTESIA_API_KEY`, `cartesia`, `voice (server)` | WARN: the browser voice speaks every reply. Add the key to `.env` and restart, or accept it. Clips already on disk still play. |
| `clips` | WARN: some fixed-list sentences have no clip on disk yet. Run `python tools/prerender_tts.py` (the server also fetches them at start, slowly on a busy network). |
| `net api.anthropic.com`, `net api.cartesia.ai` | FAIL unreachable / WARN congested: switch to a phone hotspot or a wired network. Without internet Chrome's speech recognition also stops: type the partner's lines. To accept the browser voice instead, set `speech.provider: browser` and restart (preflight then skips Cartesia). |
| `partner page` | FAIL: open the server's address in Chrome. Leave the flicker warning up: **I understand, start** starts the baseline and then the flicker. |
| `mic` | WARN before the session starts is normal (no report yet). Otherwise follow its `fix:` line (allow the mic, unmute, bring the page to the front); after **I understand, start**, check for the MIC ON badge. Check the macOS default input and output too. |

Go only on GO (no FAIL). A WARN is a fallback you have accepted: say which one in the pitch.

## 4. Headset checklist (PLAN V6 item 1)

- [ ] Headset on **at least 20 minutes** before recording or demoing; unsettled contacts ruined
      the first trials of the first two-target recording.
- [ ] One **eyes-closed alpha check** (teammate's venv, no flicker running; `-t 10` scores the
      10 Hz band, 30 s recording by default):
      `python -m ssvep.acquisition.spectrum_check -t 10 --names Fp1,Fp2,C3,C4,P7,P8,O1,O2`.
      Want a posterior 9-10 Hz peak and RMS under 20 µV on O1 and O2. No peak or a large DC
      offset: fix that contact (gel or paste on O1/O2) before going on.
- [ ] **Nothing else reads the board's serial port** (a live plot splits the stream). Close the
      spectrum check before starting the bridge.
- [ ] Allow **about 16 s** for the board to connect; the "Handshake verification failed" line it
      logs first is harmless.

## 5. Demo script

Before the first question: read the flicker warning aloud, press **I understand, start**, and wait
for the baseline (about 10 s of still screen; the user looks at the display and relaxes). The
first page is the fixed home list. From then on the squares flicker in every trial (up to 10 s on,
then about 2-3 s still) even before anyone speaks; press **Stop** between segments to rest the
user's eyes (**Resume** runs a new baseline).

The partner speaks into the lapel mic (or the operator types the line and presses Enter). Lines 1,
2 and 4 gave the options quoted (or a fitting option 1) with the demo persona (Sam,
`config/profile.yaml`) in today's logs; lines 3 and 5 are `tools/llm_eval.py` cases with no live
log yet. Claude's wording varies from run to run, so try every line once with the live setup
before the demo.

| # | Partner says | User does | Shows |
| --- | --- | --- | --- |
| 1 | "Hi, how are you doing today?" | Option 1 ("Pretty good, thanks! How about you?"), then **Say it** | pick + confirm + speech |
| 2 | "Would you like some coffee?" | Option 2 ("Not right now, thanks."), then **Back**; the same page returns. Then option 1 ("Yes, please. That would be great."), **Say it** | nothing is spoken without confirmation |
| 3 | "Do you want to go outside?" | Option 1, **Say it** | a yes/no question: a yes, a no and a later, likelier answer first |
| 4 | "How was your weekend?" | Option 1, **Say it** | a plain answer, no invented events |
| 5 | "What should we have for dinner tonight?" | **NEXT** (bottom square) whatever page 1 shows, then the best option on page 2, **Say it** | "More options": a new page of 3 from Claude (the fixed "more" list if Claude is late) |

Notes:

- The diamond: s1 top (option 1), s2 left (option 2), s3 right (option 3), s4 bottom (NEXT). On
  the confirm screen only s1 ("Say it") and s4 ("Back") flicker. In keyboard mode those are keys
  1-4 (confirm: 1 = Say it, 4 = Back).
- One NEXT only: NEXT twice in a row goes back to the home list.
- After each spoken reply the operator marks it on the partner page: **C** (correct: it was what
  the user meant) or **W** (wrong), or the Correct / Wrong buttons. The keys work only outside the
  text box. These marks are the accuracy numbers in section 8.
- A spoken question can interrupt a page trial in progress (it is recognised by its first word,
  since speech-to-text adds no "?"), at most once per 20 s (`speech.stt_interrupt_cooldown_s`); a
  remark waits for the next cue. A spoken line never interrupts the confirm screen. Typed text
  always interrupts at once, even the confirm screen (that pick is dropped and nothing is said).
- **Optional, the profile page:** open `http://127.0.0.1:8000/profile.html` in a separate Chrome
  window, not a tab (Chrome stops the partner page's mic while its tab is hidden). In "Try it",
  type "Do you want to go outside?" and press **Preview replies** (it previews the form as it is,
  unsaved), then switch Length to Chatty and Saying no to Direct and preview again: same question,
  a different voice. **Save** applies it to the live session (saved to `profiles/user.yaml`);
  **Use the demo persona** deletes that file and puts Sam back. Put Sam back before the next run.

## 6. What the audience sees, and what to say

- **The partner page mirror** (left): the same 4 boxes and sentences as the user's display, no
  flicker, with the trial phase and the picked box highlighted. Point at it, not at the flicker.
- **Badges** (right): DECODER OK (NO DECODER / DECODER NOT OK), DISPLAY OK (DISPLAY OFF), BOARD OK
  (BOARD ?), **LLM OK** (Claude wrote the last page; LLM OFF = fixed lists), **MIC ON** (listening
  to the partner; MIC MUTED, or a red MIC: DENIED / NOMIC / OFFLINE ... if Chrome cannot listen),
  FAKE DECODER / KEYBOARD when a fallback is on. In keyboard mode without a bridge, NO DECODER,
  DISPLAY OFF and BOARD ? are expected.
- **The conversation** panel: each partner line and each spoken reply.

**While waiting (PLAN design risk 4).** One reply is cue 1 s + decode at least 5 s + highlight,
then the confirm step (another 5 s or more), then the speech: about 13-14 s at best. Each NEXT
adds about 7 s, and "Didn't catch that" means the decoder abstained and the trial repeats. Say
something like: "The decoder needs a few seconds of steady looking before it commits, and it asks
again rather than guess. The confirm step costs five seconds but means a wrong pick is never said
out loud. Good ranking matters most: when option 1 is right, a reply is two looks."

**The honest claim (PLAN pitch claim, V7 item 1).** "An LLM that writes context-aware replies
turns a few-target SSVEP BCI on a dry-electrode headset into a conversation, with every option
visible and nothing spoken without a confirmation." Then state what was actually used and
measured, filled in before the demo:

- target count and mode: ___ targets, page mode (3 options + NEXT) / list mode (SAY / NEXT);
- page mode: accuracy from the 4-target gate recording, ___ % over ___ cued trials (chance 25 %),
  offline. Until that recording is scored there is no 4-target number: say "not measured yet";
- list mode only (15/17 Hz): offline re-analysis, 78 % over 50 trials with one target flickering
  per run, and in three cued recordings with both squares flickering 64-79 % at 3.86 s windows and
  71-93 % at 2 s (14-19 trials each, 30-70 % of decisions abstained) (`PLAN.md` section 14). Never quote these for 4 targets;
- from the live log (section 8): ___ % marked correct over ___ marked replies, ___ s per spoken
  reply.

Never claim more targets, accuracy or speed than these, and say which numbers are offline and
which are live. "Dry-electrode" holds only if O1/O2 had no gel or paste (the reference and bias are
gel pads either way): otherwise say what was used. If the run used `--keyboard` or
`--fake-decoder`, say so.

## 7. Fallbacks during the demo

| Problem | Do this |
| --- | --- |
| Bridge drops out | The session pauses ("Decoder disconnected"). When the bridge reconnects and its `hello` passes, the session resumes by itself with a new baseline, so the flicker comes back without a Resume. **Stop** pressed while it is paused does not cancel this: if the user needs a break, press `Esc` on the display once it is back. |
| Decoder or bridge cannot be fixed | Restart the server with `python -m app.server --keyboard`: keys 1-4 select (confirm: 1 = Say it, 4 = Back), C / W mark replies. Say it is keyboard mode. |
| Speech recognition fails (red MIC badge, offline, no mic) | Type the partner's line in the box and press Enter. **Restart mic** retries. |
| Cartesia fails or is slow | Nothing to do: the browser voice speaks that reply. Before the demo, `speech.provider: browser` in `config/default.yaml` (then restart the server) makes it browser-only. |
| Claude fails or is slow (LLM OFF, or options lag) | Nothing to do: after `llm.timeout_s` (4 s) the fixed list appears (yes / no / maybe after a question, otherwise home). `menus.dynamic: false` (then restart the server) makes it fixed lists only. |
| Flicker must stop now | `Esc` on the display (local, immediate), or **Stop** / `Esc` on the partner page. After Stop or `Esc` the server never restarts the flicker by itself: **Resume** runs a new baseline first. |
| Someone feels unwell | Stop at once; end the demo. |
| Display closed (`Esc`) | The session pauses (DISPLAY OFF). Once the bridge reports the display back, press **Resume**. |

## 8. After the demo

```
python tools/log_summary.py                              # the newest session in logs/
python tools/log_summary.py logs/2026-10-24_100110_a80fa8.jsonl   # one or more log files
python tools/log_summary.py --all                        # every session, then a total
python tools/log_summary.py --json                       # the numbers as JSON
python tools/log_summary.py --show-text                  # also the partner lines and spoken replies
```

It reports trials, the correct / wrong rate from the C / W marks, seconds and selections per
spoken reply, how often the spoken reply was option 1, the median and p95 Claude page latency,
Cartesia clips and warnings worth acting on (counts only unless `--show-text`). Exit status 1
means a log could not be read, 2 that there was no log.
Put these numbers in the README and the Devpost write-up (V7). Logs contain the conversation:
keep `--show-text` output off shared screens.
