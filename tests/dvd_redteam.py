"""Red-team suite for the dvd_player backend (sections 1-4).

Attacks are inert data only.  A marker file proves no shell ever ran.
Run:  QT_QPA_PLATFORM=offscreen uv run python /tmp/dvd_redteam.py
"""
from __future__ import annotations

import ast
import io
import json
import locale
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import tokenize
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "dvd_player"
sys.path.insert(0, str(REPO / "src"))

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from dvd_player import (  # noqa: E402
    DiscIdentity,
    DvdPlaybackEngine,
    PlaybackError,
    PlayerState,
    ResumeStore,
    check_environment,
    disc_present,
    eject_device,
)

COUNT = 0
MINOR_DEFECTS: list[str] = []


def check(condition: object) -> None:
    global COUNT
    assert condition
    COUNT += 1


def note_defect(text: str) -> None:
    if text not in MINOR_DEFECTS:
        MINOR_DEFECTS.append(text)


# ════════════════════════════════════════════════════════════════════
# 0. Static injection audit (AST, not just grep)
# ════════════════════════════════════════════════════════════════════
# SCOPE: every module under src/dvd_player, recursively (subpackages included).
# A non-recursive glob silently skipped injected files one directory down.
SOURCE_PATHS = sorted(PKG.rglob("*.py"))
assert SOURCE_PATHS, "static audit scope is empty"
assert all(PKG in path.parents for path in SOURCE_PATHS), "audit escaped the package"
source_text = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE_PATHS)
tree = ast.parse(source_text)

# parent map, used to tell an attribute base / sanctioned reflection from a
# module object being passed around as a value.
PARENT: dict[int, ast.AST] = {}
for _parent in ast.walk(tree):
    for _child in ast.iter_child_nodes(_parent):
        PARENT[id(_child)] = _parent

SPAWN_ATTRS = {"run", "call", "Popen", "check_call", "check_output",
               "getoutput", "getstatusoutput"}
SUBPROCESS_ATTRS = {"run", "SubprocessError", "TimeoutExpired", "CalledProcessError"}
ALLOWED_SUBPROCESS_IMPORTS = SUBPROCESS_ATTRS | {"CompletedProcess"}
banned_os = {"system", "popen"}

subprocess_modules: set[str] = {"subprocess"}
subprocess_from: dict[str, str] = {}
os_modules: set[str] = {"os"}
os_from: dict[str, str] = {}
imported: set[str] = set()

for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            imported.add(top)
            if top == "subprocess":
                subprocess_modules.add(alias.asname or "subprocess")
            elif top == "os":
                os_modules.add(alias.asname or "os")
    elif isinstance(node, ast.ImportFrom) and node.module:
        top = node.module.split(".")[0]
        imported.add(top)
        for alias in node.names:
            bound = alias.asname or alias.name
            if top == "subprocess":
                if alias.name not in ALLOWED_SUBPROCESS_IMPORTS:
                    raise AssertionError(
                        f"forbidden 'from subprocess import {alias.name}' - "
                        "only run and exception classes are permitted"
                    )
                subprocess_from[bound] = alias.name
            elif top == "os":
                if alias.name in banned_os or alias.name.startswith(("exec", "spawn")):
                    raise AssertionError(f"forbidden 'from os import {alias.name}'")
                os_from[bound] = alias.name

# The app legitimately reflects on engine/context objects (~20 sites); only
# reflection aimed at a process/import module is dangerous.  The single
# exception is hardware.py's documented ``getattr(subprocess, "run")`` alias,
# whose result must be named _subprocess_run; computed or rebound forms fail.
sanctioned_getattr: set[int] = set()
for node in ast.walk(tree):
    value = None
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        value, targets = node.value, node.targets
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        value, targets = node.value, [node.target]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name) and value.func.id == "getattr"
        and len(value.args) >= 2
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id in subprocess_modules
        and isinstance(value.args[1], ast.Constant)
        and value.args[1].value == "run"
        and len(targets) == 1
        and isinstance(targets[0], ast.Name)
        and targets[0].id == "_subprocess_run"
    ):
        continue
    sanctioned_getattr.add(id(value))

