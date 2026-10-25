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
      built-in panel, or **the partner's phone**: the iPhone app (section 3, "iPhone app") or
      the web page (section 3, "On a phone"). The flicker is always
      full screen, so it cannot share a screen with Chrome.
- [ ] **Lapel mic on the partner**, set as the macOS default input (System Settings > Sound >
      Input). Chrome uses the default input; plug it in before opening the page.
- [ ] **Speaker** set as the default output, loud enough for the room. A Bluetooth speaker lags:
      raise `speech.stt_tail_s` to about 0.8 so the mic never hears the reply.
- [ ] **Chrome** (the Web Speech API needs it, and needs internet). Allow the microphone when asked.
- [ ] **Keys in `.env`** at the repo root: `ANTHROPIC_API_KEY` (Claude replies) and
      `CARTESIA_API_KEY` (voice). Never show the file on screen.
- [ ] **Venue Wi-Fi check:** congested Wi-Fi made every Claude page time out on 2026-10-24
      (1-4 s ping, 3-9 s TLS setup). Preflight (section 4) tests it; a phone hotspot is the fix.
- [ ] **Fixed-list clips on disk:** `python tools/prerender_tts.py` while the network is good.
      Then the fixed pages (shown whenever Claude is slow) speak in the Cartesia voice even with
      the Wi-Fi down. Run it again after changing the voice, model or `config/intents.yaml`.
- [ ] **iPhone app installed** (if the partner uses it): built from `ios/BrainSyncPartner.xcodeproj`
      on the demo Mac (`ios/README.md`). A free Apple ID install expires after 7 days: run it from
      Xcode again on the day. Microphone and Speech Recognition allowed for BCI Partner.
- [ ] Repo on the demo branch, one venv for everything: `.venv/bin/python -m pip install -e
      ".[dev,display]"` (PsychoPy comes with `display`) plus the Cerelog fork of BrainFlow, as in
      the README's Setup. The commands below write `python` for `.venv/bin/python`.
- [ ] **The one-time checks in section 2 passed** on this Mac, with this headset.

## 2. On the Mac, before the demo

Each check once, on the demo Mac, before the day's rehearsals. Write the numbers down: the
honest claim (section 7) and the README need them.

- [ ] **PsychoPy installed.** `python -c "import psychopy; print(psychopy.__version__)"`.
      Passing: a version (2026.2 or newer), no traceback.
- [ ] **Refresh check at 60 Hz, ProMotion off.** System Settings > Displays > Refresh rate:
      60 Hz, then `python -m brainsync.stimulus -c four_diamond --free --seconds 10 --no-wait`.
      Passing: four squares in a diamond, a small mark at the centre of each, and
      `measured refresh: 60.00 Hz` (anything within 1 Hz of 60) on stderr. A refused rate exits
      with code 2 and prints the rate it measured: fix the display, never `--ignore-refresh` for
      the demo.
- [ ] **SYNC 1.5: frame drops with text on screen.** Two 60 s runs of the page-mode screen
      flickering, the first without sentences, the second with one beside every square, as the
      bridge sends them. Each prints one timing line on stdout at the end:

      ```
      { echo '{"active": [0, 1, 2, 3]}'; echo '{"flicker": true}'; sleep 60; } \
        | python -m brainsync.stimulus -c four_diamond --free --feedback stdin --hold-still --no-wait
      { echo '{"labels": ["Yes, I would like to go outside now.", "Not right now, maybe later.", "Could you help me with something, please?", "More options"]}'; \
        echo '{"active": [0, 1, 2, 3]}'; echo '{"flicker": true}'; sleep 60; } \
        | python -m brainsync.stimulus -c four_diamond --free --feedback stdin --hold-still --no-wait
      ```

      Passing: the second run's `... frames, N dropped overall` is no worse than the first's
      (for scale: one verified two-target run dropped 7 frames in 160 s). If the text costs
      frames, set `labels_during_flicker = false` in
      `brainsync/stimulus/configs/four_diamond.toml`: the sentences then show during the cue and
      the rest and hide while the squares flicker. During the second run, also check by eye
      that each sentence reads as its own square's: the top square's sentence sits to its
      right, above the right square, and the bottom one's to its left, under the left square.
      If a viewer pairs a sentence with the wrong square, move the corner boxes toward their
      own squares in `text_boxes` (`brainsync/stimulus/display.py`) before the gate recording.
