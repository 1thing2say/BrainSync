"""Tests for app/config.py: the shipped config files load, and invalid ones fail with a clear message."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from app.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_INTENTS_PATH,
    DEFAULT_PROFILE_PATH,
    AppConfig,
    ConfigError,
    load_config,
    load_intents,
    load_profile,
)
from app.schemas import PROTOCOL, DecoderHello, SlotSpec, hello_problems


def _raw(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: Any, name: str = "cfg.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Shipped files
# ---------------------------------------------------------------------------


def test_default_config_loads() -> None:
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.server.host == "127.0.0.1" and cfg.server.port == 8000
    assert [(s.slot, s.freq_hz) for s in cfg.stim.slots] == [
        ("s1", 15.0), ("s2", 17.0), ("s3", 20.0), ("s4", 12.0)]
    assert cfg.stim.roles == {"s1": "option", "s2": "option", "s3": "option", "s4": "next"}
    assert cfg.stim.slot_for("next") == "s4"
    assert (cfg.stim.confirm.say, cfg.stim.confirm.back) == ("s1", "s4")
    assert cfg.stim.waveform == "sine"
    assert cfg.menus.mode == "page" and cfg.menus.option_count == 3
    assert cfg.selection.confirm_mode == "always"
    assert cfg.speech.provider == "cartesia" and cfg.speech.model == "sonic-3.5"


def test_list_mode_fallback_loads(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _list_mode(_raw(DEFAULT_CONFIG_PATH))))
    assert cfg.menus.mode == "list"
    assert cfg.stim.slot_for("say") == "s1" and cfg.stim.slot_for("next") == "s2"


def test_default_config_accepts_a_matching_bridge_hello() -> None:
    cfg = load_config()
    hello = DecoderHello(protocol=PROTOCOL, name="bridge", refresh_hz=59.94, waveform="sine",
                         slots=[SlotSpec(slot=s.slot, freq_hz=s.freq_hz) for s in cfg.stim.slots])
    assert hello_problems(hello, cfg.stim.slots, cfg.stim.waveform,
                          cfg.display.expected_refresh_hz) == []


def test_default_intents_load() -> None:
    cfg = load_intents()
    menus = load_config().menus
    assert "yes" in cfg.intent_ids and "no" in cfg.intent_ids  # not parsed as YAML booleans
    for name in ("home", "yes_no", "more"):
        assert len(cfg.fallback_lists[name]) == menus.option_count


def test_default_profile_loads() -> None:
    profile = load_profile()
    assert profile.fictional is True
    assert profile.name


def test_explicit_paths_match_defaults() -> None:
    assert load_config(DEFAULT_CONFIG_PATH) == load_config()
    assert load_intents(DEFAULT_INTENTS_PATH) == load_intents()
    assert load_profile(DEFAULT_PROFILE_PATH) == load_profile()


# ---------------------------------------------------------------------------
# Invalid default.yaml variants
# ---------------------------------------------------------------------------


def _set(*keys_and_value: Any) -> Callable[[dict[str, Any]], None]:
    """Mutation that sets a nested key: _set("stim", "cue_s", -1)."""
    *keys, value = keys_and_value

    def apply(raw: dict[str, Any]) -> None:
        node = raw
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value

    return apply


def _delete(*keys: str) -> Callable[[dict[str, Any]], None]:
    def apply(raw: dict[str, Any]) -> None:
        node = raw
        for k in keys[:-1]:
            node = node[k]
        del node[keys[-1]]

    return apply


S1 = {"slot": "s1", "freq_hz": 15.0, "phase_deg": 0}
S2 = {"slot": "s2", "freq_hz": 17.0, "phase_deg": 0}
S3 = {"slot": "s3", "freq_hz": 20.0, "phase_deg": 0}
S4 = {"slot": "s4", "freq_hz": 12.0, "phase_deg": 0}
PAGE_ROLES = {"s1": "option", "s2": "option", "s3": "option", "s4": "next"}


def _list_mode(raw: dict[str, Any]) -> dict[str, Any]:
    """The 2-target fallback: SAY / NEXT on 15 / 17 Hz, no confirm step."""
    raw["menus"]["mode"] = "list"
    raw["stim"]["slots"] = [S1, S2]
    raw["stim"]["roles"] = {"s1": "say", "s2": "next"}
    raw["selection"]["confirm_mode"] = "never"
    return raw


def _in_list_mode(mutate: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def apply(raw: dict[str, Any]) -> None:
        mutate(_list_mode(raw))

    return apply

INVALID_CONFIGS = [
    ("unknown top-level key", _set("stim_typo", 1), "stim_typo"),
    ("unknown nested key", _set("stim", "cue_seconds", 1.0), "cue_seconds"),
    ("unknown slot key", _set("stim", "slots", [{**S1, "freq": 15}, S2, S3, S4]), "freq"),
    ("missing section", _delete("llm"), "llm"),
    ("missing key", _delete("stim", "rest_s"), "rest_s"),
    ("duplicate slot ids", _set("stim", "slots", [S1, {**S2, "slot": "s1"}, S3, S4]), "duplicate slot"),
    ("role for unknown slot", _set("stim", "roles", {**PAGE_ROLES, "s5": "option"}), "s5 is not a defined slot"),
    ("slot without role", _set("stim", "roles", {"s1": "option", "s2": "option", "s4": "next"}), "s3 has no role"),
    ("two next roles", _set("stim", "roles", {**PAGE_ROLES, "s3": "next"}), "needs 1 'next' slot(s), found 2"),
    ("say role in page mode", _set("stim", "roles", {**PAGE_ROLES, "s3": "say"}), "needs 0 'say'"),
    ("too few option slots", _set("menus", "option_count", 4), "needs 4 'option' slot(s), found 3"),
    ("confirm on unknown slot", _set("stim", "confirm", {"say": "s1", "back": "s9"}), "stim.confirm.back"),
    ("confirm say == back", _set("stim", "confirm", {"say": "s1", "back": "s1"}), "must be different"),
    ("missing confirm", _delete("stim", "confirm"), "confirm"),
    ("bad role name", _set("stim", "roles", {**PAGE_ROLES, "s2": "skip"}), "roles"),
    ("bad menu mode", _set("menus", "mode", "grid"), "mode"),
    ("list mode: two say roles", _in_list_mode(_set("stim", "roles", {"s1": "say", "s2": "say"})),
     "list mode needs 1 'next' slot(s), found 0"),
    ("list mode: option role", _in_list_mode(_set("stim", "roles", {"s1": "option", "s2": "next"})),
     "list mode needs 0 'option'"),
    ("list mode: confirm step", _in_list_mode(_set("selection", "confirm_mode", "always")), "use never"),
    ("bad slot id", _set("stim", "slots", [S1, {**S2, "slot": "x2"}, S3, S4]), "slot"),
    ("zero frequency", _set("stim", "slots", [S1, {**S2, "freq_hz": 0}, S3, S4]), "freq_hz"),
    ("frequency at nyquist", _set("stim", "slots", [S1, {**S2, "freq_hz": 30.0}, S3, S4]), "half the"),
    ("same freq and phase", _set("stim", "slots", [S1, {**S2, "freq_hz": 15.0}, S3, S4]), "same frequency"),
    (
        "same freq, phase 360 apart",
        _set("stim", "slots", [S1, {**S2, "freq_hz": 15.0, "phase_deg": 360}, S3, S4]),
        "same frequency",
    ),
    ("square at 17 Hz on 60 Hz", _set("stim", "waveform", "square"), "does not divide"),
    ("unknown waveform", _set("stim", "waveform", "triangle"), "waveform"),
    ("negative cue", _set("stim", "cue_s", -0.1), "cue_s"),
    ("negative rest", _set("stim", "rest_s", -1), "rest_s"),
    ("zero max trial", _set("stim", "max_trial_s", 0), "max_trial_s"),
    ("zero baseline", _set("baseline", "seconds", 0), "baseline.seconds"),
    ("zero llm timeout", _set("llm", "timeout_s", 0), "timeout_s"),
    ("zero option count", _set("menus", "option_count", 0), "option_count"),
    ("negative history", _set("context", "history_turns", -1), "history_turns"),
    ("bad confirm mode", _set("selection", "confirm_mode", "sometimes"), "confirm_mode"),
    ("bad stt mode", _set("speech", "stt_mode", "always_on"), "stt_mode"),
    ("max line not over the gap", _set("speech", "stt_max_line_s", 1.0), "stt_max_line_s"),
    ("negative cooldown", _set("speech", "stt_interrupt_cooldown_s", -1), "stt_interrupt_cooldown_s"),
    ("bad stt interrupts", _set("speech", "stt_interrupts", "sometimes"), "stt_interrupts"),
    ("unpinned tts model", _set("speech", "model", "sonic-latest"), "pin a model version"),
    ("bad tts provider", _set("speech", "provider", "polly"), "provider"),
    ("zero tts timeout", _set("speech", "tts_timeout_s", 0), "tts_timeout_s"),
    ("port zero", _set("server", "port", 0), "port"),
    ("port too high", _set("server", "port", 70000), "port"),
    ("zero refresh", _set("display", "expected_refresh_hz", 0), "expected_refresh_hz"),
]


@pytest.mark.parametrize(
    "mutate, expected", [(m, e) for _, m, e in INVALID_CONFIGS], ids=[n for n, _, _ in INVALID_CONFIGS]
)
def test_invalid_config_raises(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    raw = copy.deepcopy(_raw(DEFAULT_CONFIG_PATH))
    mutate(raw)
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, raw))
    assert expected in str(exc.value)


def test_square_wave_ok_when_frequencies_divide_refresh(tmp_path: Path) -> None:
    raw = _raw(DEFAULT_CONFIG_PATH)
    raw["stim"]["waveform"] = "square"
    raw["stim"]["slots"] = [S1, {**S2, "freq_hz": 10.0}, S3, S4]  # 60 / f is a whole number of frames
    assert load_config(_write(tmp_path, raw)).stim.waveform == "square"


def test_every_problem_is_listed(tmp_path: Path) -> None:
    raw = _raw(DEFAULT_CONFIG_PATH)
    raw["stim"]["cue_s"] = -1
    raw["llm"]["timeout_s"] = 0
    raw["server"]["port"] = 0
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, raw))
    assert len(exc.value.problems) == 3
    for key in ("stim.cue_s", "llm.timeout_s", "server.port"):
        assert key in str(exc.value)


def test_every_cross_field_problem_is_listed(tmp_path: Path) -> None:
    raw = _raw(DEFAULT_CONFIG_PATH)
    raw["stim"]["waveform"] = "square"
    raw["stim"]["roles"] = {"s1": "option"}
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, raw))
    message = str(exc.value)
    assert "does not divide" in message and "has no role" in message


@pytest.mark.parametrize(
    "content, expected",
    [
        ("stim: [unclosed", "invalid YAML"),
        ("- just\n- a list\n", "mapping"),
        ("", "mapping"),
        ("server: {}\nserver: {}\n", "duplicate"),
        ("stim:\n  roles: {s1: say, s1: next}\n", "duplicate"),
    ],
    ids=["bad yaml", "list at top", "empty file", "duplicate section", "duplicate nested key"],
)
def test_unreadable_config_raises(tmp_path: Path, content: str, expected: str) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=expected):
        load_config(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# Invalid intents.yaml variants
# ---------------------------------------------------------------------------

LIMITS = {
    "option_count": 4, "label_max_chars": 18, "utterance_max_words": 14, "label_max_words": 3
}


def _opt(label: str = "Okay", utterance: str = "Okay, sounds good.", intent_id: str = "yes") -> dict:
    return {"label": label, "utterance": utterance, "intent_id": intent_id}


def _intents(**lists: list[dict]) -> dict[str, Any]:
    default_list = [_opt(f"Opt {k}") for k in range(4)]
    return {
        "intents": [{"id": "yes", "description": "agree"}, {"id": "no", "description": "decline"}],
        "fallback_lists": {"home": default_list, "yes_no": default_list, **lists},
    }


def _without_list(name: str) -> dict[str, Any]:
    data = _intents()
    del data["fallback_lists"][name]
    return data


def _with_intents(intents: list[dict]) -> dict[str, Any]:
    data = _intents()
    data["intents"] = intents
    return data


THREE = [_opt(f"Opt {k}") for k in range(3)]

INVALID_INTENTS = [
    ("missing home", _without_list("home"), "'home' is missing"),
    ("missing yes_no", _without_list("yes_no"), "'yes_no' is missing"),
    ("too few options", _intents(home=THREE), "needs exactly 4"),
    ("too many options", _intents(extra=THREE + THREE[:2]), "fallback_lists.extra"),
    ("unknown intent", _intents(home=THREE + [_opt("Hmm", intent_id="ponder")]), "ponder"),
    (
        "duplicate intent ids",
        _with_intents([{"id": "yes", "description": "a"}, {"id": "yes", "description": "b"}]),
        "duplicate ids",
    ),
    ("label too long", _intents(home=THREE + [_opt("A very long label xx")]), "label over 18"),
    ("label too many words", _intents(home=THREE + [_opt("one two three four")]), "words"),
    ("utterance too long", _intents(home=THREE + [_opt(utterance="word " * 15)]), "utterance over"),
    ("empty label", _intents(home=THREE + [_opt(label=" ")]), "label is empty"),
    ("duplicate labels", _intents(home=THREE + [_opt("Opt 0")]), "duplicate labels"),
    ("unknown option key", _intents(home=THREE + [{**_opt("X"), "rank": 1}]), "rank"),
    ("yaml boolean id", _with_intents([{"id": True, "description": "a"}]), "intents.0.id"),
    ("match shows an unknown list", {**_intents(), "fallback_match": [{"when": "hi", "show": "nope"}]},
     "unknown list 'nope'"),
    ("match with a bad regex", {**_intents(), "fallback_match": [{"when": "(hi", "show": "home"}]},
     "bad regex"),
    ("match with an unknown key", {**_intents(), "fallback_match": [{"when": "hi", "show": "home",
                                                                     "rank": 1}]}, "rank"),
]


@pytest.mark.parametrize(
    "data, expected", [(d, e) for _, d, e in INVALID_INTENTS], ids=[n for n, _, _ in INVALID_INTENTS]
)
def test_invalid_intents_raise(tmp_path: Path, data: dict[str, Any], expected: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_intents(_write(tmp_path, data, "intents.yaml"), **LIMITS)
    assert expected in str(exc.value)


def test_intents_limits_follow_arguments(tmp_path: Path) -> None:
    path = _write(tmp_path, _intents(), "intents.yaml")
    assert load_intents(path, **LIMITS)
    with pytest.raises(ConfigError, match="needs exactly 3"):
        load_intents(path, **{**LIMITS, "option_count": 3})


# ---------------------------------------------------------------------------
# Invalid profile.yaml variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"fictional": False}, "fictional"),
        ({"name": ""}, "name"),
        ({"hometown": "Oakland"}, "hometown"),
    ],
    ids=["not fictional", "empty name", "unknown key"],
)
def test_invalid_profile_raises(tmp_path: Path, change: dict[str, Any], expected: str) -> None:
    data = {**_raw(DEFAULT_PROFILE_PATH), **change}
    with pytest.raises(ConfigError) as exc:
        load_profile(_write(tmp_path, data, "profile.yaml"))
    assert expected in str(exc.value)


# ---------------------------------------------------------------------------
# The user's own profile (profiles/user.yaml) and the personality map
# ---------------------------------------------------------------------------

from app.config import (  # noqa: E402
    REPO_ROOT,
    USER_PROFILE_PATH,
    Personality,
    Profile,
    load_user_profile,
    save_user_profile,
)
from pydantic import ValidationError  # noqa: E402


def test_demo_persona_has_a_personality():
    p = load_profile().personality
    assert p.length == "brief" and p.formality in ("casual", "neutral", "formal")


def test_a_minimal_user_profile_gets_defaults():
    p = Profile(name="Ana")
    assert not p.fictional and p.likes == [] and p.personality == Personality()
    assert p.personality.length == "brief"  # basic replies unless the user says otherwise


@pytest.mark.parametrize("raw, fragment", [
    ({"name": ""}, "name"),
    ({"name": "Ana", "personality": {"humour": "sarcastic"}}, "humour"),
    ({"name": "Ana", "likes": ["x" * 61]}, "likes.0"),
    ({"name": "Ana", "likes": [f"thing {i}" for i in range(16)]}, "likes"),
    ({"name": "Ana", "about": "x" * 301}, "about"),
    ({"name": "Ana", "people": [{"name": "", "relation": "sister"}]}, "people.0.name"),
    ({"name": "Ana", "hometown": "Oakland"}, "hometown"),
], ids=["empty name", "unknown humour", "long like", "too many likes", "long about",
        "nameless person", "unknown key"])
def test_invalid_user_profile_is_rejected(raw, fragment):
    with pytest.raises(ValidationError) as exc:
        Profile.model_validate(raw)
    assert any(fragment in ".".join(str(p) for p in e["loc"]) for e in exc.value.errors())


def test_list_entries_are_trimmed_and_blank_ones_rejected():
    assert Profile(name="Ana", likes=["  jazz  "]).likes == ["jazz"]
    with pytest.raises(ValidationError):
        Profile(name="Ana", likes=["   "])


def test_user_profile_round_trips_and_is_absent_until_saved(tmp_path):
    path = tmp_path / "profiles" / "user.yaml"
    assert load_user_profile(path) is None
    profile = Profile(name="Ana", likes=["jazz"], personality=Personality(humour="playful"))
    save_user_profile(profile, path)
    assert load_user_profile(path) == profile
    assert path.read_text(encoding="utf-8").startswith("# Personal profile")
    assert not path.with_suffix(".tmp").exists()


def test_invalid_saved_user_profile_raises(tmp_path):
    path = tmp_path / "user.yaml"
    path.write_text("name: ''\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="name"):
        load_user_profile(path)


def test_user_profiles_are_gitignored():
    assert USER_PROFILE_PATH.parent.name == "profiles"
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "profiles/" in ignored