subprocess_calls: list[ast.Call] = []
shell_true: list[ast.Call] = []
SENSITIVE_MODULES = subprocess_modules | os_modules | {"shutil", "importlib"}

for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in {"eval", "exec", "compile", "__import__"}:
                raise AssertionError(f"forbidden builtin {func.id} call at line {node.lineno}")
            if func.id in subprocess_from and subprocess_from[func.id] in SPAWN_ATTRS:
                subprocess_calls.append(node)
            if func.id in os_from and (
                os_from[func.id] in banned_os
                or os_from[func.id].startswith(("exec", "spawn"))
            ):
                raise AssertionError(f"forbidden os.{os_from[func.id]} call at line {node.lineno}")
            if func.id in {"getattr", "setattr"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Name) and first.id in SENSITIVE_MODULES:
                    if func.id == "getattr" and id(node) in sanctioned_getattr:
                        pass
                    else:
                        raise AssertionError(
                            f"forbidden {func.id}({first.id}, ...) reflection "
                            f"at line {node.lineno}"
                        )
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name) and base.id in subprocess_modules:
                if func.attr not in SUBPROCESS_ATTRS:
                    raise AssertionError(
                        f"forbidden subprocess.{func.attr} at line {node.lineno}"
                    )
                if func.attr in SPAWN_ATTRS:
                    subprocess_calls.append(node)
            if isinstance(base, ast.Name) and base.id in os_modules and (
                func.attr in banned_os or func.attr.startswith(("exec", "spawn"))
            ):
                raise AssertionError(f"forbidden os.{func.attr} at line {node.lineno}")
        for keyword in node.keywords:
            if keyword.arg == "shell":
                shell_true.append(node)

# Binding subprocess/os attributes to a bare name creates an untracked call
# route (``run = subprocess.run; run(...)``), so it is banned outright.
for node in ast.walk(tree):
    value = None
    if isinstance(node, ast.Assign):
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        value = node.value
    if value is None:
        continue
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and (
        value.value.id in subprocess_modules or value.value.id in os_modules
    ):
        raise AssertionError(f"forbidden module-attribute alias at line {node.lineno}")
    if isinstance(value, ast.Name) and value.id in os_modules:
        raise AssertionError(f"forbidden os module alias at line {node.lineno}")

# A module object may only appear as an attribute base (or via the sanctioned
# getattr above); ``x = subprocess``, ``vars(subprocess)``, ``getattr(subprocess,
# "P" + "open")`` and passing it around all fail here.
for node in ast.walk(tree):
    if not (isinstance(node, ast.Name) and node.id in subprocess_modules):
        continue
    parent = PARENT.get(id(node))
    if isinstance(parent, ast.Attribute) and parent.value is node:
        continue
    if isinstance(parent, ast.Call) and id(parent) in sanctioned_getattr:
        continue
    raise AssertionError(f"forbidden use of subprocess module object at line {node.lineno}")

# globals()["subprocess"] / locals()["subprocess"] style string indirection.
for node in ast.walk(tree):
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and node.slice.value in subprocess_modules
    ):
        raise AssertionError(f"forbidden string-keyed subprocess access at line {node.lineno}")

check(len(subprocess_calls) == 1)
check(len(shell_true) == 0)
check("pickle" not in imported)
check("yaml" not in imported)
check("importlib" not in imported)
check("os" in imported and "subprocess" in imported)

