"""Xbox 360-style DVD player for Arch Linux."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import struct
import time
from pathlib import Path

from .hardware import BLKGETSIZE64
from .state import RESUME_FILE, RESUME_MAX_ENTRIES, PlaybackError

# SECTION 2 — Disc identity & resume store
# ════════════════════════════════════════════════════════════════════

# SPEC for SECTION 2 (DiscIdentity + ResumeStore) — contract, do not edit:
#   class DiscIdentity:
#       @staticmethod compute(source: str) -> str   (lowercase hex sha256)
#       @staticmethod label(source: str) -> str     (PVD volume id ONLY)
#       * block device (/dev/sr*, /dev/cdrom, /dev/dvd via stat.S_ISBLK):
#         read ISO9660 PVD at byte offset 32768 (1 sector, 2048 bytes);
#         volume id = bytes 40:72 decoded utf-8, stripped of NULs/spaces;
#         size via ioctl BLKGETSIZE64 (struct 'Q'); sample = 256 KiB read at
#         offset 524288 (best effort, b'' on failure). Digest over
#         volume_id_bytes | str(size) | sample.
#       * regular file: stream sha256 of the whole file, 1 MiB chunks.
#       * directory: hash VIDEO_TS/VIDEO_TS.IFO (fallback VIDEO_TS.BUP),
#         1 MiB chunks; PlaybackError if neither exists ("not a DVD layout").
#       * missing/unreadable source -> PlaybackError with actionable message.
#       * label(): PVD-only read at offset 32768 (block device or ISO file);
#         raises PlaybackError when the source/PVD cannot be read.
#   class ResumeStore:
#       __init__(self, path: Path = RESUME_FILE)
#       load(self) -> dict   ({} when file missing/corrupt — never raise)
#       get(self, disc_hash: str) -> Optional[dict]
#       save(self, disc_hash: str, pos: float, title: Optional[int],
#            chapter: Optional[int], aid: Optional[int] = None,
#            sid: Optional[int] = None) -> None   (upsert, set "updated" to
#            time.time(), prune to RESUME_MAX_ENTRIES by newest "updated";
#            atomic write: tmp file in same dir + os.replace; mkdir parents)
#       forget(self, disc_hash: str) -> None
#   Entry schema v2: {"pos": float, "title": int|None, "chapter": int|None,
#                  "aid": int|None, "sid": int|None, "updated": float}
#   Legacy entries without aid/sid load unchanged; malformed aid/sid values
#   are coerced to None on load. pos is sanitized by the consumer
#   (NaN/±inf/negative -> 0.0); the store itself only persists finite floats.
# ── [AGENT1: SECTION 2 BEGIN] ──
def _hash_stream(path: Path) -> str:
    """Return the sha256 hex digest of a file, reading it in 1 MiB chunks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise PlaybackError(f"cannot read {str(path)!r}: {exc}") from exc
    return digest.hexdigest()


