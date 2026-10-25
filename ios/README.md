# BrainSync on the iPhone (iOS)

A suite of small apps driven by c-VEP, plus Voice, the BCI Voice partner app.

- **Home, Camera, Reels** (the suite): the iPhone is the screen the user looks at. It draws
  two white squares, never more, showing the c-VEP code 31 frames apart
  (`docs/CVEP-STANDARD.md`), and `brainsync.server` (`PROTOCOL-v3.md`, code mode) reads the
  headset and says which square the user looked at. Each app gives the two squares its own
  meaning. See "The c-VEP suite" below.
- **Voice**: the partner app below, unchanged; it talks to `app.server` instead.

## The c-VEP suite

Two squares at most (lags 0 and 31 frames, the widest two allow), at the screen's edges:

| App | Left (t0, lag 0) | Right (t1, lag 31) |
| --- | --- | --- |
| Home | Open (the highlighted app) | Next (Camera, Reels, Voice, ...) |
| Camera, viewfinder | Shutter | More |
| Camera, More | Flip camera | Gallery (no photos: Back) |
| Camera, gallery | Next photo | More |
| Camera, gallery More | Delete (then Delete / Keep) | Back to camera |
| Reels | Like / pause (a paused reel: Play) | Next |

In Reels the squares are stacked on the right (Like on top). Home (top left) goes back by touch.

1. Start the headset server on the Mac or the Jetson: `python -m brainsync.server` (Cerelog
   X8; c-VEP is the default), or `python -m brainsync.server --board fake --no-auth` to try it
   without a headset (the Simulator reaches it at `http://127.0.0.1:8765/`). It prints a link,
   `http://…:8765/#token=…`; the Jetson's tunnel link is in `~/brainsync-link.txt`.
2. In the app, paste that link on Home and press Pair (or open a `brainsync://v3?url=…&token=…`
   link). A link to another server asks before it replaces the saved one.
3. Read the flicker warning and press **I understand, start**. The app asks for the camera now,
   so no permission prompt interrupts the flicker later, checks for 2.5 s that the screen runs
   at 60 Hz (the code shows one bit a frame; Low Power Mode or a hot phone can lower it), then
   connects.
4. Training: the left square has a blue box; look at it for about 32 s (30 code cycles). The
   status line shows `training: keep looking at …`, then `template ready: 83 % on held-out
   cycles (chance 50 %)`. The server keeps the template for 10 minutes, across apps.
5. Look at a square to pick it. Under each square: its confidence bar and score, `<` on the
   decoder's current best; the status line shows `lead … choice: …`. A pick rings the square
   yellow.

- **Stop** (top right) turns the flicker off at once and pauses the server; **Resume** brings it
  back (after a pause of 2 minutes or more the server trains again). Leaving the app, a lost
  connection, Voice and the Settings sheet also stop the flicker.
- A look still held on a square right after it opened an app does not count in the new app
  (2.5 s), so opening Camera never takes a photo by itself.
- With `--board fake`, tapping a square points the fake EEG at it (a dashed ring), so the whole
  loop can be tried by touch. With a headset, taps on the squares do nothing.
