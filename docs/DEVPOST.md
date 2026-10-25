# BCI Voice: Devpost write-up (draft)

Draft for the Cal Hacks 13.0 submission (`PLAN.md` V7). Numbers marked **TBD** come from
`python -m brainsync.score_cued` (the 4-target gate recording) and `python tools/log_summary.py`
(the live sessions); fill them in, or leave the sentence out, before submitting. Never replace a
TBD with a number from another setup. The same placeholders are in the README's "Honest claim".

## Inspiration

People who cannot speak or move their hands, after ALS, a stroke or a spinal injury, often talk
through eye-gaze boards or a helper reading letters aloud. Both are slow, and a brain-computer
interface on a cheap dry-electrode headset is slower still if it spells letter by letter: it can
only tell a few targets apart, and it needs seconds of steady looking for each pick. We asked what
happens if the interface never spells at all. In a conversation, the partner has just said
something, so the likely replies are few and predictable. A language model can write them; the
brain only has to choose one.

## What it does

A conversation partner talks to the user (or types). BCI Voice listens, and Claude writes three
short replies in the user's own voice, ranked most likely first. The user's screen shows them as
four squares in a diamond, each flickering at its own rate (13, 15, 17 and 11 Hz), each option's
full sentence beside its square, and the fourth square for "More options". The user looks at a
reply. The visual cortex follows that square's flicker, an 8-channel EEG headset picks it up, and
the decoder works out which square it was. A confirm screen follows: "Say it" or "Back". Only
"Say it" speaks, in a natural Cartesia voice, and the conversation moves on.

- **Every option is visible at once.** The user never picks something only because the better
  answer was on a page they could not see.
- **Nothing is spoken without a confirmation.** A wrong pick costs one "Back", never a sentence
  said out loud.
- **The language model never chooses.** Only the decoder (or, as a labelled fallback, a
  keyboard) decides what was picked; Claude only writes the options.
- **The partner sees what the user sees:** a partner page in Chrome, or a native iPhone app,
  mirrors the user's screen without the flicker, shows the conversation, and has a Stop button
  that turns the flicker off at once.

## How we built it

- **EEG and decoder.** A Cerelog X8 board (8 channels at 250 Hz, dry scalp electrodes, gel
  reference and bias) over BrainFlow. The decoder is training-free: canonical correlation
  analysis and its filter-bank version (CCA / FBCCA) against sine templates, minus a resting
  baseline recorded on a still screen. Live, it scores a 1.5 s window every 0.25 s and commits
  after 4 agreeing windows in a row; it abstains unless the best target clearly leads the others
  and sits above its own resting level, because a confident wrong pick is worse than a slow one.
  We checked both classifiers on the public Nakanishi 2015 12-target benchmark (FBCCA 85 % at
  2 s, 94 % at 4 s, matching the published figures) before trusting them on our own data, and
  picked the live settings from a 576-setting replay grid over our cued recordings.
- **Choosing the frequencies.** Single-target and cued two-target recordings on the user decided
  which frequencies the EEG follows: 10 Hz sits on the user's alpha rhythm and is unusable, 12 Hz
  borrows a head start from it, 15 and 17 Hz work best. The four-target set (13/15/17/11 Hz,
  sine flicker) stays clear of the user's 9.25 Hz alpha, of the 18-21 Hz interference we measured
  at the venue, and of shared harmonics below 45 Hz. NEXT sits on 11 Hz, nearest alpha, so the
  likeliest error turns a page instead of picking a reply.
- **Display.** A full-screen PsychoPy display with frame-locked sine flicker, a refresh check
  that refuses to run at the wrong rate, a fixation mark at the centre of each square (gaze
  drifts off a plain square after about 2 s), and Unix-time markers on the flip of the first
  flickering frame. It takes commands on stdin: the sentences, which squares flicker, flicker on
  and off. `Esc` on it always stops the flicker, locally.
- **Bridge.** One process owns the board, the decoder and the display, and talks to the server
  over a WebSocket contract we wrote down first (`INTERFACE-v2-draft.md`): a handshake that checks
  the frequencies, waveform and refresh against the server's config, a baseline, then trials
  (`stim_start`, `stim_onset`, `selection`). It turns the flicker off the moment it loses the
  server. A stand-in server, `tools/contract_check.py`, checks any bridge against the contract.
- **Server.** FastAPI and an event-driven state machine that owns all state: what is on screen,
  when each trial starts and stops, the confirm step, what is spoken, and a JSON-lines log of every
  event. The shown options can never change during a trial, which the tests enforce. The state
  machine runs on an injectable clock, so its tests need no timers.
- **Ranked replies with Claude.** Claude Haiku (`claude-haiku-5-5`) writes each page with one
  forced, strict tool call whose schema is the ranked option list; the server validates every
  answer, drops repeats, keeps a spare candidate to fill a gap, sends a hedged second call when
  the first is slow, and falls back to a fixed list chosen by keyword ("How are you?" gets the
  feelings list) when Claude is late or down. A user profile and a personality map (formality,
  length, humour, directness) shape the wording; `tools/llm_eval.py` runs fixed partner lines
  through Claude and flags invented facts, long sentences and look-alike options.
- **Voice.** Cartesia's `sonic-3.5`, called over REST as soon as a page is shown, so each of the
  three clips is ready before the user picks. Clips are cached on disk, so the fixed lists speak
  in the same voice with the network down; the browser's local voice is the last fallback.
- **Partner page.** Plain HTML and JavaScript, no build step: Chrome's speech recognition listens
  to the partner hands-free (and stops while a reply plays, so it never hears itself), a spoken
  question can interrupt a page but never a pick in progress, and typing always works. A
  Cloudflare quick tunnel with a per-start access key puts it on a phone.
