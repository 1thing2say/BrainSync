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
import os
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.schemas import Option, SlotId, SlotSpec, Waveform, validate_option
from brainsync import paths

# In a source checkout these are the repo's folders; brainsync/paths.py has the installed places
# and the BRAINSYNC_* variables that override them.
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = paths.config_dir()
STATE_DIR = paths.state_dir()  # relative paths the app writes (speech.cache_dir) resolve here
DEFAULT_CONFIG_PATH = CONFIG_DIR / "default.yaml"
DEFAULT_INTENTS_PATH = CONFIG_DIR / "intents.yaml"
DEFAULT_PROFILE_PATH = CONFIG_DIR / "profile.yaml"  # the fictional demo persona (committed)
USER_PROFILE_PATH = STATE_DIR / "profiles" / "user.yaml"  # a real user's profile: gitignored
DEFAULT_ENV_PATH = paths.env_file()  # gitignored; secrets such as CARTESIA_API_KEY

REQUIRED_FALLBACK_LISTS = ("home", "yes_no")

# Page mode: "option" slots carry one reply each, "next" asks for a new page.
# List mode (the 2-target fallback): "say" speaks the highlighted option, "next" moves the highlight.
Role = Literal["option", "say", "next"]


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


class ConfirmSlots(Strict):
    """The two targets of the confirm screen in page mode."""

    say: SlotId
    back: SlotId


class StimConfig(Strict):
    slots: list[SlotConfig] = Field(min_length=1)
    roles: dict[SlotId, Role]
    confirm: ConfirmSlots
    waveform: Waveform
    cue_s: float = Field(ge=0)
    max_trial_s: float = Field(gt=0)
    rest_s: float = Field(ge=0)

    def slot_for(self, role: Role) -> str:
        """The slot id that has ``role`` (validation guarantees exactly one)."""
        return next(slot for slot, r in self.roles.items() if r == role)


class BaselineConfig(Strict):
    seconds: float = Field(gt=0)
    timeout_s: float = Field(gt=0)


class MenusConfig(Strict):
    mode: Literal["page", "list"]
    dynamic: bool
    option_count: int = Field(ge=1)
    # extra candidates Claude writes, ranked last, that replace an invalid or repeated option
    spare_options: int = Field(default=1, ge=0, le=3)
    label_max_chars: int = Field(ge=1)
    label_max_words: int = Field(ge=1)
    utterance_max_words: int = Field(ge=1)


class LLMConfig(Strict):
    model: str = Field(min_length=1)
    timeout_s: float = Field(gt=0)
    keepalive_s: float = Field(default=20.0, ge=0)  # 0: no keep-alive calls
    hedge_after_s: float = Field(default=2.5, ge=0)  # a second, identical call if none answered; 0: off


class ContextConfig(Strict):
    history_turns: int = Field(ge=0)


class SpeechConfig(Strict):
    provider: Literal["cartesia", "browser"]
    voice_id: str = ""
    model: str = Field(min_length=1)
    api_version: str = Field(min_length=1)
    language: str = Field(min_length=1)
    format: Literal["mp3", "wav"]
    sample_rate: Literal[8000, 16000, 22050, 24000, 44100, 48000]
    bit_rate: Literal[32000, 64000, 96000, 128000, 192000]
    cache_dir: str = "cache/tts"  # clips on disk, relative to the repo (gitignored); "" = memory only
    tts_timeout_s: float = Field(gt=0)
    max_concurrent: int = Field(ge=1)
    tts_rate: float = Field(gt=0)
    # always: the whole session except while a reply plays (hands-free); turn: listen_window_s on
    # the partner's turn; push_to_talk: only the Listen button
    stt_mode: Literal["always", "turn", "push_to_talk"]
    listen_window_s: float = Field(gt=0)  # turn mode, and the Listen button while paused
    stt_tail_s: float = Field(ge=0)  # wait after a reply ends before listening (no echo)
    stt_interrupts: Literal["question", "never"] = "question"  # may a spoken question stop a trial
    stt_interrupt_cooldown_s: float = Field(default=20.0, ge=0)  # at most one spoken interrupt per
    stt_gap_s: float = Field(default=1.2, gt=0)  # silence that ends a partner line
    stt_max_line_s: float = Field(default=15.0, gt=0)  # a long monologue is sent after this long
    stt_echo_s: float = Field(default=4.0, ge=0)  # check lines this soon after reopening for echo
    start_muted: bool = False
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
    mode = cfg.menus.mode
    if mode == "page":
        wanted = {"option": cfg.menus.option_count, "next": 1, "say": 0}
    else:
        wanted = {"option": 0, "say": 1, "next": 1}
    for role, n in wanted.items():
        if role_counts[role] != n:
            problems.append(f"stim.roles: {mode} mode needs {n} '{role}' slot(s), found {role_counts[role]}")
    if mode == "page":
        for name in ("say", "back"):
            if getattr(stim.confirm, name) not in slot_ids:
                problems.append(f"stim.confirm.{name}: {getattr(stim.confirm, name)} is not a defined slot")
        if stim.confirm.say == stim.confirm.back:
            problems.append("stim.confirm: say and back must be different slots")
    elif cfg.selection.confirm_mode != "never":
        problems.append("selection.confirm_mode: list mode has no confirm step; use never")

    if cfg.llm.hedge_after_s and cfg.llm.hedge_after_s >= cfg.llm.timeout_s:
        problems.append(
            f"llm.hedge_after_s ({cfg.llm.hedge_after_s:g}) must be shorter than "
            f"llm.timeout_s ({cfg.llm.timeout_s:g}), or 0 for no hedge"
        )
    if cfg.speech.stt_max_line_s <= cfg.speech.stt_gap_s:
        problems.append("speech.stt_max_line_s must be longer than speech.stt_gap_s")
    if cfg.speech.model.endswith("-latest"):
        problems.append(f"speech.model: pin a model version, not {cfg.speech.model}")

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
    if cfg.baseline.timeout_s <= cfg.baseline.seconds:
        problems.append(
            f"baseline.timeout_s ({cfg.baseline.timeout_s:g}) must be longer than "
            f"baseline.seconds ({cfg.baseline.seconds:g})"
        )
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


