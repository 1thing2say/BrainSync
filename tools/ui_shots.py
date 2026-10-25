"""Screenshots of the partner page in its main states, for reviewing the UI (and a new palette).

Each state is the real page (ui/index.html, ui/style.css, ui/*.js) loaded from disk with a
stand-in WebSocket that plays a few server messages; no server, decoder or microphone is needed.
Headless Chrome takes one PNG per state into --out (default cache/ui_shots, gitignored).

    python tools/ui_shots.py                    # every state
    python tools/ui_shots.py page phone         # some of them
    python tools/ui_shots.py --list

Chrome is found on macOS, Linux and WSL (the Windows chrome.exe); --chrome or $CHROME overrides it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import REPO_ROOT  # noqa: E402

UI_DIR = REPO_ROOT / "ui"
DEFAULT_OUT = REPO_ROOT / "cache" / "ui_shots"
WAIT_MS = 3200  # virtual time before the shot: the selection timer reads 3 s

READY = {"type": "status", "decoder_connected": True, "decoder_ok": True, "display_ok": True,
         "board_ok": True, "llm_ok": True, "llm": "ok", "fake_decoder": False, "keyboard_mode": False,
         "mic": "listening", "mic_muted": False, "resume_ready": False,
         "decoder_name": "ssvep-fbcca", "decoder_message": ""}
LONG_TALK = [{"type": "transcript", "who": w, "text": t} for w, t in [
    ("partner", "Good morning Sam, did you sleep well?"), ("user", "Yes, I slept well."),
    ("partner", "Great. Your sister is coming by at noon."), ("user", "That's wonderful news."),
    ("partner", "She asked if you want her to bring anything."), ("user", "Could she bring some fruit?"),
    ("partner", "I'll tell her. Do you want to go outside before she comes?")]]
TALK = [{"type": "transcript", "who": "partner", "text": "Hi Sam, how are you feeling today?"},
        {"type": "transcript", "who": "user", "text": "I'm doing well, thank you."},
        {"type": "transcript", "who": "partner", "text": "Do you want to go outside?"}]
PAGE = {"type": "show_page", "list_id": "m3", "page": 2, "mode": "page", "highlight_index": 0, "text": "",
        "slots": [{"slot": "s1", "role": "option", "label": "Let's go", "utterance": "Yes, let's go outside."},
                  {"slot": "s2", "role": "option", "label": "Not now", "utterance": "Not right now, thanks."},
                  {"slot": "s3", "role": "option", "label": "After lunch", "utterance": "Maybe after lunch."},
                  {"slot": "s4", "role": "next", "label": "More options", "utterance": ""}]}
CONFIRM = {"type": "show_page", "list_id": "m3", "page": 2, "mode": "confirm", "highlight_index": 0,
           "text": "Yes, let's go outside.",
           "slots": [{"slot": "s1", "role": "say", "label": "Say it", "utterance": ""},
                     {"slot": "s4", "role": "back", "label": "Back", "utterance": ""}]}
LIST = {"type": "show_page", "list_id": "m4", "page": 1, "mode": "list", "highlight_index": 1,
        "text": "Not right now, thanks.",
        "options": [{"label": "Let's go", "utterance": "Yes, let's go outside."},
                    {"label": "Not now", "utterance": "Not right now, thanks."},
                    {"label": "After lunch", "utterance": "Maybe after lunch."}],
        "slots": [{"slot": "s1", "role": "say", "label": "SAY", "utterance": ""},
                  {"slot": "s2", "role": "next", "label": "NEXT", "utterance": ""}]}


def state(name: str, detail: str = "") -> dict:
    return {"type": "state", "name": name, "detail": detail}


def trial(phase: str) -> dict:
    return {"type": "trial", "trial_id": 5, "phase": phase}


# name -> (window width, height, server messages, mic card (state, words), open the health details)
STATES: dict[str, tuple[int, int, list[dict], tuple[str, str], bool]] = {
    "page": (1440, 900, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
             ("listening", ""), False),
    "page-laptop": (1366, 768, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
                    ("listening", "do you want to go"), False),
    "picked": (1440, 900, [READY, *TALK, PAGE, state("SELECTED"), trial("rest"),
                           {"type": "highlight", "slot": "s1"}], ("listening", ""), False),
    "confirm": (1440, 900, [READY, *TALK, CONFIRM, state("CONFIRMING"), trial("flicker")],
                ("listening", ""), False),
    "speaking": (1440, 900, [READY, *TALK, CONFIRM, state("SPEAKING", "Yes, let's go outside."),
                             trial("rest")], ("speaking", ""), False),
    "problems": (1440, 900, [{**READY, "decoder_connected": False, "decoder_ok": False,
                              "display_ok": False, "board_ok": False, "llm_ok": False, "llm": "failed",
                              "decoder_name": "", "mic": "denied", "resume_ready": True},
                             *TALK, PAGE, state("PAUSED", "Decoder disconnected")],
                 ("denied", ""), True),
    "fake": (1440, 900, [{**READY, "fake_decoder": True, "decoder_name": "fake-decoder"}, *TALK, PAGE,
                         state("STIMULATING"), trial("flicker")], ("muted", ""), False),
    "list": (1440, 900, [READY, *TALK, LIST, state("STIMULATING"), trial("flicker")],
             ("listening", ""), False),
    # A 1366 x 768 laptop once the browser's bars take their part: a long talk, a reply being
    # spoken and the phone links must still fit without a page scroll.
    "laptop-busy": (1366, 650, [READY, *LONG_TALK, CONFIRM, state("SPEAKING", "Yes, let's go outside."),
                                trial("rest")], ("speaking", ""), False),
    # A small laptop window: the diamond's side targets must stay inside their thirds of the mirror.
    "laptop-narrow": (1024, 768, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
                      ("listening", ""), False),
    # A tablet or narrow window (one column): Stop in the bar fixed at the bottom.
    "tablet": (800, 700, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
               ("listening", ""), False),
    # Headless Chrome on Windows lays out no narrower than about 510 px; 520 is still a phone
    # (the 600 px breakpoint in style.css).
    "phone": (520, 960, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
              ("listening", "do you want"), False),
    # Tall enough to show the mirror under the partner's part: the page as a list of phrase cards,
    # and the confirm screen's Say it and Back side by side.
    "phone-page": (520, 1400, [READY, *TALK, PAGE, state("STIMULATING"), trial("flicker")],
                   ("listening", ""), False),
    "phone-confirm": (520, 1300, [READY, *TALK, CONFIRM, state("CONFIRMING"), trial("flicker")],
                      ("listening", ""), False),
    # The flicker warning, as it opens before the first trial.
    "warning": (1440, 900, [READY, *TALK, PAGE, state("IDLE")], ("idle", ""), False),
}
# States that keep the flicker warning open (the others close it, as after the start tap).
WARNING_STATES = {"warning"}

CONFIG = {"tts_rate": 1.0, "keyboard_mode": False, "stt_mode": "always", "listen_window_s": 10,
          "stt_gap_s": 1.2, "stt_max_line_s": 15, "stt_echo_s": 4, "cue_s": 1.0, "max_trial_s": 10.0,
          "user_name": "Sam"}
# Extra /api/config fields per state.
STATE_CONFIG: dict[str, dict] = {
    "laptop-busy": {"phone_url": "https://example-words-here.trycloudflare.com/?key=0123456789abcdef0123",
                    "app_link": "brainsync://pair?url=https%3A%2F%2Fexample-words-here.trycloudflare.com"
                                "&key=0123456789abcdef0123"},
}


def harness(name: str, base_url: str) -> str:
    """ui/index.html with a stand-in WebSocket and /api/config that play ``name``'s messages."""
    _, _, messages, (mic_state, mic_words), details = STATES[name]
    if mic_state == "muted":
        messages = [*messages, {"type": "listen", "seconds": 0, "delay_s": 0, "until_stopped": False,
                                "muted": True, "reason": "muted"}]
    prelude = f"""<base href="{base_url}">
<style>* {{ transition: none !important; }}</style>
<script>
// tools/ui_shots.py: no server. The page's WebSocket and /api/config are stand-ins.
window.fetch = () => Promise.resolve({{ json: () => Promise.resolve({json.dumps({**CONFIG, **STATE_CONFIG.get(name, {})})}) }});
window.SpeechRecognition = window.webkitSpeechRecognition = undefined;
class ShotSocket {{
  constructor() {{ this.readyState = 1; setTimeout(() => {{
    this.onopen && this.onopen();
    for (const m of {json.dumps(messages)}) this.onmessage({{ data: JSON.stringify(m) }});
    document.getElementById("warning").hidden = {json.dumps(name not in WARNING_STATES)};
  }}, 20); }}
  send() {{}} close() {{}}
}}
ShotSocket.OPEN = 1;
window.WebSocket = ShotSocket;
</script>"""
    postlude = f"""<script>
setTimeout(() => {{
  micState = {json.dumps(mic_state)}; micText = {json.dumps(mic_words)}; renderMic();
  if ({json.dumps(details)}) document.getElementById("health").click();
}}, 60);
</script>"""
    page = (UI_DIR / "index.html").read_text(encoding="utf-8")
    page = page.replace("<head>", "<head>\n" + prelude, 1)
    return page.replace("</body>", postlude + "\n</body>", 1)


