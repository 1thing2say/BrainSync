# BCI Voice partner app (iOS)

A native iPhone app that replaces the partner page in Safari. It connects to the BrainSync
server's `/ws/ui` socket like the web page does (`PLAN.md` section 6) and does the same jobs: it
mirrors the user's screen (never flickering), plays the spoken replies, listens to the partner
hands-free, takes typed lines, and has a big Stop. The server owns all state (PLAN rule 6).

Why native: on-device speech recognition (works offline, nothing sent to Google), echo
cancellation while listening, no tab to keep visible, and the screen stays awake while connected.

```
ios/
  BrainSyncPartner.xcodeproj   the app project (Xcode 16+)
  BrainSyncPartner/            app sources (SwiftUI) and Assets.xcassets
  BrainSyncPartner-Info.plist  mic / speech / local network texts, the brainsync:// scheme
  BrainSyncKit/                Swift package: links, messages, the socket, ListenCore and the
                               setup order (tested)
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
   - `python -m app.server --tunnel` (add `--fake-decoder` or `--keyboard` as usual): a
     Cloudflare `https://….trycloudflare.com` address that works from any network.
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
written from `app/schemas.py`, so a change on the server side shows up there.