- [ ] **The 4-target gate recording** (`PLAN.md` section 14). Headset on for 20 minutes first
      (section 5). Preview, record, score:

      ```
      python -m brainsync.stimulus -c four_diamond --plan --seed 1   # 32 trials, 4 min 16 s
      python -m brainsync.record -c four_diamond --trials 32         # headset + display, 8 per target
      python -m brainsync.score_cued <the saved file>_cued.npz --windows 2
      ```

      `record` opens the display itself (Space starts it, `Esc` stops it), cues each trial with
      the box held on its square, and saves `<date>_<time>_cued.npz` under
      `$BRAINSYNC_DATA_DIR/spectrum_checks` (or `--save-dir`); it prints how many trials were
      complete. `score_cued` scores CCA and FBCCA at 2 s and at the recorded 3.86 s window, with
      the still windows as a control. Passing: the still control near chance (25 %), and the
      best cell's accuracy and p read off for the decision:

      | 4-class accuracy | Decision |
      | --- | --- |
      | About 70 % or more | Keep page mode (4 targets) |
      | 55-70 % | Train TRCA on the same recording (`PLAN.md` section 13, item 11), test on held-out trials; keep page mode if it reaches 70 % |
      | Below that | List mode (`menus.mode: list`): options visible with a moving highlight, SAY / NEXT on 15/17 Hz |

      The cued session draws the diamond and the fixation marks, but not the option sentences
      beside the squares; the live screen has them (SYNC 1.5 above checks what they cost in
      frames, not in accuracy).
- [ ] **10 live trials with C / W marks** (`PLAN.md` V5). `python -m app.server --bridge cerelog`
      (section 3), run the demo script (section 6), and mark every spoken reply **C** or **W** on
      the partner page. Then `python tools/log_summary.py`. Passing: the session line says
      `decoder brainsync-fbcca` (not `fake decoder` or `keyboard mode`), at least 10 marked
      replies, and the correct rate, seconds per reply, option-1 rate and Claude latency
      printed; these are the live numbers in section 7.
- [ ] **The iPhone app** (only if the partner uses it). Build and run it from Xcode on this Mac
      (`ios/README.md`), then `(cd ios/BrainSyncKit && swift test)` and
      `python tools/ui_fixture.py --check`. Passing: the app pairs with the server's app link and
      shows the mirror, the Swift tests pass, and the fixture check exits 0 (not stale).
- [ ] **The Jetson** (only if it is shown; it runs `brainsync.server`, the BrainPoint / reference
      page server, not BCI Voice). On the Jetson: `sudo /opt/brainsync/deploy/jetson/install.sh`
      (`deploy/jetson/README.md`). Passing: it prints `done: brainsync-tunnel is running with
      --board cerelog` (notes may follow) and a link in `~/brainsync-link.txt` that held during
      its stability check (a new link after every restart).
- [ ] **The screen recording and two rehearsals** (`PLAN.md` V7). Record one full live run
      (screen and room audio: the partner's line, the pick, the confirm, the spoken reply); a
      keyboard-mode backup recording must say so on screen. Then rehearse the whole demo script
      twice with the teammate, from a cold start (section 3) to the log summary (section 9).
      Passing: both rehearsals reach every row of the script without a restart, or the fallback
      used is one you will name in the pitch.

## 3. Start

In the repo root, one command starts the whole system: our server, and the bridge with the
headset and the decoder.

```
python -m app.server --bridge cerelog   # server + brainsync.voice_bridge (Cerelog X8 + decoder)
```

**The page in Chrome is the user's screen and draws the flicker** (`display.flicker: browser`,
the default since 2026-10-25): each answer card flickers at its slot's frequency, the bridge
runs without its PsychoPy window (`--no-stimulus`, added by the server). With
`display.flicker: psychopy` the bridge's PsychoPy window flickers instead and the page is a
still mirror (the steps for that window are in `INTERFACE-v2-draft.md` section 7).

