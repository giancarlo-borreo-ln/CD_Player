#!/usr/bin/env python3
"""Fetch Windows runtime libraries pinned by scripts/deps-manifest.json.

Standard library only. Every download is HTTPS, streamed to a temporary file
next to its destination, checked against the pinned SHA-256 and only then
renamed into place. A mismatch deletes the partial file and aborts that
entry. Entries without a hash are marked "unverified" and are refused.

    python3 scripts/fetch_deps.py --yes
    python3 scripts/fetch_deps.py --verify-only --offline ./deps
    python3 scripts/fetch_deps.py --offline ./deps --yes
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "scripts" / "deps-manifest.json"
MANIFEST_ENV = "DVD_PLAYER_DEPS_MANIFEST"
CHUNK = 1024 * 256
TIMEOUT = 60


class DepError(RuntimeError):
    """A single dependency could not be fetched or verified."""


def manifest_path() -> Path:
    override = os.environ.get(MANIFEST_ENV)
    return Path(override) if override else DEFAULT_MANIFEST


def arch_key() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86", "i386", "i686"):
        arch = "x86"
    else:
        arch = machine or "unknown"
    return f"windows-{arch}"


def lib_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "dvd-player" / "lib"


def load_manifest(path: Path) -> dict:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DepError(f"cannot read manifest {path}: {error}") from error
    except ValueError as error:
        raise DepError(f"manifest {path} is not valid JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("deps"), dict):
        raise DepError(f"manifest {path} has no 'deps' object")
    return data


def entries_for(data: dict) -> list[dict]:
    key = arch_key()
    deps = data["deps"].get(key)
    if not isinstance(deps, list) or not deps:
        raise DepError(f"manifest has no dependencies for {key}")
    return deps


def entry_name(entry: dict) -> str:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise DepError("manifest entry is missing 'name'")
    return name


def entry_url(entry: dict) -> str:
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise DepError(f"{entry_name(entry)}: manifest entry is missing 'url'")
    return url


def require_https(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DepError(
            f"refusing non-HTTPS URL {url!r}; only https:// downloads are allowed"
        )


def pinned_hash(entry: dict) -> str | None:
    value = entry.get("sha256")
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            return None
        return value.lower()
    return None


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_HTTPSOnlyRedirect)


def download(url: str, target: Path, expected: str) -> None:
    """Stream url to target via a verified temporary file (atomic rename)."""
    require_https(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".part-", dir=str(target.parent))
    temp = Path(temp_name)
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dvd-player-installer"})
        with _opener().open(request, timeout=TIMEOUT) as response:
            length = response.headers.get("Content-Length")
            expected_length = int(length) if length and length.isdigit() else None
            received = 0
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if expected_length is not None and received != expected_length:
            raise DepError(
                f"incomplete download: got {received} of {expected_length} bytes"
            )
        actual = digest.hexdigest()
        if actual != expected:
            raise DepError(
                f"checksum mismatch: expected {expected}, got {actual}; "
                "partial file deleted, nothing was installed"
            )
        os.replace(temp, target)
    except urllib.error.URLError as error:
        raise DepError(f"download failed: {error.reason}") from error
    except OSError as error:
        raise DepError(f"download failed: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _find_7z() -> str | None:
    for name in ("7z", "7za", "7zr", "bsdtar", "tar"):
        found = shutil.which(name)
        if found:
            return found
    return None


def extract_member(archive: Path, member: str, target: Path) -> None:
    """Extract one member from .zip (stdlib) or .7z (7z/bsdtar/tar)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".part-", dir=str(target.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        with tempfile.TemporaryDirectory(prefix=".extract-") as work_name:
            work = Path(work_name)
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as bundle:
                    names = [
                        n
                        for n in bundle.namelist()
                        if n.replace("\\", "/").endswith("/" + member) or n == member
                    ]
                    if not names:
                        raise DepError(f"{member} is not inside {archive.name}")
                    with bundle.open(names[0]) as source, temp.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
            else:
                tool = _find_7z()
                if tool is None:
                    raise DepError(
                        f"{archive.suffix} extraction needs 7z (or bsdtar/tar) on PATH; "
                        "install it or use --offline with the extracted file"
                    )
                if Path(tool).name in ("bsdtar", "tar"):
                    argv = [tool, "-xf", str(archive), "-C", str(work), member]
                else:
                    argv = [tool, "e", "-y", "-o" + str(work), str(archive), member]
                result = subprocess.run(argv, check=False, capture_output=True, text=True)
                if result.returncode != 0:
                    raise DepError(
                        "extraction failed: "
                        + (result.stderr.strip() or result.stdout.strip())
                    )
                matches = list(work.rglob(member))
                if not matches:
                    raise DepError(f"{member} is not inside {archive.name}")
                shutil.copyfile(matches[0], temp)
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def install_entry(entry: dict, source: Path, target: Path) -> None:
    member = entry.get("member")
    if isinstance(member, str) and member:
        extract_member(source, member, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".part-", dir=str(target.parent))
        os.close(fd)
        temp = Path(temp_name)
        try:
            shutil.copyfile(source, temp)
            os.replace(temp, target)
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass


def offline_source(entry: dict, directory: Path) -> Path | None:
    name = entry_name(entry)
    candidates = [directory / name]
    url = entry.get("url")
    if isinstance(url, str):
        candidates.append(directory / Path(urllib.parse.urlsplit(url).path).name)
    member = entry.get("member")
    if isinstance(member, str):
        candidates.append(directory / member)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def verify_offline(entries: list[dict], directory: Path) -> int:
    failures = 0
    for entry in entries:
        name = entry_name(entry)
        expected = pinned_hash(entry)
        if expected is None:
            print(f"[fail] {name}: unverified entry (no pinned sha256)", file=sys.stderr)
            failures += 1
            continue
        source = offline_source(entry, directory)
        if source is None:
            print(f"[fail] {name}: not found in {directory}", file=sys.stderr)
            failures += 1
            continue
        actual = hash_file(source)
        if actual != expected:
            print(f"[fail] {name}: checksum mismatch in {source}", file=sys.stderr)
            failures += 1
        else:
            print(f"[ok]   {name}: {actual}")
    return 1 if failures else 0


def consent(assume_yes: bool, message: str) -> bool:
    if assume_yes:
        print(f"[consent] --yes: {message}")
        return True
    if sys.stdin is None or not sys.stdin.isatty():
        print(
            "[refuse] no terminal for consent; re-run with --yes to download dependencies",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("[refuse] not confirmed; nothing was downloaded", file=sys.stderr)
        return False
    return True


def fetch_all(entries: list[dict], assume_yes: bool, offline: Path | None) -> int:
    if not consent(
        assume_yes,
        "download pinned Windows runtime libraries into %LOCALAPPDATA%\\dvd-player\\lib?",
    ):
        return 1
    destination = lib_dir()
    failures = 0
    for entry in entries:
        try:
            name = entry_name(entry)
            target = destination / name
            expected = pinned_hash(entry)
            if expected is None:
                raise DepError(
                    "entry is unverified (no pinned sha256); refusing to install "
                    "unverified bytes. Run --verify-only, then add the hash to "
                    "scripts/deps-manifest.json or use a verified --offline copy"
                )
            if offline is not None:
                source = offline_source(entry, offline)
                if source is None:
                    raise DepError(f"not found in offline directory {offline}")
                actual = hash_file(source)
                if actual != expected:
                    raise DepError(f"checksum mismatch in {source}")
                install_entry(entry, source, target)
                print(f"[ok]   {name}: verified from {source}")
                continue
            url = entry_url(entry)
            print(f"[fetch] {name} <- {url}")
            archive_member = entry.get("member")
            if isinstance(archive_member, str) and archive_member:
                suffix = Path(urllib.parse.urlsplit(url).path).suffix or ".download"
                fd, temp_name = tempfile.mkstemp(prefix=".archive-", suffix=suffix)
                os.close(fd)
                temp = Path(temp_name)
                try:
                    download(url, temp, expected)
                    install_entry(entry, temp, target)
                finally:
                    try:
                        if temp.exists():
                            temp.unlink()
                    except OSError:
                        pass
            else:
                download(url, target, expected)
            print(f"[ok]   {name}: sha256 verified -> {target}")
        except DepError as error:
            required = entry.get("required", True)
            label = "[fail]" if required else "[warn]"
            print(f"{label} {entry.get('name', '?')}: {error}", file=sys.stderr)
            if required:
                failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_deps.py",
        description=(
            "Download Windows runtime libraries listed in scripts/deps-manifest.json, "
            "verify each pinned SHA-256, then install atomically. HTTPS only."
        ),
    )
    parser.add_argument("--yes", action="store_true", help="consent to downloads unattended")
    parser.add_argument(
        "--offline",
        metavar="DIR",
        default=None,
        help="use already-downloaded files from DIR; never touches the network",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check hashes without downloading or installing anything",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help=f"manifest path (default: {DEFAULT_MANIFEST}; env {MANIFEST_ENV})",
    )
    args = parser.parse_args(argv)

    offline = Path(args.offline).expanduser().resolve() if args.offline else None
    path = Path(args.manifest).expanduser() if args.manifest else manifest_path()
    try:
        entries = entries_for(load_manifest(path))
    except DepError as error:
        print(f"[fail] {error}", file=sys.stderr)
        return 1

    if args.verify_only:
        if offline is not None:
            return verify_offline(entries, offline)
        bad = 0
        for entry in entries:
            name = entry_name(entry)
            expected = pinned_hash(entry)
            if expected is None:
                print(f"[unverified] {name}: no pinned sha256")
                bad += 1
            else:
                print(f"[pinned] {name}: {expected}")
        return 1 if bad else 0

    return fetch_all(entries, args.yes, offline)


if __name__ == "__main__":
    sys.exit(main())
