"""Config loader: config/default.yaml, config/intents.yaml and config/profile.yaml.

Every model uses ``extra="forbid"`` so a typo in our own YAML fails loudly. Every field is
required (no defaults here): the YAML files are the single place tunable numbers live.

Problems are collected and raised together as one ``ConfigError`` whose message lists each of
them, one per line. Field-level problems come from pydantic; cross-field rules (slot roles,
frequencies against the refresh rate, fallback lists against the menu limits) are checked after.
The stimulus rules mirror the teammate's ``ssvep/stimulus/config.py`` ``validate``, except that a
square wave at a frequency that does not divide the refresh rate is an error here, not a warning.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas import Option, SlotId, SlotSpec, Waveform, validate_option

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "default.yaml"
DEFAULT_INTENTS_PATH = CONFIG_DIR / "intents.yaml"
DEFAULT_PROFILE_PATH = CONFIG_DIR / "profile.yaml"

REQUIRED_FALLBACK_LISTS = ("home", "yes_no")

Role = Literal["say", "next"]


class ConfigError(Exception):
    """A config file is missing, unreadable or invalid. The message lists every problem."""

    def __init__(self, path: Path, problems: list[str]) -> None:
        self.path = path
        self.problems = problems
        super().__init__(f"invalid config {path}:\n  " + "\n  ".join(problems))


class Strict(BaseModel):
    """Base for our own config files: unknown keys and NaN/Infinity are errors."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ---------------------------------------------------------------------------
# default.yaml
# ---------------------------------------------------------------------------


class SlotConfig(SlotSpec):
    """A stimulus slot as written in default.yaml (a SlotSpec that rejects unknown keys)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ServerConfig(Strict):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class DisplayConfig(Strict):
    expected_refresh_hz: float = Field(gt=0)


class StimConfig(Strict):
    slots: list[SlotConfig] = Field(min_length=1)
    roles: dict[SlotId, Role]
    waveform: Waveform
    cue_s: float = Field(ge=0)
    max_trial_s: float = Field(gt=0)
    rest_s: float = Field(ge=0)

    def slot_for(self, role: Role) -> str:
        """The slot id that has ``role`` (validation guarantees exactly one)."""
        return next(slot for slot, r in self.roles.items() if r == role)


class BaselineConfig(Strict):
    seconds: float = Field(gt=0)


class MenusConfig(Strict):
    dynamic: bool
    option_count: int = Field(ge=1)
    label_max_chars: int = Field(ge=1)
    label_max_words: int = Field(ge=1)
    utterance_max_words: int = Field(ge=1)


class LLMConfig(Strict):
    model: str = Field(min_length=1)
    timeout_s: float = Field(gt=0)


class ContextConfig(Strict):
    history_turns: int = Field(ge=0)


class SpeechConfig(Strict):
    tts_rate: float = Field(gt=0)
    stt_mode: Literal["push_to_talk"]
    max_speak_s: float = Field(gt=0)


class SelectionConfig(Strict):
    confirm_mode: Literal["never", "always", "low_score"]
    chosen_highlight_s: float = Field(ge=0)


class AppConfig(Strict):
    server: ServerConfig
    display: DisplayConfig
    stim: StimConfig
    baseline: BaselineConfig
    menus: MenusConfig
    llm: LLMConfig
    context: ContextConfig
    speech: SpeechConfig
    selection: SelectionConfig


def check_app_config(cfg: AppConfig) -> list[str]:
    """Cross-field rules pydantic cannot express per field. Returns problems (empty = fine)."""
    problems: list[str] = []
    stim = cfg.stim
    refresh = cfg.display.expected_refresh_hz
    slot_ids = [s.slot for s in stim.slots]

    dupes = sorted(slot for slot, n in Counter(slot_ids).items() if n > 1)
    if dupes:
        problems.append(f"stim.slots: duplicate slot ids {dupes}")

    for slot in stim.roles:
        if slot not in slot_ids:
            problems.append(f"stim.roles: {slot} is not a defined slot")
    for slot in dict.fromkeys(slot_ids):
        if slot not in stim.roles:
            problems.append(f"stim.roles: slot {slot} has no role")
    role_counts = Counter(stim.roles.values())
    for role in ("say", "next"):
        if role_counts[role] != 1:
            problems.append(f"stim.roles: need exactly one '{role}' slot, found {role_counts[role]}")

    nyquist = refresh / 2
    for s in stim.slots:
        if not 0 < s.freq_hz < nyquist:
            problems.append(
                f"stim.slots: {s.slot} ({s.freq_hz:g} Hz) must be between 0 and {nyquist:g} Hz "
                f"(half the {refresh:g} Hz refresh rate)"
            )
        if stim.waveform == "square" and not math.isclose(
            refresh / s.freq_hz, round(refresh / s.freq_hz)
        ):
            problems.append(
                f"stim.slots: {s.slot} ({s.freq_hz:g} Hz) does not divide the {refresh:g} Hz "
                "refresh rate, so a square wave has an uneven duty cycle; use waveform: sine"
            )
    pairs = [(s.freq_hz, round(s.phase_deg % 360, 6)) for s in stim.slots]
    if len(set(pairs)) != len(pairs):
        problems.append("stim.slots: two slots share the same frequency and phase")
    return problems


# ---------------------------------------------------------------------------
# intents.yaml
# ---------------------------------------------------------------------------


class Intent(Strict):
    id: str = Field(min_length=1)
    description: str


class FallbackOption(Option):
    """A fallback option as written in intents.yaml (an Option that rejects unknown keys)."""

    model_config = ConfigDict(extra="forbid")


class IntentsConfig(Strict):
    intents: list[Intent] = Field(min_length=1)
    fallback_lists: dict[str, list[FallbackOption]]

    @property
    def intent_ids(self) -> list[str]:
        return [i.id for i in self.intents]


def check_intents(
    cfg: IntentsConfig,
    option_count: int,
    label_max_chars: int,
    utterance_max_words: int,
    label_max_words: int,
) -> list[str]:
    """Cross-field rules for intents.yaml against the menu limits. Returns problems."""
    problems: list[str] = []
    ids = cfg.intent_ids
    dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
    if dupes:
        problems.append(f"intents: duplicate ids {dupes}")
    known = set(ids)

    for name in REQUIRED_FALLBACK_LISTS:
        if name not in cfg.fallback_lists:
            problems.append(f"fallback_lists: required list '{name}' is missing")

    for name, options in cfg.fallback_lists.items():
        where = f"fallback_lists.{name}"
        if len(options) != option_count:
            problems.append(
                f"{where}: has {len(options)} options, needs exactly {option_count} "
                "(menus.option_count)"
            )
        for k, opt in enumerate(options):
            if opt.intent_id not in known:
                problems.append(f"{where}[{k}]: unknown intent_id '{opt.intent_id}'")
            for p in validate_option(opt, label_max_chars, utterance_max_words, label_max_words):
                problems.append(f"{where}[{k}]: {p}")
        labels = [o.label.strip().lower() for o in options]
        if len(set(labels)) != len(labels):
            problems.append(f"{where}: duplicate labels")
    return problems


# ---------------------------------------------------------------------------
# profile.yaml
# ---------------------------------------------------------------------------


class Person(Strict):
    name: str = Field(min_length=1)
    relation: str


class Profile(Strict):
    """Fictional demo persona. ``fictional: true`` is required so nobody drops in a real person."""

    fictional: Literal[True]
    note: str
    name: str = Field(min_length=1)
    about: str
    likes: list[str]
    dislikes: list[str]
    people: list[Person]
    routines: list[str]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

M = TypeVar("M", bound=BaseModel)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a key repeated in one mapping (safe_load keeps the last silently)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(path, [f"cannot read file: {e}"]) from e
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as e:
        raise ConfigError(path, [f"invalid YAML: {e}"]) from e
    if not isinstance(raw, dict):
        raise ConfigError(path, ["top level must be a mapping of keys"])
    return raw


def _validate(model: type[M], raw: dict[str, Any], path: Path) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        problems = [
            f"{'.'.join(str(p) for p in err['loc']) or '(top level)'}: {err['msg']}"
            for err in e.errors()
        ]
        raise ConfigError(path, problems) from e


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate default.yaml (or ``path``). Raises ConfigError listing every problem."""
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    cfg = _validate(AppConfig, _read_yaml(path), path)
    problems = check_app_config(cfg)
    if problems:
        raise ConfigError(path, problems)
    return cfg