class DiscIdentity:
    """Stable per-disc identifiers used to key resume state."""

    @staticmethod
    def compute(source: str) -> str:
        """Return a lowercase sha256 hex digest for a disc device, ISO or folder."""
        path = Path(source)
        try:
            info = path.stat()
        except (OSError, ValueError) as exc:
            raise PlaybackError(
                f"cannot access disc source {source!r}: {exc}"
            ) from exc
        if stat.S_ISBLK(info.st_mode):
            return DiscIdentity._compute_block(path)
        if stat.S_ISDIR(info.st_mode):
            return DiscIdentity._compute_directory(path)
        if stat.S_ISREG(info.st_mode):
            return _hash_stream(path)
        raise PlaybackError(f"unsupported disc source {source!r}: not a device, file or directory")

    @staticmethod
    def _compute_block(path: Path) -> str:
        """Digest volume id, device size and a data sample of a block device."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise PlaybackError(
                f"cannot open disc device {str(path)!r}: {exc}"
            ) from exc
        try:
            size = struct.unpack(
                "Q", fcntl.ioctl(fd, BLKGETSIZE64, struct.pack("Q", 0))
            )[0]
            volume_id = DiscIdentity._read_pvd(fd)
            try:
                sample = os.pread(fd, 256 * 1024, 524288)
            except OSError:
                sample = b""
        except OSError as exc:
            raise PlaybackError(
                f"cannot read disc device {str(path)!r}: {exc}"
            ) from exc
        finally:
            os.close(fd)
        digest = hashlib.sha256()
        digest.update(volume_id)
        digest.update(str(size).encode("ascii"))
        digest.update(sample)
        return digest.hexdigest()

    @staticmethod
    def _decode_volume_id(pvd: bytes) -> bytes:
        """Decode PVD bytes 40:72 into a stripped UTF-8 volume-id byte string."""
        raw = pvd[40:72].decode("utf-8", "replace").replace("\x00", "").strip()
        return raw.encode("utf-8")

    @staticmethod
    def _read_pvd(fd: int) -> bytes:
        """Return the ISO9660 volume id (PVD bytes 40:72), stripped of NUL/spaces."""
        try:
            pvd = os.pread(fd, 2048, 32768)
        except OSError:
            return b""
        return DiscIdentity._decode_volume_id(pvd)

    @staticmethod
    def label(source: str) -> str:
        """Return the disc's PVD volume id (label), or raise PlaybackError."""
        path = Path(source)
        try:
            info = path.stat()
        except (OSError, ValueError) as exc:
            raise PlaybackError(
                f"cannot access disc source {source!r}: {exc}"
            ) from exc
        if not (stat.S_ISBLK(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise PlaybackError(
                f"cannot read volume label from {source!r}: "
                "not a block device or ISO image"
            )
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise PlaybackError(
                f"cannot read volume label from {str(path)!r}: {exc}"
            ) from exc
        try:
            pvd = os.pread(fd, 2048, 32768)
        except OSError as exc:
            raise PlaybackError(
                f"cannot read volume label from {str(path)!r}: {exc}"
            ) from exc
        finally:
            os.close(fd)
        if len(pvd) < 72:
            raise PlaybackError(
                f"cannot read volume label from {str(path)!r}: PVD unreadable"
            )
        return DiscIdentity._decode_volume_id(pvd).decode("utf-8", "replace")

    @staticmethod
    def _compute_directory(path: Path) -> str:
        """Hash VIDEO_TS/VIDEO_TS.IFO, falling back to VIDEO_TS.BUP."""
        video_ts = path / "VIDEO_TS"
        for name in ("VIDEO_TS.IFO", "VIDEO_TS.BUP"):
            candidate = video_ts / name
            if candidate.is_file():
                return _hash_stream(candidate)
        raise PlaybackError(
            f"not a DVD layout: {str(path)!r} has no VIDEO_TS/VIDEO_TS.IFO"
        )


def _entry_updated(entry: dict[str, object]) -> float:
    """Return an entry's ``updated`` timestamp as float (0.0 when absent)."""
    value = entry.get("updated")
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _coerce_track_id(value: object) -> int | None:
    """Coerce a stored track id to int, rejecting bools and malformed values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


class ResumeStore:
    """Persist per-disc resume positions in a small JSON file."""

    def __init__(self, path: Path | str = RESUME_FILE) -> None:
        """Use ``path`` as the backing JSON file."""
        self._path = Path(path)

    def load(self) -> dict[str, dict[str, object]]:
        """Return all entries; ``{}`` when the file is missing or corrupt."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        entries: dict[str, dict[str, object]] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            entry = dict(value)
            # v2 fields: legacy entries without them stay untouched; malformed
            # values degrade to None instead of poisoning the schema.
            for name in ("aid", "sid"):
                if name in entry:
                    entry[name] = _coerce_track_id(entry[name])
            entries[str(key)] = entry
        return entries

    def get(self, disc_hash: str) -> dict[str, object] | None:
        """Return the stored entry for ``disc_hash``, if any."""
        return self.load().get(disc_hash)

    def save(
        self,
        disc_hash: str,
        pos: float,
        title: int | None,
        chapter: int | None,
        aid: int | None = None,
        sid: int | None = None,
    ) -> None:
        """Upsert an entry, stamp it, and prune the oldest entries."""
        entries = self.load()
        entries[disc_hash] = {
            "pos": float(pos),
            "title": title,
            "chapter": chapter,
            "aid": _coerce_track_id(aid),
            "sid": _coerce_track_id(sid),
            "updated": time.time(),
        }
        if len(entries) > RESUME_MAX_ENTRIES:
            newest = sorted(
                entries.items(), key=lambda item: _entry_updated(item[1]), reverse=True
            )
            entries = dict(newest[:RESUME_MAX_ENTRIES])
        self._write(entries)

    def forget(self, disc_hash: str) -> None:
        """Drop the entry for ``disc_hash`` (no-op when absent)."""
        entries = self.load()
        if entries.pop(disc_hash, None) is None:
            return
        self._write(entries)

    def _write(self, entries: dict[str, dict[str, object]]) -> None:
        """Atomically replace the backing file with ``entries``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(
                json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
# ── [AGENT1: SECTION 2 END] ──

# ════════════════════════════════════════════════════════════════════