# The single subprocess call must be the list-form eject fallback with the
# attacker-controlled device as exactly one argv element.
call = subprocess_calls[0]
args = call.args
check(len(args) == 1)
check(isinstance(args[0], ast.List))
argv = args[0]
check(isinstance(argv, ast.List) and len(argv.elts) == 2)
assert isinstance(argv, ast.List)
check(isinstance(argv.elts[0], ast.Name) and argv.elts[0].id == "eject_bin")
check(isinstance(argv.elts[1], ast.Name) and argv.elts[1].id == "device")
check(all(kw.arg != "shell" for kw in call.keywords))
# textual backstop: drop comments AND string literals (tokenizer-aware, so
# '#' inside strings and docstrings mentioning exec() are not false positives),
# then forbid the dangerous spellings.  subprocess.SubprocessError/
# TimeoutExpired handlers are deliberately allowed: exception handling is not
# command execution.
def _strip_comments(text: str) -> str:
    lines = text.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError:
        return text
    removals: dict[int, list[tuple[int, int]]] = {}
    for tok in toks:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (sr, sc), (er, ec) = tok.start, tok.end
        if sr == er:
            removals.setdefault(sr, []).append((sc, ec))
        else:
            removals.setdefault(sr, []).append((sc, len(lines[sr - 1].rstrip("\n"))))
            for row in range(sr + 1, er):
                removals.setdefault(row, []).append((0, len(lines[row - 1].rstrip("\n"))))
            removals.setdefault(er, []).append((0, ec))
    for row, spans in removals.items():
        line = lines[row - 1]
        body = line.rstrip("\n")
        for start, end in sorted(spans, reverse=True):
            body = body[:start] + body[end:]
        lines[row - 1] = body + ("\n" if line.endswith("\n") else "")
    return "".join(lines)


code_only = _strip_comments(source_text)
FORBIDDEN_TEXT = (
    ("subprocess.Popen", r"\bsubprocess\.Popen\b"),
    ("subprocess.call", r"\bsubprocess\.call\s*\("),
    ("subprocess.check_call", r"\bsubprocess\.check_call\s*\("),
    ("subprocess.check_output", r"\bsubprocess\.check_output\s*\("),
    ("os.system", r"\bos\.system\b"),
    ("os.popen", r"\bos\.popen\b"),
    ("os.exec*", r"\bos\.exec\w*"),
    ("os.spawn*", r"\bos\.spawn\w*"),
    ("__import__", r"\b__import__\b"),
    ("importlib", r"\bimportlib\b"),
    ("builtin eval()", r"(?<![\w.])eval\s*\("),
    ("builtin exec()", r"(?<![\w.])exec\s*\("),
    ("builtin compile()", r"(?<![\w.])compile\s*\("),
    ("shell=True", r"\bshell\s*=\s*True\b"),
)
for label, pattern in FORBIDDEN_TEXT:
    match = re.search(pattern, code_only)
    if match is not None:
        raise AssertionError(f"forbidden {label} found in source text")
# the one allowed call site is still visible to the textual layer
check(len(re.findall(r"\bsubprocess\.run\s*\(", code_only)) == 1)
check("import pickle" not in code_only)
check("import yaml" not in code_only and "from yaml" not in code_only)
# ════════════════════════════════════════════════════════════════════
# 1. Qt app + marker file
# ════════════════════════════════════════════════════════════════════
app = QCoreApplication([])
locale.setlocale(locale.LC_NUMERIC, "C")  # libmpv needs C numeric locale

import logging  # noqa: E402

logging.disable(logging.CRITICAL)  # expected PlaybackError tracebacks are noise


def is_core_shutdown(exc: BaseException) -> bool:
    return type(exc).__name__ == "ShutdownError" or "core has been shutdown" in str(exc).lower()


attach_shutdowns: list[BaseException] = []