def load_intents(
    path: str | Path | None = None,
    *,
    option_count: int | None = None,
    label_max_chars: int | None = None,
    utterance_max_words: int | None = None,
    label_max_words: int | None = None,
) -> IntentsConfig:
    """Load and validate intents.yaml (or ``path``) against the menu limits.

    Limits left as None are taken from the default config's ``menus`` section.
    """
    path = Path(path) if path is not None else DEFAULT_INTENTS_PATH
    limits = {
        "option_count": option_count,
        "label_max_chars": label_max_chars,
        "utterance_max_words": utterance_max_words,
        "label_max_words": label_max_words,
    }
    if None in limits.values():
        menus = load_config().menus
        limits = {k: getattr(menus, k) if v is None else v for k, v in limits.items()}
    cfg = _validate(IntentsConfig, _read_yaml(path), path)
    problems = check_intents(cfg, **limits)
    if problems:
        raise ConfigError(path, problems)
    return cfg


def load_profile(path: str | Path | None = None) -> Profile:
    """Load and validate profile.yaml (or ``path``). Raises ConfigError listing every problem."""
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    return _validate(Profile, _read_yaml(path), path)


__all__ = [
    "REPO_ROOT",
    "CONFIG_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_INTENTS_PATH",
    "DEFAULT_PROFILE_PATH",
    "REQUIRED_FALLBACK_LISTS",
    "Role",
    "ConfigError",
    "SlotConfig",
    "ServerConfig",
    "DisplayConfig",
    "StimConfig",
    "BaselineConfig",
    "MenusConfig",
    "LLMConfig",
    "ContextConfig",
    "SpeechConfig",
    "SelectionConfig",
    "AppConfig",
    "check_app_config",
    "Intent",
    "FallbackOption",
    "IntentsConfig",
    "check_intents",
    "Person",
    "Profile",
    "load_config",
    "load_intents",
    "load_profile",
]