- Settings (Home): forget the server, dim the reel while choosing (on: moving pictures next to
  the squares weaken the response), copy photos to Photos, and the **Flicker lab** (the two squares
  with the measured refresh, late frames and cycle times, to check a phone's timing).
- Photos stay in the app (Application Support/Photos) unless the Photos copy is on.
- Simulator smoke run without taps (DEBUG builds; `SuiteDebugLaunch.swift`):
  `xcrun simctl launch booted org.calhacks.brainsync.partner -debugSuiteLink
  http://127.0.0.1:8765/ -debugSuiteAccept YES -debugSuiteApp reels -debugSuiteGaze t1`
  trains on the fake board, then scrolls Reels to the next reel every few seconds while the gaze holds.

Not yet done: a run with the headset on the phone (the c-VEP response to a phone-sized square,
and iOS frame timing on a device, are unmeasured).

## Voice: the BCI Voice partner app

A native iPhone app that replaces the partner page in Safari. It connects to the BrainSync
server's `/ws/ui` socket like the web page does (`PLAN.md` section 6) and does the same jobs: it
mirrors the user's screen (never flickering), plays the spoken replies, listens to the partner
hands-free, takes typed lines, and has a big Stop. The server owns all state (PLAN rule 6).

Why native: on-device speech recognition (works offline, nothing sent to Google), echo
cancellation while listening, no tab to keep visible, and the screen stays awake while connected.

```
ios/
  BrainSyncPartner.xcodeproj   the app project (Xcode 16+)
  BrainSyncPartner/            app sources (SwiftUI) and Assets.xcassets; the suite is
                               SuiteModel / SuiteViews, CodeFlicker (the 60 Hz squares),
                               Camera*, PhotoStore, Reel*
  BrainSyncPartner-Info.plist  camera / photos / mic / speech / local network texts, the
                               brainsync:// scheme
  BrainSyncKit/                Swift package: links, messages, the sockets (V3Connection for
                               brainsync.server, UIConnection for app.server), CodeVep and
                               FrameClock (the c-VEP code and frame timing), the suite's app
                               cores, ListenCore and the setup order (tested)
```

## Requirements

- A Mac with **Xcode 16 or newer**.
- An iPhone on **iOS 17 or newer**, and a USB cable.
- An Apple ID. A free one works: the app then installs for 7 days, after which you run it from
  Xcode again.

## Install on the phone

1. Open `ios/BrainSyncPartner.xcodeproj` in Xcode. It resolves the local package `BrainSyncKit`
   by itself.
2. Select the **BrainSyncPartner** target, tab **Signing & Capabilities**: pick your **Team**
   (add your Apple ID under Xcode > Settings > Accounts if it is not listed) and change the
   **Bundle Identifier** to something unique, for example `org.calhacks.brainsync.partner.yourname`
   (a free account cannot use one that someone else registered).
3. Plug the iPhone in, unlock it and tap **Trust**. Turn on **Developer Mode** on the phone
   (Settings > Privacy & Security > Developer Mode, then restart); the switch appears after Xcode
   has seen the phone once.
4. Choose the phone as the run destination at the top of Xcode and press **Run** (Cmd-R).
5. First run with a free account: the phone refuses to open the app until you trust the
   developer under Settings > General > VPN & Device Management.

## First run: the setup

The first time the app opens it walks through a short setup, one step per screen like Google's
Look to Speak, with a step indicator and a big **Continue**:

1. **Welcome:** what the app does (the user picks replies with their eyes on the laptop's flicker
   screen; this phone shows what they see, listens to the partner and plays the replies).
2. **Pair:** the pair form below. Skipped when the phone is already paired.
3. **Safety:** the full flicker warning, and "I have read this warning" before Continue.
4. **Microphone:** asks iOS for microphone and speech recognition access. It only asks: the mic
   opens when the server wants it, after the session starts.
   - **How the user picks** (only with a c-VEP server, see "Look to Speak on the phone" below):
     left or right card, Say it / Back, the 20 s calibration first, tap anywhere to stop.
5. **Practice:** an offline walk-through with sample replies (no server, nothing spoken): a
   question, three reply cards and More options, a pick, the Say it / Back screen, Speaking.
6. **Done:** "Start the session" accepts the warning for this run and sends resume, like the
   warning's "I understand, start".

Nothing before the last step starts the session. Once the phone is paired, a red **Stop** is on
every step, because the app may be opened while the laptop already flickers. Settings > Show setup
again runs it again.

## Pair with the server

The laptop runs the server; the app needs its address and access key.

1. Start the server in one of two ways:
   - `python -m app.server --tunnel` (add `--bridge cerelog`, `--bridge fake`, `--fake-decoder`
     or `--keyboard` as usual): a Cloudflare `https://….trycloudflare.com` address that works
     from any network.
   - `server.host: 0.0.0.0` in `config/default.yaml`, then `python -m app.server`: plain
     `http://<laptop-ip>:8000` on the same Wi-Fi.
2. Open the partner page on the laptop. It offers the **app link**
   (`brainsync://pair?url=…&key=…`) next to the phone link. AirDrop it to the phone, or send it in
   Messages and tap it: the app opens and connects.
3. Or paste a link in the app's pair screen: the app link, or the web phone link
   (`https://…/?key=…`), both work.

The app keeps the last server. The access key changes every time the server starts, so after a
restart the app shows **"Link expired — pair again"**: open the new app link. A link opened from
outside the app that points to another server than the saved one (a stranger's link, or a new
`trycloudflare.com` address, which changes every start) asks **"Pair with <host>?"** first;
only **Pair** switches. A new key for the same server, or a link pasted on the pair screen,
pairs at once.

## One partner page at a time

The server keeps one partner page. When the app connects, the laptop's page (or another phone)
shows "Open on another page" and stops; if the laptop page is opened again or "Use this page
instead" is pressed, the app shows **"Replaced by another partner page"** with a **Reconnect**
button that takes the session back. The laptop's page is still where the operator copies links
and edits the profile.

## Using it

- The **flicker warning** comes first, every time the app starts after the setup (PLAN rule 10).
  "I understand, start" starts the session and asks for the microphone and speech recognition.
  Resume stays off until the warning is accepted.
- The screen is **phrase first**: a slim status line (with the Settings gear), what the partner
  said last, then the user's screen as big cards: the three replies, each with a chip saying
  where its square sits on the user's diamond (Top / Left / Right / Bottom), then More options.
  A flickering target is only a steady lighter card and the picked one gets a yellow ring;
  nothing blinks. The confirm screen shows the sentence large with Say it and Back side by side;
  while a reply plays, one large Speaking card. In keyboard mode a tap on a card is its key.
- **Stop** is pinned at the bottom and always visible: it pauses the session and turns the
  flicker off. `Esc` on the flicker display does the same, and is the one to use when the app
  says it is not connected.
- The mic follows the server: on during the session, off while a reply plays (so the phone never
  hears itself), when paused, or muted. **Mute** is held by the server. **Restart mic** starts a
  new recognition session now.
- Typing always works (the text field under the conversation).
- The operator line at the bottom says "Ready" or what is wrong; tap it for the badges. Correct /
  Wrong mark the last spoken reply, like C / W on the laptop.
- **Settings** (the gear) stay on the phone: text size of the reply cards (standard, large,
  extra large; a larger iOS text size still wins), a vibration on a pick and when a reply is
  spoken, **Test the speaker** (the phone's own voice, only while the mic is off), the microphone
  permission with a link to iOS Settings, the server with **Pair again**, and **Show setup
  again**. Mute and the session stay on the main screen (the server holds them), and Stop is on
  the Settings sheet too.

## Look to Speak on the phone (c-VEP)

With `config/phone_cvep.yaml` the phone is the **user's** screen, like Google's Look to Speak, and
it draws the flicker itself (`INTERFACE-v2-draft.md` section 9):

```
python -m app.server --config config/phone_cvep.yaml --bridge cerelog --tunnel
```

Pair as above. Once the server sends its `stimulus` (the c-VEP code, on connect), the app shows
the Look to Speak screen instead of the partner screen; with the default config nothing changes.

- **Two cards**, left (s1) and right (s2), each with half of the phrases still in play ("More
  options" muted). The user looks at the card with the phrase they want; both cards flash one
  black/white code, the right one half a cycle behind, and the headset decodes which one the user
  watched. That half stays and is split again until one phrase is left; then **Say it** (left) /
  **Back** (right), with the phrase in the centre. Nothing is spoken without Say it. The picked
  card gets a white ring; the state line in the centre says "Waiting to start", why it is paused,
  "Didn't catch that" or the calibration score, and is empty while the user reads or looks.
- **60 Hz, never ProMotion.** The app locks its display link to exactly 60 Hz, measures it and
  reports it to the server (`display_report`). A phone that cannot hold 60 Hz (Low Power Mode, a
  hot phone) does not flicker, and the server pauses until it can. Settings shows the measured
  rate.
- **Calibration.** After the start and the still baseline, both cards flicker for about 20 s
  while the user looks at the left card (Look here); the bridge fits this user's template from
  it. The score shows on the state line (below 70 % it stays, as a warning). **Settings >
  Recalibrate** runs it again before the next page. Settings also shows the clock the phone uses
  to time-stamp its frames on the laptop's clock (offset and best round trip).
- **Stop.** A **tap anywhere** on the flickering screen turns it dark at once and sends Stop; the
  small red **Stop** at the bottom is always there, and **Resume** appears while paused. The
  flicker also stops when the connection drops or the app leaves the screen (in the background a
  running session is paused). Nothing flickers before the warning is accepted.
- **Partner** (bottom right) opens the partner tools over it: the conversation, typing, the mic,
  Correct / Wrong and Settings, with a still copy of the two cards. The phone does not flicker
  while they are open.
- The setup gains a step, **How the user picks**, and the practice round walks through the two
  cards (still, never flickering).

## Troubleshooting

- **Stuck on "Connecting…" on the LAN address:** iOS asks for **Local Network** access the first
  time; if it was refused, allow it in Settings > Privacy & Security > Local Network >
  BCI Partner. Check the phone is on the same Wi-Fi and that the laptop's firewall lets port 8000
  in. The tunnel needs no local network permission.
- **"Microphone blocked":** allow both Microphone and Speech Recognition for BCI Partner in
  Settings (Privacy & Security), then tap Restart mic. Typing works meanwhile.
- **"Speech recognition offline":** this iPhone has no on-device English model and Apple's
  servers are unreachable. On-device recognition needs iOS 17 with the English (US) Siri /
  dictation language downloaded (Settings > General > Keyboard > Enable Dictation, and
  Siri language English (United States)). The app retries by itself.
- **Replies are quiet:** the phone's ring/silent switch does not matter (the app plays through the
  speaker), but the media volume does. A Bluetooth speaker works (A2DP).
- **The app was in the background:** iOS takes the mic away from background apps. Coming back,
  the app reconnects and listens again by itself.

## If the project file will not open

The project is hand-written for Xcode 16 (folder-synchronized groups). If Xcode cannot open it:

1. Xcode > File > New > Project > iOS **App**, name `BrainSyncPartner`, interface SwiftUI,
   language Swift, saved in a temporary folder.
2. Delete the template's `ContentView.swift`, `BrainSyncPartnerApp.swift` and `Assets.xcassets`,
   then drag the files of `ios/BrainSyncPartner/` (the Swift files and `Assets.xcassets`) into
   the project, "Copy items if needed" off.
3. File > Add Package Dependencies… > Add Local… > choose `ios/BrainSyncKit`, and add the
   `BrainSyncKit` library to the app target.
4. Target settings: iOS Deployment Target 17.0, iPhone only, portrait only, Swift Language
   Version 5.
5. Copy the keys of `ios/BrainSyncPartner-Info.plist` into the target's Info tab:
   `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`,
   `NSLocalNetworkUsageDescription`, `NSAppTransportSecurity` > `NSAllowsLocalNetworking` = YES,
   and a URL Type with the scheme `brainsync`.

## Testing BrainSyncKit

The package builds on macOS and on Linux (`swift test` in `ios/BrainSyncKit`). Its tests decode
`Tests/BrainSyncKitTests/Fixtures/ui_messages.json`, every message the server sends and accepts,
written from `app/schemas.py`, so a change on the server side shows up there. The same holds for
the suite: `v3_messages.json` comes from `brainsync/server/protocol.py`
(`python tools/ios_v3_fixture.py`, checked by `tests/test_ios_v3_fixture.py`) and
`cvep_frames.json` from `brainsync.vep.live` and `clients/js/cvep.js`
(`python tools/ios_cvep_fixture.py`, needs `node`). Each tool's `--check` exits 1 when its
fixture is stale. With a server running, `BRAINSYNC_V3_LINK=http://127.0.0.1:8765/ swift test
--filter V3LiveServerTests` checks the real socket end to end.