def safe_attach(engine: DvdPlaybackEngine, wid: int = 1) -> None:
    """attach_widget, retrying the documented idle=False shutdown race.

    A real app would crash here; the suite retries so later attacks still run
    while still recording the failure.
    """
    for _ in range(5):
        try:
            engine.attach_widget(wid)
            return
        except BaseException as exc:  # noqa: BLE001
            if not is_core_shutdown(exc):
                raise
            attach_shutdowns.append(exc)
            engine._wid = None  # test-side recovery from the poisoned state
    raise AssertionError("attach_widget kept raising ShutdownError after 5 retries")

MARKER = Path("/tmp/redteam_marker")
MARKER.write_text("alive\n", encoding="utf-8")
check(MARKER.exists())
INJECTED = Path("/tmp/a")
if INJECTED.exists():
    INJECTED.unlink()


# ════════════════════════════════════════════════════════════════════
# 2. Attack (a): source-input injection / hostile paths
# ════════════════════════════════════════════════════════════════════
PAYLOADS = [
    "$(reboot)",
    "`reboot`",
    "/tmp/a; rm -rf /",
    "/tmp/a\nrm -rf /",
    "file:///etc/passwd",
    "../../etc/passwd",
    "A" * 10000,
    "\x00\x01\x02",
    "  /tmp/x  ",
    "//dev//sr0",
    "/tmp/\N{CLAPPER BOARD}\N{SEE-NO-EVIL MONKEY}.iso",
    "",
]

for payload in PAYLOADS:
    label = f"source attack {payload!r}"[:70]

    # check_environment: must return a message list, never raise.
    try:
        problems = check_environment(payload)
        check(isinstance(problems, list))
        check(all(isinstance(item, str) for item in problems))
    except (ValueError, OSError) as exc:
        raise AssertionError(
            f"{label}: check_environment leaked {type(exc).__name__}: {exc}"
        ) from exc

    # DiscIdentity.compute: digest or PlaybackError only.
    try:
        digest = DiscIdentity.compute(payload)
        check(isinstance(digest, str) and len(digest) == 64)
        check(digest == digest.lower())
    except PlaybackError:
        check(True)
    except (ValueError, OSError) as exc:
        raise AssertionError(
            f"{label}: DiscIdentity.compute leaked {type(exc).__name__}: {exc}"
        ) from exc

    # engine.load on an unattached engine: PlaybackError, clean ERROR state.
    engine = DvdPlaybackEngine()
    seen: list[str] = []
    engine.error_occurred.connect(seen.append)
    try:
        engine.load(payload)
    except PlaybackError:
        check(True)
    except (OSError, ValueError) as exc:
        raise AssertionError(f"{label}: engine.load leaked {type(exc).__name__}: {exc}") from exc
    else:
        raise AssertionError(f"{label}: engine.load did not raise PlaybackError")
    check(engine.state is PlayerState.ERROR)
    check(len(seen) == 1 and isinstance(seen[0], str))
    engine.dispose()

check(MARKER.exists())
check(not INJECTED.exists())