The server prints which voice (`Cartesia sonic-3.5` or `browser speechSynthesis only`), which
replies (`Claude claude-haiku-5-5` or `fixed lists only`) and which profile it uses, then the page
URL. The demo script (section 6) assumes `profile: the demo persona`; if it says
`your profile (profiles/user.yaml)`, a saved profile is in use.

1. Open `http://127.0.0.1:8000` in Chrome on the Mac's built-in panel (fixed 60 Hz, ProMotion
   off), full screen (Cmd-Ctrl-F), nothing else open. The flicker warning measures the screen
   for a second: an orange line ("runs at 120 Hz", "frames are uneven") means fix the display
   first; never flicker at another rate.
2. The bridge (its log lines in the same terminal under `voice_bridge`) connects the board once,
   about 16 s (the "Handshake verification failed" line is harmless), then sends `hello`.
3. **I understand, start** starts a fresh session (empty conversation, the home page): a 10 s
   still baseline, then the first page. (`README.md` phase 3 recommends 30 s for the tuned 1.5 s
   window; with 10 s the rest spread is noisier, so live accuracy may suffer until
   `baseline.seconds` is raised, an agreed value to settle with the teammate.)

What the user's screen shows: four answer cards in a diamond, each against its own edge of the
screen (top, left, right; More options at the bottom). Only during a trial do they flicker, each
at its own frequency (13, 15, 17 and 11 Hz); the text stays white with a black outline. The
confirm screen shows the picked sentence in the still centre, Say it at the top and Back at the
bottom, and only those two flicker. Esc, Stop, or leaving the tab turns the flicker off at once;
closing or reloading the page pauses the session (a new page flickers only after its own start
button).

Rehearsal without the headset:

```
python -m app.server --bridge fake      # fake EEG; the page flickers (display.flicker: browser)
```

The fake bridge's name starts with "fake", so preflight and the log say fake decoder (the page
itself shows no badge). Its picks are a random active square per trial (a selection about 4.3 s after the onset),
so never present them as the user's choices.

To run the bridge in its own terminal (to read its log apart, or restart it while the server
keeps running), start the server without `--bridge` and then:

```
python -m brainsync.voice_bridge --no-stimulus          # headset; the page draws the flicker
python -m brainsync.voice_bridge --fake --no-stimulus   # fake EEG
python -m brainsync.voice_bridge                        # headset + PsychoPy window (display.flicker: psychopy)
```

A restarted bridge connects the board again (about 16 s); the server pauses meanwhile and
resumes with a new baseline once its `hello` passes. If the teammate's own bridge exists and
passes `tools/contract_check.py`, it can take this one's place the same way.

Fallbacks, instead of the bridge:

```
python -m app.server --fake-decoder     # starts tools/fake_decoder.py --random: picks at random
python -m app.server --keyboard         # keys 1-4 on the partner page select (no headset)
```

Both show a FAKE DECODER or KEYBOARD badge on the partner page. Say so out loud if you use them.
The fake decoder is for rehearsing only: its picks (and confirms) are random, so never present
them as the user's choices. `--bridge` cannot be combined with `--fake-decoder`: both connect to
`/ws/decoder`.

### On a phone (iPhone Safari or Android Chrome)

```
python -m app.server --tunnel           # add --fake-decoder / --keyboard as above
```

`--tunnel` starts a Cloudflare quick tunnel (`cloudflared`, no account: `brew install cloudflared`)
and prints a **phone link**, `https://….trycloudflare.com/?key=…`, in the terminal. Send it to the
phone (AirDrop, Messages) and open it in Safari. Then:

- The phone link is new every start and carries a secret key: share it only with the partner's
  phone. Without the key the address answers 403; the decoder socket never works through it.
