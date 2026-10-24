"""Pre-generate the Cartesia clips for every fixed sentence in config/intents.yaml.

Run it once while the network is good (or before the demo): the clips go to speech.cache_dir
(cache/tts, gitignored), and the server plays them from disk, even with the Wi-Fi down. Clips
already there are skipped; a change of voice, model or format makes new ones.

    python tools/prerender_tts.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import REPO_ROOT, load_config, load_env, load_intents  # noqa: E402
from app.tts import CartesiaTTS  # noqa: E402


async def prerender(tts: CartesiaTTS, texts: list[str]) -> tuple[int, int]:
    """Fetch every missing clip; returns (cached, total) afterwards."""
    await tts.start()
    try:
        await tts.prefetch(texts)
    finally:
        await tts.stop()
    return tts.cached_count(texts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python tools/prerender_tts.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="config file (default config/default.yaml)")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    m = cfg.menus
    intents = load_intents(option_count=m.option_count, label_max_chars=m.label_max_chars,
                           utterance_max_words=m.utterance_max_words,
                           label_max_words=m.label_max_words)
    if not cfg.speech.cache_dir:
        print("speech.cache_dir is empty: nothing to keep clips in", file=sys.stderr)
        return 1
    load_env()
    tts = CartesiaTTS(cfg.speech, os.environ.get("CARTESIA_API_KEY"),
                      cache_dir=REPO_ROOT / cfg.speech.cache_dir)
    texts = intents.utterances
    if not tts.enabled:
        print(f"Cartesia is off: {tts.why_disabled}", file=sys.stderr)
        return 1
    before, total = tts.cached_count(texts)
    ready, total = asyncio.run(prerender(tts, texts))
    print(f"{ready}/{total} fixed clips on disk ({ready - before} new) in {cfg.speech.cache_dir}")
    return 0 if ready == total else 1


if __name__ == "__main__":
    sys.exit(main())