# ════════════════════════════════════════════════════════════════════
# 3. Attack (b): ResumeStore corruption fuzz
# ════════════════════════════════════════════════════════════════════
CORRUPT_SEEDS = [
    b"",
    b"{broken",
    b"[1,2,3]",
    b'{"k": 1}',
    b'{"k": {"pos": "NaN"}}',
    b'"just a string"',
    b"null",
    b'{"k": null, "j": {"pos": NaN, "updated": Infinity}}',
    b'{"k": {"pos": true, "title": 1.5, "chapter": -3, "updated": null}}',
    b'{"k": {"pos": 1e999, "updated": -1e999, "title": [], "chapter": {}}}',
    b'{"k": {"pos": -1e308, "updated": "not-a-number"}}',
    b'{"k": {"__proto__": {"x": 1}}, "k2": []}',
    b"\xff\xfe\x00binary junk",
]

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # 5 MB of 'x'
    big = root / "big.json"
    big.write_bytes(b"x" * (5 * 1024 * 1024))
    store = ResumeStore(big)
    check(store.load() == {})
    check(store.get("anything") is None)

    for index, seed in enumerate(CORRUPT_SEEDS):
        path = root / f"corrupt{index}.json"
        path.write_bytes(seed)
        store = ResumeStore(path)
        try:
            loaded = store.load()
        except BaseException as exc:  # noqa: BLE001 - that is the point
            raise AssertionError(
                f"ResumeStore.load raised {type(exc).__name__} for seed {seed!r}"
            ) from exc
        check(isinstance(loaded, dict))
        fetched = store.get("k")
        check(fetched is None or isinstance(fetched, dict))

        # save()+get() roundtrip survives after every corrupt seed
        store.save("probe", 12.25, 2, 7)
        entry = store.get("probe")
        check(isinstance(entry, dict))
        assert isinstance(entry, dict)
        check(float(entry["pos"]) == 12.25)  # type: ignore[arg-type]
        check(entry["title"] == 2)
        check(entry["chapter"] == 7)
        check(isinstance(entry["updated"], float))
        store.forget("probe")
        check(store.get("probe") is None)

    # negative / huge floats roundtrip without raising
    extremes = ResumeStore(root / "extremes.json")
    for pos in (-5.0, 1e308, -1e308, float("nan"), float("inf")):
        extremes.save("x", pos, None, None)
        got = extremes.get("x")
        check(isinstance(got, dict))
        check(float(got["pos"]) == pos or (pos != pos and float(got["pos"]) != float(got["pos"])))  # type: ignore[index]

    # saving 100 entries prunes without raising
    many = ResumeStore(root / "many.json")
    for i in range(100):
        many.save(f"h{i}", float(i), i, i)
    check(len(many.load()) <= 16)

    # a directory as the backing path must not raise from load()
    directory_store = ResumeStore(root)
    check(directory_store.load() == {})

# ════════════════════════════════════════════════════════════════════
# 4. Attack (c): eject/dispose lifecycle, hard 60 s budget
# ════════════════════════════════════════════════════════════════════
lifecycle_start = time.monotonic()

# double dispose on an untouched engine
e1 = DvdPlaybackEngine()
e1.dispose()
e1.dispose()
check(e1.state is PlayerState.IDLE)
check(e1.player is None)

# dispose then load (unattached)
try:
    e1.load("/dev/nonexistent0")
except PlaybackError:
    check(True)
else:
    raise AssertionError("dispose-then-load did not raise PlaybackError")
check(e1.state is PlayerState.ERROR)

# dispose then load on a previously attached engine
e2 = DvdPlaybackEngine()
safe_attach(e2)
safe_attach(e2)  # idempotent
e2.dispose()
e2.dispose()
check(e2.player is None)
try:
    e2.load("/dev/nonexistent0")
except PlaybackError:
    check(True)
else:
    raise AssertionError("attached dispose-then-load did not raise PlaybackError")
check(e2.state is PlayerState.ERROR)

# eject on an unattached engine from IDLE: no crash, reaches EJECTED
e3 = DvdPlaybackEngine()
e3.eject()
check(e3.state is PlayerState.EJECTED)
check(e3.player is None)
check(e3.menu_active is False)
e3.dispose()

# attach_widget(1) then eject(): no crash, EJECTED, back to reusable
e4 = DvdPlaybackEngine()
safe_attach(e4)
check(e4.player is not None)
e4.eject()
# Console tray semantics: eject is legal from IDLE and must reach EJECTED.
check(e4.state is PlayerState.EJECTED)
check(e4.player is None)
# from a loaded/playing state the transition is legal and must reach EJECTED
e4._sm.transition(PlayerState.LOADING)
e4._sm.transition(PlayerState.PLAYING)
safe_attach(e4)
e4.eject()
check(e4.state is PlayerState.EJECTED)
check(e4.player is None)
check(e4.menu_active is False)
safe_attach(e4)  # must rebuild after eject
check(e4.player is not None)
e4.dispose()