class FallbackMatch(Strict):
    """A keyword rule: a partner line matching ``when`` (a regex) gets the fixed list ``show``."""

    when: str = Field(min_length=1)
    show: str = Field(min_length=1)


class IntentsConfig(Strict):
    intents: list[Intent] = Field(min_length=1)
    fallback_lists: dict[str, list[FallbackOption]]
    fallback_match: list[FallbackMatch] = []

    def fallback_for(self, text: str, question: bool) -> str:
        """The fixed list for a partner line: the first matching rule, else ``yes_no`` for a
        question and ``home`` for anything else."""
        line = text.strip()
        for rule in self.fallback_match:
            if re.search(rule.when, line, re.IGNORECASE):
                return rule.show
        return "yes_no" if question else "home"

    @property
    def intent_ids(self) -> list[str]:
        return [i.id for i in self.intents]

    @property
    def utterances(self) -> list[str]:
        """Every fixed sentence, each once (the clips pre-generated at startup)."""
        return list(dict.fromkeys(o.utterance for opts in self.fallback_lists.values() for o in opts))


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

    for k, rule in enumerate(cfg.fallback_match):
        where = f"fallback_match[{k}]"
        if rule.show not in cfg.fallback_lists:
            problems.append(f"{where}: shows unknown list '{rule.show}'")
        try:
            re.compile(rule.when)
        except re.error as exc:
            problems.append(f"{where}: bad regex ({exc})")
    return problems


# ---------------------------------------------------------------------------
# profile.yaml
# ---------------------------------------------------------------------------


# Short free-text entries (a like, a phrase, a topic). The limits keep the LLM prompt small.
Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Sentence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class Person(Strict):
    name: str = Field(min_length=1, max_length=40)
    relation: str = Field(default="", max_length=40)


class Personality(Strict):
    """How the user talks (the "personality map"). It shapes the tone and length of every option,
    never its facts."""

    formality: Literal["casual", "neutral", "formal"] = "neutral"
    length: Literal["brief", "medium", "chatty"] = "brief"
    humour: Literal["none", "light", "playful"] = "none"
    warmth: Literal["reserved", "warm"] = "warm"
    directness: Literal["gentle", "direct"] = "gentle"
    phrases: list[Item] = Field(default_factory=list, max_length=10)  # things they often say
    avoid_phrases: list[Item] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=300)  # "how I talk", in their own words


class Profile(Strict):
    """Who the user is and how they talk. Facts are used only when the partner asks about them.

    The committed demo persona (config/profile.yaml) must say ``fictional: true``, so nobody drops
    a real person into the repo; a real user's profile lives in the gitignored ``profiles/``.
    """

    fictional: bool = False
    note: str = ""
    name: str = Field(min_length=1, max_length=40)
    about: str = Field(default="", max_length=300)
    likes: list[Item] = Field(default_factory=list, max_length=15)
    dislikes: list[Item] = Field(default_factory=list, max_length=15)
    people: list[Person] = Field(default_factory=list, max_length=15)
    routines: list[Sentence] = Field(default_factory=list, max_length=10)
    avoid_topics: list[Item] = Field(default_factory=list, max_length=10)  # never brought up
    personality: Personality = Field(default_factory=Personality)


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
    """Load and validate the demo persona, profile.yaml (or ``path``); it must be fictional.
    Raises ConfigError listing every problem."""
    path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    profile = _validate(Profile, _read_yaml(path), path)
    if not profile.fictional:
        raise ConfigError(path, ["fictional: the demo persona must say fictional: true "
                                 "(a real user's profile belongs in profiles/user.yaml)"])
    return profile


def load_user_profile(path: str | Path = USER_PROFILE_PATH) -> Profile | None:
    """The user's own profile, or None if there is none yet. Raises ConfigError if it is invalid."""
    path = Path(path)
    if not path.exists():
        return None
    return _validate(Profile, _read_yaml(path), path)


def save_user_profile(profile: Profile, path: str | Path = USER_PROFILE_PATH) -> None:
    """Write the user's profile (a temp file, then replace, so a crash never leaves half a file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Personal profile: stays on this computer (profiles/ is gitignored).\n" + body,
                   encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "REPO_ROOT",
    "CONFIG_DIR",
    "STATE_DIR",
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
    "Personality",
    "Profile",
    "USER_PROFILE_PATH",
    "load_config",
    "load_intents",
    "load_profile",
    "load_user_profile",
    "save_user_profile",
]


def load_env(path: str | Path = DEFAULT_ENV_PATH) -> list[str]:
    """Set ``KEY=VALUE`` lines from a ``.env`` file in ``os.environ``, unless already set there.

    Returns the names that were set, never the values: secrets must not be printed or logged.
    A missing file is fine (the environment may already hold the keys).
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    names = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            names.append(key)
    return names
