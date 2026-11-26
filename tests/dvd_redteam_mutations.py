"""Committed proof for audit clauses A1-A8: every mutant must go RED.

Run:  QT_QPA_PLATFORM=offscreen uv run python tests/dvd_redteam_mutations.py
Exit code is non-zero if any mutant survives.  Mutants are built in a temp
directory; the repository is never modified.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUITE = REPO / "tests" / "dvd_redteam.py"
MARKER = "# 1. Qt app + marker file"


def audit_source() -> str:
    text = SUITE.read_text(encoding="utf-8")
    return text.split(MARKER)[0]


def _append(root: Path, relative: str, extra: str) -> None:
    path = root / "src" / "dvd_player" / relative
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


def a1_symlink_dir(root: Path, audit: str) -> str:
    outside = root / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_text(
        'import os\n\n\ndef evil() -> int:\n    return os.system("true")\n',
        encoding="utf-8",
    )
    (root / "src" / "dvd_player" / "evil").symlink_to(outside, target_is_directory=True)
    return audit


def a2_ctypes_pydll(root: Path, audit: str) -> str:
    _append(root, "hardware.py", '\n\ndef _evil() -> object:\n    return ctypes.PyDLL("libc.so.6")\n')
    return audit


def a2_posix_system(root: Path, audit: str) -> str:
    _append(
        root, "hardware.py",
        '\n\ndef _evil() -> int:\n    import posix\n'
        '    return posix.system("true")\n',
    )
    return audit


def a2_loop_subprocess_exec(root: Path, audit: str) -> str:
    _append(
        root, "hardware.py",
        '\n\ndef _evil(loop: object) -> object:\n'
        '    return loop.subprocess_exec(None, "/bin/true")\n',
    )
    return audit


def a2_loop_subprocess_shell(root: Path, audit: str) -> str:
    _append(
        root, "hardware.py",
        '\n\ndef _evil(loop: object) -> object:\n'
        '    return loop.subprocess_shell(None, "true")\n',
    )
    return audit


def a3_subscript_alias(root: Path, audit: str) -> str:
    _append(
        root, "hardware.py",
        '\n\n_r = [_subprocess_run][0]\n\n\ndef _evil(argv: list[str]) -> object:\n'
        "    return _r(argv)\n",
    )
    return audit


def a4_shell_false(root: Path, audit: str) -> str:
    path = root / "src" / "dvd_player" / "hardware.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "                check=False,\n                capture_output=True,",
        "                shell=False,\n                check=False,\n                capture_output=True,",
        1,
    )
    path.write_text(text, encoding="utf-8")
    return audit


def a5_extra_sys_modules(root: Path, audit: str) -> str:
    _append(
        root, "__init__.py",
        '\n\ndef _extra_loader() -> object:\n    return sys.modules.get("x")\n',
    )
    audit += "\n"
    return audit


def a6_no_coverage(root: Path, audit: str) -> str:
    path = root / "src" / "dvd_player" / "hardware.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '    eject_bin = shutil.which("eject")',
        '    if sys.platform == sys.platform:\n        return\n'
        '    eject_bin = shutil.which("eject")',
        1,
    )
    path.write_text(text, encoding="utf-8")
    return audit


def a7_charter_tamper(root: Path, audit: str) -> str:
    return audit.replace(
        "# This audit is a tripwire, not a sandbox:",
        "# This audit is an informal smoke check:",
        1,
    )


def a8_unresolved(root: Path, audit: str) -> str:
    _append(
        root, "hardware.py",
        '\n\ndef _evil() -> object:\n    return spawn_process(["/bin/true"])\n',
    )
    return audit


EXPECTED_MUTANTS = ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8")
MUTANTS = [
    ("A1", a1_symlink_dir),
    ("A2", a2_ctypes_pydll),
    ("A2", a2_posix_system),
    ("A2", a2_loop_subprocess_exec),
    ("A2", a2_loop_subprocess_shell),
    ("A3", a3_subscript_alias),
    ("A4", a4_shell_false),
    ("A5", a5_extra_sys_modules),
    ("A6", a6_no_coverage),
    ("A7", a7_charter_tamper),
    ("A8", a8_unresolved),
]


def run_mutant(clause: str, builder) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix=f"redteam_{clause}_") as tmp:
        root = Path(tmp)
        shutil.copytree(REPO / "src", root / "src", symlinks=False)
        shutil.rmtree(root / "src" / "dvd_player" / "__pycache__", ignore_errors=True)
        (root / "tests").mkdir()
        audit = builder(root, audit_source())
        audit_path = root / "tests" / "dvd_redteam.py"
        audit_path.write_text(audit, encoding="utf-8")
        env = dict(os.environ)
        env.pop("AUDIT_SOURCE", None)
        env["QT_QPA_PLATFORM"] = "offscreen"
        completed = subprocess.run(
            [sys.executable, str(audit_path)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        red = completed.returncode != 0 and "AssertionError" in output
        return red, output[-200:]


def main() -> int:
    # A2 carries several fixture families (ctypes, posix, event-loop), so ids
    # may repeat; the pinned manifest is the ordered set of unique ids.
    ids = list(dict.fromkeys(clause for clause, _ in MUTANTS))
    if ids != list(EXPECTED_MUTANTS):
        print(f"MANIFEST MISMATCH: {ids} != {list(EXPECTED_MUTANTS)}")
        return 1
    if len(EXPECTED_MUTANTS) != 8:
        print(f"MANIFEST PIN BROKEN: {len(EXPECTED_MUTANTS)} ids, expected 8")
        return 1
    # A no-op mutant must not be classified RED: if it is, the harness itself
    # or the baseline is broken and the manifest proof is meaningless.
    noop_red, _ = run_mutant("NOOP", lambda root, audit: audit)
    if noop_red:
        print("NO-OP MUTANT CLASSIFIED RED - manifest guard broken")
        return 1
    survived: list[str] = []
    for clause, builder in MUTANTS:
        red, tail = run_mutant(clause, builder)
        label = f"{clause}/{builder.__name__}"
        print(f"{label}: {'RED' if red else 'SURVIVED'}")
        if not red:
            survived.append(label)
            print(tail)
    print(
        f"MUTANTS {len(MUTANTS) - len(survived)}/{len(MUTANTS)} RED "
        f"({len(EXPECTED_MUTANTS)} pinned ids, {len(MUTANTS)} fixtures)"
    )
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