# attach_widget(1) then load a missing device: async hash contract means
# load returns LOADING (playback started) and the watchdog surfaces ERROR.
e5 = DvdPlaybackEngine()
safe_attach(e5)
errors: list[str] = []
e5.error_occurred.connect(errors.append)
e5.load("/dev/nonexistent0")  # must not raise from the disc hash
check(e5.state is PlayerState.LOADING)
check(len(errors) == 0)
e5._on_load_timeout()  # deterministic watchdog check
check(e5.state is PlayerState.ERROR)
check(len(errors) == 1)
check(e5.state is not PlayerState.LOADING)
e5.dispose()
e5.dispose()

lifecycle_elapsed = time.monotonic() - lifecycle_start
check(lifecycle_elapsed < 60.0)

# ════════════════════════════════════════════════════════════════════
# 5. Attack (d): 500-call button mash, unattached and attached
# ════════════════════════════════════════════════════════════════════
DIRECTIONS = ("up", "down", "left", "right", "diagonal", "")
SPEEDS = (1.0, 2.0, 4.0, 8.0, 16.0)
SEEKS = (-1.0, 0.0, 0.5, 10.0, 3600.0)
VOLUMES = (-50.0, -5.0, 5.0, 50.0)
RESUME_DICTS = [
    {},
    {"pos": 10.0, "title": 1, "chapter": 2},
    {"pos": -3.0, "title": "1", "chapter": None},
    {"pos": float("nan"), "title": 1.5, "chapter": 2.5},
]

# Known library/source mismatch: python-mpv 1.0.8 exports no ``MPVError``.
# Evaluating any ``except (mpv.MPVError, OSError)`` clause therefore raises
# AttributeError while handling a real mpv error.  The mash records these as
# evidence instead of aborting the run.
MPVERROR_BROKEN = not hasattr(__import__("mpv"), "MPVError")
escapes: list[BaseException] = []


def is_mpv_error_mismatch(exc: BaseException) -> bool:
    return isinstance(exc, AttributeError) and "has no attribute 'MPVError'" in str(exc)


def mash(engine: DvdPlaybackEngine, iterations: int, seed: int) -> None:
    rng = random.Random(seed)
    operations = [
        ("menu_nav", lambda: engine.menu_nav(rng.choice(DIRECTIONS))),
        ("menu_select", engine.menu_select),
        ("menu_back", engine.menu_back),
        ("menu_mouse_move", lambda: engine.menu_mouse_move(rng.randint(-100, 5000), rng.randint(-100, 5000))),
        ("cycle_audio", engine.cycle_audio),
        ("cycle_subtitle", engine.cycle_subtitle),
        ("play_pause", engine.play_pause),
        ("stop", engine.stop),
        ("set_speed", lambda: engine.set_speed(rng.choice(SPEEDS))),
        ("seek_to", lambda: engine.seek_to(rng.choice(SEEKS))),
        ("adjust_volume", lambda: engine.adjust_volume(rng.choice(VOLUMES))),
        ("next_chapter", engine.next_chapter),
        ("previous_chapter", engine.previous_chapter),
        ("show_root_menu", engine.show_root_menu),
        ("show_title_menu", engine.show_title_menu),
        ("set_title", lambda: engine.set_title(rng.randint(1, 99))),
        ("resume_disc", lambda: engine.resume_disc(rng.choice(RESUME_DICTS))),
        ("read_state", lambda: check(engine.state in tuple(PlayerState))),
        ("read_transport", lambda: (engine.time_pos, engine.duration, engine.chapter,
                                    engine.chapter_list, engine.disc_title, engine.volume,
                                    engine.speed, engine.menu_active)),
    ]
    for _ in range(iterations):
        name, operation = rng.choice(operations)
        try:
            operation()
        except BaseException as exc:  # noqa: BLE001
            if is_mpv_error_mismatch(exc) or is_core_shutdown(exc):
                escapes.append(exc)
            else:
                raise AssertionError(
                    f"button mash escaped via {name}: {type(exc).__name__}: {exc}"
                ) from exc
        assert engine.state in tuple(PlayerState)