- The phone takes over the session from the laptop's page (only one partner page at a time):
  the laptop shows "Open on another page". The phone speaks the replies now: turn its volume up,
  and keep it off silent mode (the ring/silent switch mutes web audio on an iPhone).
- Tap **I understand, start** on the phone: that tap lets Safari play the replies, and asks for
  the microphone. Allow it. The screen stays on while the page is open.
- iPhone speech recognition stops more often than Chrome's: if the mic card says "Tap to start
  the mic", tap anywhere. Typing always works.
- "Add to Home Screen" from Safari's share menu opens it full screen next time (same link).
- It needs internet on both ends; on a slow network the tunnel adds a little delay to each
  message, not to the decoding (the bridge stays local).

### iPhone app

The native app (`ios/`) replaces the Safari page: on-device speech recognition, echo cancellation,
Cartesia clips with the iPhone's own voice as fallback, no tab to keep in front. Install it once
from Xcode (`ios/README.md`). Start the server in one of two ways:

```
python -m app.server --tunnel           # https through Cloudflare, any network
python -m app.server                    # with server.host: 0.0.0.0, plain http on the same Wi-Fi
```

The server prints an **iPhone app link**, `brainsync://pair?url=…&key=…`, next to the phone
link in its terminal, and the laptop's partner page shows only **Copy iPhone app link** (the
phone link is in the terminal). Then:

- AirDrop or message the app link to the iPhone and tap it: the app opens and connects. Pasting
  the app link or the web phone link into the app's pair screen works too.
- The key changes every server start: after a restart the app says the link expired; send the new
  one. On the LAN address, allow **Local Network** the first time iOS asks.
- First launch: the app walks through its setup (pair, flicker warning, microphone, a short
  offline practice). **Start the session** on the last step starts it, like **I understand,
  start**. Do the setup before the demo; **Stop the flicker** is on every step once paired.
- Later launches: tap **I understand, start** and allow the microphone and speech recognition if
  asked. Turn the media volume up (the silent switch does not matter here). Text size, the
  speaker test and **Pair again** are in Settings (the gear).
- **The app is the partner page while it is connected.** The laptop's page shows "Open on another
  page"; preflight's `partner page` line says "connected (iPhone app)". Keep the laptop page open
  anyway for links, preflight and the profile page; do not press "Use this page instead" there
  unless you mean to move the session back.
- **Stop** is pinned at the bottom of the app. `Esc` on the display still stops the flicker
  locally, and is the one to use if the app shows it is not connected.
- If the app fails (does not install, cannot connect, mic blocked): open the phone link in Safari
  (section above), or press **Use this page instead** on the laptop page and run the demo from
  Chrome. Typing works in all three.

## 4. Preflight

With the server (and the bridge) running, in a second terminal:

```
python tools/preflight.py                         # checks http://127.0.0.1:8000
python tools/preflight.py --url http://127.0.0.1:8013   # a server on another port
python tools/preflight.py --skip-network          # no Claude / Cartesia test calls
```

It prints PASS / WARN / FAIL per check (SKIP when not run), with a `fix:` line under each one
that did not pass, a GO / NO-GO line, and the headset checklist (section 5) as a reminder. It
exits 0 only if nothing failed. `--json` prints one JSON object instead. What to do:

