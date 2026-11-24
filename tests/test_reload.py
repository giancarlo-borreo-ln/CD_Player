"""Regression test: the dvd_player package facade must survive reloads.

Guards the Loki findings: assigning importlib machinery (__spec__, __loader__,
__path__, __file__, __name__, __package__, __cached__) through the facade's
__setattr__ must not be forwarded into submodules (that made a second
importlib.reload raise RecursionError), while genuine public re-exports must
still be forwarded to the modules that use them.

Run: QT_QPA_PLATFORM=offscreen uv run python tests/test_reload.py
"""
from __future__ import annotations

import importlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import dvd_player as dp  # noqa: E402
import dvd_player.engine as engine_mod  # noqa: E402
import dvd_player.state as state_mod  # noqa: E402

# --- 1. three consecutive reloads must be clean (no RecursionError) ---------
for _ in range(3):
    dp = importlib.reload(dp)

# --- 2. importlib machinery is restored/owned by the module itself ----------
for _attr in (
    "__spec__",
    "__loader__",
    "__file__",
    "__path__",
    "__name__",
    "__package__",
    "__cached__",
    "__all__",
):
    assert hasattr(dp, _attr), f"reload lost {_attr}"
assert dp.__class__ is type(dp)
assert dp.__class__.__name__ == "_DvdPlayerModule"
assert all(mod.__class__.__module__ != "dvd_player" for mod in (state_mod, engine_mod)), (
    "reload leaked the facade module class into submodules"
)

# --- 3. identities hold after reload ---------------------------------------
assert dp.PlayerState is dp.state.PlayerState is state_mod.PlayerState
assert dp.DvdPlaybackEngine is dp.engine.DvdPlaybackEngine is engine_mod.DvdPlaybackEngine

# --- 4. behavioral: post-reload monkeypatch reaches the consumer ------------
assert engine_mod.LOAD_TIMEOUT_MS == 30000
dp.LOAD_TIMEOUT_MS = 4242
assert dp.state.LOAD_TIMEOUT_MS == 4242, "patch did not reach state module"
assert engine_mod.LOAD_TIMEOUT_MS == 4242, "patch did not reach engine module"
dp.LOAD_TIMEOUT_MS = 30000
assert engine_mod.LOAD_TIMEOUT_MS == 30000

# --- 5. unknown / dunder assignments are not forwarded ---------------------
engine_globals = set(vars(engine_mod))
dp.__loki_probe__ = object()
dp.not_a_public_name = object()
assert "__loki_probe__" not in set(vars(engine_mod)) - engine_globals
assert "not_a_public_name" not in set(vars(engine_mod)) - engine_globals

print("RELOAD-OK")
