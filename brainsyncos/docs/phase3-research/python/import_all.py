"""Import every module of the installed brainsync/app/tools packages; report failures."""
import importlib, pkgutil, sys, traceback
ok, bad = 0, []
for top in ("brainsync", "app", "tools"):
    try:
        pkg = importlib.import_module(top)
    except Exception as e:
        bad.append((top, repr(e))); continue
    for m in pkgutil.walk_packages(pkg.__path__, top + "."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name); ok += 1
        except Exception as e:
            bad.append((m.name, f"{type(e).__name__}: {e}"))
print("imported", ok)
for n, e in bad:
    print("FAIL", n, e)
# lazy imports used at runtime
for mod in ("psychopy.visual", "psychopy.event", "matplotlib.backends.backend_agg", "chess.pgn", "chess.engine", "uvicorn", "fastapi", "anthropic", "httpx", "PIL.ImageFont", "yaml", "websockets"):
    try:
        importlib.import_module(mod)
    except Exception as e:
        print("FAIL lazy", mod, e)
print("lazy ok")