| Check | On FAIL or WARN |
| --- | --- |
| `server` | FAIL: start `python -m app.server` and check that `--url` matches the port it printed. "no /api/health" means an older server: restart it from this branch. |
| `decoder` | FAIL "no bridge connected": start the bridge and read its terminal. FAIL "not ok": the bridge's `hello` was rejected (slots, waveform or refresh rate; the reason is on the partner page): fix the bridge's config, not ours. WARN keyboard / fake decoder: accepted only if you say so in the pitch. Out of time: restart with `--keyboard`. |
| `display` | FAIL "display down": the bridge relaunches the display by itself after `Esc` or a crash; press Space on the new display, then **Resume**. If it stays down, restart the bridge. A refresh more than 1 Hz from 60 Hz stops the display at start, and the bridge exits with code 1 (its terminal says why); a bridge run with `--ignore-refresh` gets its `hello` rejected under `decoder` instead. Either way: ProMotion off, 60 Hz, restart the bridge. Never run the flicker at another rate. |
| `ANTHROPIC_API_KEY`, `claude`, `claude (server)` | FAIL: add the key to `.env` and restart the server; on a network error, see `net` below. To go without Claude, set `menus.dynamic: false` and restart: the pages are the fixed lists (`config/intents.yaml`, LLM OFF badge) and preflight skips these checks. WARN keep-alive: rerun in 20 s. |
| `CARTESIA_API_KEY`, `cartesia`, `voice (server)` | WARN: the browser voice speaks every reply. Add the key to `.env` and restart, or accept it. Clips already on disk still play. |
| `clips` | WARN: some fixed-list sentences have no clip on disk yet. Run `python tools/prerender_tts.py` (the server also fetches them at start, slowly on a busy network). |
| `net api.anthropic.com`, `net api.cartesia.ai` | FAIL unreachable / WARN congested: switch to a phone hotspot or a wired network. Without internet Chrome's speech recognition also stops: type the partner's lines. To accept the browser voice instead, set `speech.provider: browser` and restart (preflight then skips Cartesia). |
| `partner page` | FAIL: open the server's address in Chrome (or connect the iPhone app; PASS then says "connected (iPhone app)"). Leave the flicker warning up: **I understand, start** starts the baseline and then the flicker. |
| `mic` | WARN before the session starts is normal (no report yet). Otherwise follow its `fix:` line (allow the mic, unmute, bring the page to the front); after **I understand, start**, check for the MIC ON badge. Check the macOS default input and output too. |

Go only on GO (no FAIL). A WARN is a fallback you have accepted: say which one in the pitch.

## 5. Headset checklist (PLAN V6 item 1)

- [ ] Headset on **at least 20 minutes** before recording or demoing; unsettled contacts ruined
      the first trials of the first two-target recording.
- [ ] One **eyes-closed alpha check** (no flicker running, eyes closed for the whole recording;
      `--target 10` scores the 10 Hz band, 30 s after a 5 s warm-up):
      `python -m brainsync.check --target 10 --names Fp1,Fp2,C3,C4,P7,P8,O1,O2`
      (or the teammate's `python -m ssvep.acquisition.spectrum_check -t 10 --names ...` in his
      venv). Want a posterior 9-10 Hz peak and RMS under 20 µV on O1 and O2. No peak or a large DC
      offset: fix that contact (gel or paste on O1/O2) before going on.
- [ ] **Nothing else reads the board's serial port** (a live plot splits the stream). Close the
      electrode check, the brain map and any other BrainSync tool before starting the bridge.
- [ ] Allow **about 16 s** for the board to connect; the "Handshake verification failed" line it
      logs first is harmless.

## 6. Demo script

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
- After each spoken reply the operator marks it with a key on the laptop page: **C** (correct: it
  was what the user meant) or **W** (wrong). The page has no buttons for it. These marks are the
  accuracy numbers in section 9.
- A spoken question can interrupt a page trial in progress (it is recognised by its first word,
  since speech-to-text adds no "?"), at most once per 20 s (`speech.stt_interrupt_cooldown_s`); a
  remark waits for the next cue. A spoken line never interrupts the confirm screen. Text typed in
  the iPhone app always interrupts at once, even the confirm screen (that pick is dropped and nothing is said).