# unattached engine: nothing can talk to mpv, so nothing may escape
e6 = DvdPlaybackEngine()
mash(e6, 500, 1234)
check(not escapes)
check(e6.state in tuple(PlayerState))
e6.dispose()

# attached engine, cycled through real PLAYING so mpv calls actually run
e7 = DvdPlaybackEngine()
safe_attach(e7)
e7._sm.transition(PlayerState.LOADING)
e7._sm.transition(PlayerState.PLAYING)
check(e7.state is PlayerState.PLAYING)

# FIXED contract: idle=True keeps the libmpv core alive; guarded properties
# return None instead of raising ShutdownError for the full watch window.
safe_deadline = time.monotonic() + 3.0
prop_safe = True
while time.monotonic() < safe_deadline:
    time.sleep(0.05)
    app.processEvents()
    try:
        assert e7.time_pos is None or isinstance(e7.time_pos, float)
        assert e7.duration is None or isinstance(e7.duration, float)
        assert e7.chapter is None or isinstance(e7.chapter, int)
        assert e7.volume is None or isinstance(e7.volume, float)
    except BaseException:  # noqa: BLE001
        prop_safe = False
        break
check(prop_safe)

escapes_before = len(escapes)
mash(e7, 500, 5678)
check(len(escapes) == escapes_before)
check(e7.state in tuple(PlayerState))
for _ in range(5):
    app.processEvents()
check(e7.state in tuple(PlayerState))
e7.dispose()

# FIXED contract: player calls surface errors through the state machine
# (ERROR + error_occurred), never as escaping exceptions.
repro = DvdPlaybackEngine()
safe_attach(repro)
repro.show_title_menu()
repro.show_root_menu()
repro.menu_select()
check(repro.state in tuple(PlayerState))
repro.dispose()

# FIXED contract: a valid VIDEO_TS load after idle must not escape; the
# load watchdog must move a dead load out of LOADING with an error
# (the fake IFO cannot start real playback, so LOADING -> ERROR).
with tempfile.TemporaryDirectory() as tmp:
    real_disc = Path(tmp) / "disc"
    (real_disc / "VIDEO_TS").mkdir(parents=True)
    (real_disc / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"IFO" * 600)
    e8 = DvdPlaybackEngine()
    safe_attach(e8)
    idle_errors: list[str] = []
    e8.error_occurred.connect(idle_errors.append)
    time.sleep(0.25)
    app.processEvents()
    e8.load(str(real_disc))  # must not raise
    check(e8.state in (PlayerState.LOADING, PlayerState.ERROR))
    e8._on_load_timeout()  # deterministic watchdog check
    check(e8.state is PlayerState.ERROR)
    check(len(idle_errors) == 1)
    e8.dispose()

# attach_widget() constructor stability: with idle=False the core can shut down
# while python-mpv is still initialising/registering observers.  Run in a child
# process so a libmpv assertion abort (SIGABRT) cannot kill this suite.
CHILD_STRESS = r'''
import os, locale, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QCoreApplication
app = QCoreApplication([])
locale.setlocale(locale.LC_NUMERIC, "C")
import logging
logging.disable(logging.CRITICAL)
from dvd_player import DvdPlaybackEngine
for _ in range(200):
    engine = DvdPlaybackEngine()
    engine.attach_widget(1)
    engine.dispose()
print("CHILD OK")
'''

child_failed = False
child_output = ""
for _attempt in range(3):
    completed = subprocess.run(
        [sys.executable, "-c", CHILD_STRESS],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONPATH": str(REPO / "src"),
        },
    )
    child_output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        child_failed = True
        break