- **iPhone app.** A SwiftUI app that replaces the phone browser: on-device speech recognition with
  echo cancellation, the Cartesia clips with the iPhone's own voice as fallback, and the same Stop.
  Its message types are tested against a fixture generated from the server's schemas, so the two
  cannot drift apart silently.
- **Jetson.** A one-command installer (`deploy/jetson/install.sh`) sets up BrainSync's headset
  server (`brainsync.server`, which today serves the BrainPoint and reference-page clients, not
  yet BCI Voice) on an NVIDIA Jetson behind a Cloudflare tunnel, with BrainFlow built for Linux
  ARM, so the board could later live next to the user instead of on the laptop.

## Challenges we ran into

- **The screen lied about its refresh rate.** Our first four recording sessions showed no
  response at all. The MacBook's ProMotion panel was running near 120 Hz and changing rate by
  itself, so no square flickered at the frequency we asked for. The display now measures its
  refresh and refuses to run more than 1 Hz from 60 Hz, and so does the server's handshake.
- **The brain's own rhythms.** At rest the user's alpha (9-10 Hz) makes low templates win on a
  still screen, so every classifier subtracts a resting baseline, and the decoder also asks
  whether any target responds at all before it commits.
- **Dry electrodes are noisy.** Contacts take about 20 minutes to settle, two unconnected
  channels floated and upset the bias loop, and the live decoder abstains often. We chose to
  abstain and ask again ("Didn't catch that") rather than guess.
- **Four targets are harder than two.** Two-target accuracy was 78-86 % with one square
  flickering per run and 64-93 % with both flickering; three targets fell to 61-66 %. Four had never been recorded, so we built a gate: one cued four-target recording
  decides between page mode and a two-target list with a moving highlight. Result: **TBD**.
- **Venue Wi-Fi.** On the congested network a TLS handshake took 3-9 s, and every Claude page
  timed out against a 4 s budget. We keep the connection warm with a free call while idle, hedge
  slow calls, cache the voice clips on disk, and fall back to fixed lists, so the app keeps
  working with the internet down.
- **One screen cannot show both.** The flicker must be full screen, but the partner needs a
  visible page with Stop and the microphone. That is why the partner page moved to a second
  monitor or the partner's phone.
- **Safety.** Every frequency we use is in the photosensitive range. The flicker runs only during
  a trial and only on that trial's squares, and stops on `Esc`, on Stop, on a lost connection
  and on any error; the bridge also turns it off by itself if a trial runs 20 s without a stop.
  After Stop or `Esc` it never restarts by itself (Resume runs a new baseline first); after a
  dropped bridge it resumes only once the bridge reconnects and passes the handshake, with a
  new baseline.

## Accomplishments that we're proud of

- The server, the confirm step and the log run end to end against our bridge on synthetic EEG:
  it passes all 25 contract checks, and a 60 s session driven by a scripted partner page spoke
  two confirmed replies. Speech recognition and live Claude pages still wait for a run in Chrome
  on a good network (see "What is fake or untested").
- Measured, not assumed: frequency selection with controls for resting rhythms, the classifiers
  checked against a public benchmark, live settings tuned on replays, and still-screen controls
  that score near chance.
- A design where the language model can make a page better but can never put words in the user's
  mouth.
- Live numbers: **TBD** % of **TBD** replies marked correct, **TBD** s per spoken reply, the
  spoken reply was option 1 **TBD** % of the time, Claude pages in a median **TBD** ms (from
  `tools/log_summary.py`). Four-target accuracy, cued and offline: **TBD** % over **TBD** trials,
  chance 25 % (from `brainsync.score_cued`).

## What we learned

- Hardware first: a fixed 60 Hz refresh and good contact on O1 and O2 mattered more than any
  classifier change.
- With a few slow targets, ranking quality is the speed: when option 1 is right, a reply takes two
  looks.
- Write the contract between the two halves down early, and build a stand-in for each side; we
  could build and test the server long before a bridge existed.
- An honest "not measured yet" beats a borrowed number: two-target accuracy says nothing about
  four.

## What's next

- The 4-target gate recording and a live session on the headset, to fill in the numbers above.
- A classifier trained on the user's own trials (TRCA), recalibrated every session, and every
  confirmed "Say it" kept as a labelled trial so accuracy can improve while the user talks.
- Gel or paste on the occipital electrodes, and shorter windows if the replays agree.
- Voice banking: the user's own voice, with their consent.
- The decoder on the Jetson next to the headset, so any laptop or phone can be the screen.

## What is fake or untested (keep this in the submission)

- The bridge has run only on synthetic EEG and with a stand-in for the display window, never with
  the headset or the real PsychoPy window at the time of writing.
- The end-to-end bridge run used a scripted partner page: no speech recognition and no browser
  voice. Partner speech input still needs a manual check in Chrome, and on the venue Wi-Fi every
  Claude page timed out, so the fixed lists stood in.
- The four-target layout has never been recorded on EEG.
- The fake decoder's picks are random, and keyboard mode is a person pressing keys; both are
  labelled on screen whenever they are used, and any recording made with them says so.
- The iPhone app has not been built or run on a device yet; the Jetson installer has not run on a
  Jetson yet (its BrainFlow wheel was checked in an arm64 container only).
- "Dry-electrode" holds only if no gel or paste was used on O1 and O2.

## Built with

Python, FastAPI, uvicorn, pydantic, NumPy, SciPy, PsychoPy, BrainFlow (the Cerelog fork), the
Cerelog X8 EEG board, the Anthropic Claude API (Claude Haiku), Cartesia (`sonic-3.5`), the Web
Speech API, JavaScript, HTML/CSS, Swift and SwiftUI (Speech, AVFoundation), Cloudflare Tunnel,
NVIDIA Jetson, pytest.
