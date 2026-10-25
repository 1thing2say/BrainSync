"""The /ws/ui message fixture for the iPhone app's tests (ios/BrainSyncKit).

Builds every server -> partner page and partner page -> server message (PLAN.md section 6) from the
pydantic models in app/schemas.py, each exactly as the server sends or accepts it
(``model_dump(mode="json")``), with every variant the app has to handle: each show_page mode, speak
with and without audio, each listen form and reason, each trial phase, each mic_report state.

    python tools/ui_fixture.py           # write the fixture
    python tools/ui_fixture.py --check   # exit 1 if the committed fixture is stale
"""

from __future__ import annotations

import argparse
import json
import sys
import typing
from pathlib import Path

if __package__ in (None, ""):  # python tools/ui_fixture.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import (  # noqa: E402
    Highlight,
    Judgment,
    Key,
    Listen,
    MicReport,
    MicSet,
    Option,
    PartnerText,
    Resume,
    ServerToUI,
    ShowPage,
    Speak,
    State,
    Stop,
    Transcript,
    Trial,
    TTSDone,
    UISlot,
    UIStatus,
    UIToServer,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "ios/BrainSyncKit/Tests/BrainSyncKitTests/Fixtures/ui_messages.json"


def union_models(union) -> list[type]:
    """The message classes of a discriminated union such as ``ServerToUI``."""
    inner = typing.get_args(union)[0]  # Annotated[Union[...], Field(...)] -> Union[...]
    return list(typing.get_args(inner))


def message_type(model: type) -> str:
    return model.model_fields["type"].default


def literal_values(model: type, field: str) -> list[str]:
    return list(typing.get_args(model.model_fields[field].annotation))


def server_to_ui() -> list:
    page = ShowPage(list_id="home-1", page=1, mode="page", slots=[
        UISlot(slot="s1", role="option", label="Go outside", utterance="Yes, let's go outside."),
        UISlot(slot="s2", role="option", label="Not now", utterance="Not right now."),
        UISlot(slot="s3", role="option", label="After lunch", utterance="Maybe after lunch."),
        UISlot(slot="s4", role="next", label="More options"),
    ])
    confirm = ShowPage(list_id="home-1", page=1, mode="confirm", text="Yes, let's go outside.",
                       slots=[UISlot(slot="s1", role="say", label="Say it"),
                              UISlot(slot="s4", role="back", label="Back")])
    options = [Option(label="Yes", utterance="Yes, please.", intent_id="yes"),
               Option(label="No", utterance="No, thank you.", intent_id="no"),
               Option(label="Maybe", utterance="Maybe later.", intent_id="maybe")]
    listing = ShowPage(list_id="yes_no", page=2, mode="list", options=options, highlight_index=1,
                       slots=[UISlot(slot="s1", role="say", label="SAY"),
                              UISlot(slot="s2", role="next", label="NEXT")])
    listens = [
        Listen(seconds=0, until_stopped=True, delay_s=0.5, reason="start"),  # always mode
        Listen(seconds=10.0, delay_s=0.5, reason="turn"),  # turn mode
        Listen(seconds=0, reason="speaking"),  # stop
        Listen(seconds=0, muted=True, reason="muted"),
        Listen(seconds=0, reason="paused"),
        Listen(seconds=0, reason="idle"),
        Listen(seconds=0, until_stopped=True, reason="reconnect"),
    ]
    assert {m.reason for m in listens} == set(literal_values(Listen, "reason"))
    statuses = [
        UIStatus(decoder_connected=True, decoder_ok=True, display_ok=True, llm_ok=True,
                 fake_decoder=False, keyboard_mode=False, board_ok=True,
                 decoder_message="posterior RMS 12-20 uV", decoder_name="ssvep-fbcca", llm="ok",
                 mic_muted=False, mic="listening", resume_ready=True),
        UIStatus(decoder_connected=False, decoder_ok=False, display_ok=False, llm_ok=False,
                 fake_decoder=True, keyboard_mode=True, board_ok=False,
                 decoder_message="hello rejected: refresh 120 Hz", decoder_name="fake-random",
                 llm="failed", mic_muted=True, mic="muted", resume_ready=False),
        UIStatus(decoder_connected=True, decoder_ok=False, display_ok=True, llm_ok=False,
                 fake_decoder=False, keyboard_mode=False, llm="waiting", mic="denied"),
        UIStatus(decoder_connected=False, decoder_ok=False, display_ok=False, llm_ok=False,
                 fake_decoder=False, keyboard_mode=False),  # every default: llm off, no mic
    ]
    assert {m.llm for m in statuses} == set(literal_values(UIStatus, "llm"))
    trials = [Trial(trial_id=7, phase=p) for p in literal_values(Trial, "phase")]
    return [page, confirm, listing, *trials, Highlight(slot="s1"),
            Speak(text="Yes, let's go outside.", utterance_id="u-7", audio_url="/audio/3f2a9c"),
            Speak(text="Not right now.", utterance_id="u-8"),
            *listens,
            State(name="STIMULATING", detail=""),
            State(name="PAUSED", detail="Decoder disconnected"),
            Transcript(who="partner", text="Do you want to go outside?"),
            Transcript(who="user", text="Yes, let's go outside."),
            *statuses]


def ui_to_server() -> list:
    reports = [MicReport(state=s, restarts=i) for i, s in enumerate(literal_values(MicReport, "state"))]
    return [TTSDone(utterance_id="u-7"),
            PartnerText(text="Do you want to go outside?", source="stt"),
            PartnerText(text="How was your weekend?", source="typed"),
            Key(key="1"), Key(key="C"), Stop(), Resume(),
            Judgment(utterance_id="u-7", correct=True), Judgment(utterance_id="u-8", correct=False),
            MicSet(muted=True), MicSet(muted=False),
            *reports]


def build() -> dict:
    return {"server_to_ui": [m.model_dump(mode="json") for m in server_to_ui()],
            # Clients leave unset optional fields out (resume's start), so the fixture does too.
            "ui_to_server": [m.model_dump(mode="json", exclude_none=True) for m in ui_to_server()]}


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/ui_fixture.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="exit 1 if the fixture is missing or stale")
    p.add_argument("--out", type=Path, default=FIXTURE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"stale: {args.out} (run python tools/ui_fixture.py)", file=sys.stderr)
            return 1
        print(f"up to date: {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