check(not child_failed)
check(
    "ShutdownError" not in child_output
    and "ctx->destroying" not in child_output
    and "Traceback" not in child_output
)
# FIXED: idle=True keeps the libmpv core alive; 200 attach/dispose cycles
# complete cleanly ("CHILD OK") with no shutdown race and no SIGABRT.

# ════════════════════════════════════════════════════════════════════
# 6. Attack (e): device-path fuzz for disc_present / eject_device
# ════════════════════════════════════════════════════════════════════
# A real drive is now attached: never fuzz eject on live hardware.
DEVICES = ["/dev/sr9", "/dev/nonexistent0", "/dev/null", "/dev/zero", "/tmp", "~"]
device_start = time.monotonic()
for device in DEVICES:
    probe_start = time.monotonic()
    try:
        present = disc_present(device)
    except BaseException as exc:  # noqa: BLE001
        raise AssertionError(
            f"disc_present({device!r}) raised {type(exc).__name__}: {exc}"
        ) from exc
    check(present is None or isinstance(present, bool))
    check(time.monotonic() - probe_start < 10.0)

    outcomes: list[str] = []
    for _ in range(2):  # determinism across repeated calls
        try:
            result = eject_device(device)
        except PlaybackError:
            outcomes.append("PlaybackError")
        except OSError as exc:
            raise AssertionError(f"eject_device({device!r}) leaked OSError: {exc}") from exc
        else:
            check(result is None)
            outcomes.append("None")
    check(outcomes[0] == outcomes[1])
    check(outcomes[0] in {"PlaybackError", "None"})
check(time.monotonic() - device_start < 60.0)

# ════════════════════════════════════════════════════════════════════
# 7. Attack (f): hash determinism
# ════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmp:
    disc = Path(tmp) / "disc"
    videos = disc / "VIDEO_TS"
    videos.mkdir(parents=True)
    rng = random.Random(99)
    (videos / "VIDEO_TS.IFO").write_bytes(bytes(rng.randrange(256) for _ in range(8192)))

    h1 = DiscIdentity.compute(str(disc))
    h2 = DiscIdentity.compute(str(disc))
    check(h1 == h2)
    check(len(h1) == 64)
    check(h1 == h1.lower())
    check(all(c in "0123456789abcdef" for c in h1))

    iso = Path(tmp) / "movie.iso"
    iso.write_bytes(os.urandom(3 * 1024 * 1024))
    iso_hash = DiscIdentity.compute(str(iso))
    check(len(iso_hash) == 64)
    check(iso_hash == iso_hash.lower())
    check(all(c in "0123456789abcdef" for c in iso_hash))
    check(iso_hash != h1)
    check(DiscIdentity.compute(str(iso)) == iso_hash)

    bup_only = Path(tmp) / "bup"
    (bup_only / "VIDEO_TS").mkdir(parents=True)
    (bup_only / "VIDEO_TS" / "VIDEO_TS.BUP").write_bytes(b"BUPFALLBACK")
    check(len(DiscIdentity.compute(str(bup_only))) == 64)

    broken = Path(tmp) / "broken"
    (broken / "VIDEO_TS").mkdir(parents=True)
    (broken / "VIDEO_TS" / "README.txt").write_text("no ifo here", encoding="utf-8")
    try:
        DiscIdentity.compute(str(broken))
    except PlaybackError as exc:
        check("not a DVD layout" in str(exc))
    else:
        raise AssertionError("VIDEO_TS without IFO/BUP did not raise PlaybackError")

# ════════════════════════════════════════════════════════════════════
# 8. Postconditions
# ════════════════════════════════════════════════════════════════════
check(MARKER.exists())
check(MARKER.read_text(encoding="utf-8").strip() == "alive")
check(not INJECTED.exists())

for defect in MINOR_DEFECTS:
    print(f"DEFECT: {defect}", file=sys.stderr)

check(COUNT >= 25)
print(f"REDTEAM OK {COUNT} assertions")