def find_chrome(given: str | None = None) -> str | None:
    candidates = [given, os.environ.get("CHROME"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
                  "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
                  "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"]
    return next((c for c in candidates if c and Path(c).exists()), None)


def for_chrome(path: Path, chrome: str) -> str:
    """A path the browser can open: Windows Chrome under WSL needs C:/... paths."""
    if chrome.endswith(".exe") and shutil.which("wslpath"):
        return subprocess.run(["wslpath", "-m", str(path)], capture_output=True, text=True,
                              check=True).stdout.strip()
    return str(path)


def file_url(path: Path, chrome: str) -> str:
    p = for_chrome(path, chrome)
    return ("file:///" + p.lstrip("/")) if not p.startswith("/") else "file://" + p


def shoot(names: list[str], out: Path, chrome: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    base = file_url(UI_DIR, chrome) + "/"
    profile = out / ".chrome-profile"
    shots = []
    for name in names:
        width, height = STATES[name][:2]
        page = out / f"{name}.html"
        page.write_text(harness(name, base), encoding="utf-8")
        png = out / f"{name}.png"
        png.unlink(missing_ok=True)
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--allow-file-access-from-files", f"--window-size={width},{height}",
                        f"--virtual-time-budget={WAIT_MS}", f"--user-data-dir={for_chrome(profile, chrome)}",
                        f"--screenshot={for_chrome(png, chrome)}", file_url(page, chrome)],
                       capture_output=True, timeout=120)
        page.unlink(missing_ok=True)
        if png.exists():
            shots.append(png)
        else:
            print(f"{name}: no screenshot (Chrome failed)", file=sys.stderr)
    return shots


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/ui_shots.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("states", nargs="*", help="states to shoot (default: all)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"folder for the PNGs (default {DEFAULT_OUT})")
    p.add_argument("--chrome", help="path to Chrome (default: found, or $CHROME)")
    p.add_argument("--list", action="store_true", help="list the states and exit")
    args = p.parse_args(argv)
    if args.list:
        for name, (w, h, *_rest) in STATES.items():
            print(f"{name:12} {w}x{h}")
        return 0
    unknown = [s for s in args.states if s not in STATES]
    if unknown:
        p.error(f"unknown state(s) {unknown}; --list shows them")
    chrome = find_chrome(args.chrome)
    if chrome is None:
        print("Chrome not found: pass --chrome or set $CHROME", file=sys.stderr)
        return 1
    shots = shoot(args.states or list(STATES), args.out, chrome)
    for png in shots:
        print(png)
    return 0 if len(shots) == len(args.states or STATES) else 1


if __name__ == "__main__":
    sys.exit(main())
