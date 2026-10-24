"""Conversation history and the context sent with each LLM page request (PLAN.md V3, item 1).

The orchestrator owns one ``Conversation``: it records every partner line and every spoken reply,
and the sentences shown since the partner last spoke (so "More options" never repeats them).
``page_request`` turns that into a ``PageRequest`` for app/menus.py. No I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Who = Literal["partner", "user"]


@dataclass(frozen=True)
class Turn:
    who: Who
    text: str


@dataclass(frozen=True)
class PageRequest:
    """Everything the LLM sees for one page of options."""

    request_id: int
    reason: Literal["partner", "more"]  # a new partner line, or NEXT ("More options")
    partner_last: str  # what the partner said last ("" before they spoke)
    history: tuple[Turn, ...]  # the last history_turns turns, oldest first
    shown: tuple[str, ...]  # utterances already shown since partner_last (not to repeat)
    time_of_day: str  # e.g. "Saturday, 3:40 PM (afternoon)"


@dataclass
class Conversation:
    history_turns: int
    turns: list[Turn] = field(default_factory=list)
    shown: list[str] = field(default_factory=list)

    def partner_said(self, text: str) -> None:
        self.turns.append(Turn("partner", text))
        self.shown.clear()  # a new question: earlier options may fit again

    def user_said(self, text: str) -> None:
        self.turns.append(Turn("user", text))

    def showed(self, utterances: list[str]) -> None:
        for u in utterances:
            if u not in self.shown:
                self.shown.append(u)

    @property
    def partner_last(self) -> str:
        return next((t.text for t in reversed(self.turns) if t.who == "partner"), "")

    def page_request(self, request_id: int, reason: Literal["partner", "more"],
                     now_unix: float) -> PageRequest:
        recent = self.turns[-self.history_turns:] if self.history_turns else []
        return PageRequest(request_id=request_id, reason=reason, partner_last=self.partner_last,
                           history=tuple(recent), shown=tuple(self.shown),
                           time_of_day=time_of_day(now_unix))


def time_of_day(now_unix: float) -> str:
    """Local weekday, clock time and part of the day, for the prompt."""
    now = datetime.fromtimestamp(now_unix)
    hour = now.hour
    part = ("night" if hour < 5 else "morning" if hour < 12 else "afternoon" if hour < 17
            else "evening" if hour < 22 else "night")
    clock = now.strftime("%I:%M %p").lstrip("0")
    return f"{now.strftime('%A')}, {clock} ({part})"