- **Optional, the profile page:** open `http://127.0.0.1:8000/profile.html` in a separate Chrome
  window, not a tab (Chrome stops the partner page's mic while its tab is hidden). In "Try it",
  type "Do you want to go outside?" and press **Preview replies** (it previews the form as it is,
  unsaved), then switch Length to Chatty and Saying no to Direct and preview again: same question,
  a different voice. **Save** applies it to the live session (saved to `profiles/user.yaml`);
  **Use the demo persona** deletes that file and puts Sam back. Put Sam back before the next run.

## 7. What the audience sees, and what to say

- **The partner page mirror** (left): the same 4 boxes and sentences as the user's display, no
  flicker, with the trial phase and the picked box highlighted. Point at it, not at the flicker.
- **Badges** (right): DECODER OK (NO DECODER / DECODER NOT OK), DISPLAY OK (DISPLAY OFF), BOARD OK
  (BOARD ?), **LLM OK** (Claude wrote the last page; LLM OFF = fixed lists), **MIC ON** (listening
  to the partner; MIC MUTED, or a red MIC: DENIED / NOMIC / OFFLINE ... if Chrome cannot listen),
  FAKE DECODER / KEYBOARD when a fallback is on. In keyboard mode without a bridge, NO DECODER,
  DISPLAY OFF and BOARD ? are expected.
- **The conversation** panel: each partner line and each spoken reply.

**While waiting (PLAN design risk 4).** One reply is the reading time (a new page stays still
2–6 s, longer for longer sentences, so the user reads every option before anything flickers) +
decode + highlight, then the confirm step (1.5 s still, then decode again), then the speech. The bridge's decoder (1.5 s windows every
0.25 s, 4 agreeing windows in a row, starting fresh at each trial) committed about 4.3 s after
the onset on the fake board, so a reply takes about 12-14 s at best; the headset can only be
slower. Each NEXT adds one cue and one decode, and "Didn't catch that" means the decoder abstained and the trial repeats. Say
something like: "The decoder needs a few seconds of steady looking before it commits, and it asks
again rather than guess. The confirm step costs a few seconds but means a wrong pick is never said
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
- from the live log (section 9): ___ % marked correct over ___ marked replies, ___ s per spoken
  reply.

Never claim more targets, accuracy or speed than these, and say which numbers are offline and
which are live. "Dry-electrode" holds only if O1/O2 had no gel or paste (the reference and bias are
gel pads either way): otherwise say what was used. If the run used `--keyboard` or
`--fake-decoder`, say so.

## 8. Fallbacks during the demo

| Problem | Do this |
| --- | --- |
| Bridge drops out | The session pauses ("Decoder disconnected"). When the bridge reconnects and its `hello` passes, the session resumes by itself with a new baseline, so the flicker comes back without a Resume. **Stop** pressed while it is paused does not cancel this: if the user needs a break, press `Esc` on the display once it is back. |
| Bridge process gone (its terminal shows it exited; NO DECODER stays) | The server does not restart it. Start it by hand in a second terminal: `python -m brainsync.voice_bridge` (the server keeps running). Exit code 1 means the board or the display failed at start: read the line above it (board unplugged, port busy, refresh refused). |
| Decoder or bridge cannot be fixed | Restart the server with `python -m app.server --keyboard`: keys 1-4 select (confirm: 1 = Say it, 4 = Back), C / W mark replies. Say it is keyboard mode. |
| Speech recognition fails (red MIC badge, offline, no mic) | Type the partner's line in the box and press Enter. **Restart mic** retries. |
| iPhone app drops out or misbehaves | It reconnects by itself every second. If it stays down: the Safari phone link, or **Use this page instead** on the laptop page. A restarted server needs a new app link. |
| Cartesia fails or is slow | Nothing to do: the browser voice speaks that reply. Before the demo, `speech.provider: browser` in `config/default.yaml` (then restart the server) makes it browser-only. |
| Claude fails or is slow (LLM OFF, or options lag) | Nothing to do: after `llm.timeout_s` (4 s) the fixed list appears (yes / no / maybe after a question, otherwise home). `menus.dynamic: false` (then restart the server) makes it fixed lists only. |
| Flicker must stop now | `Esc` on the display (local, immediate), or **Stop** / `Esc` on the partner page. After Stop or `Esc` the server never restarts the flicker by itself: **Resume** runs a new baseline first. |
| Someone feels unwell | Stop at once; end the demo. |
| Display closed (`Esc`) | The session pauses (DISPLAY OFF). The bridge relaunches the display by itself, held still, with the last screen restored, and reports it back (DISPLAY OK). Press Space on the new display's start screen first, then **Resume**: a Resume before Space runs the baseline on the start screen and the next trials get no onset. |

## 9. After the demo

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
