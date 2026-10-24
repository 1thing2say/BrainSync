"""SSVEP flicker stimulus (PsychoPy, frame-locked). psychopy is imported only to open a window."""
from __future__ import annotations

from .config import (CONFIG_DIR, StimulusConfig, Target, Trial, available_configs,
                     config_warnings, load_config, make_trials, override, parse_config,
                     session_trials)
from .flicker import luminance

__all__ = [
    "CONFIG_DIR", "StimulusConfig", "Target", "Trial", "available_configs", "config_warnings",
    "load_config", "luminance", "make_trials", "override", "parse_config", "session_trials",
]
