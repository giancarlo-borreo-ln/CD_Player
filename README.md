# DVD Player — Xbox 360-style DVD player for Arch Linux

Play physical commercial DVDs — interactive menus, chapters, audio tracks and
subtitles — with the exact look and feel of the **Xbox 360 DVD Player**:
translucent OSD transport bar, D-pad menu navigation, console auto-boot and
per-disc resume state.

A modern `src/`-layout Python package (`src/dvd_player/`: PyQt6 GUI + libmpv
`dvdnav://` backend), packaged with `uv` and hatchling.

> Built by a multi-agent pipeline — see [docs/AGENTS.md](docs/AGENTS.md) for
> the agent roster, activation protocol, and verification ledger.

---

## Features

- **Console auto-boot** — insert a disc and the app boots it straight into the
  DVD's first-play sequence (warnings → trailers → root menu), like a 360.
  Disc present at launch auto-boots; discs inserted while the app is running
  are detected within ~2 s (console tray-close behavior).
- **Interactive DVD menus** — full mouse support (move/click) plus Xbox 360
  D-pad navigation via keyboard or gamepad.
- **Xbox 360-style OSD** — translucent transport bar that slides up on any
  movement or keypress and auto-hides after 4 s: Play/Pause, Stop, FF/RW
  (2×, 4×, 8×, 16×), Previous/Next chapter, Audio track, Subtitle track,
  Root menu, Eject. Hover highlights in Xbox green; the active audio/subtitle
  languages are shown right on the bar (`AUD en · SUB fr`).
- **Resume state** — playback position is remembered per disc; re-inserting
  prompts "Resume playback from mm:ss?" / start from beginning.
- **Language switching** — `I` cycles audio languages, `O` cycles subtitles
  (or click `AUD` / `SUB` on the OSD).
- **Fullscreen** — `Ctrl+F`.
- Sources: physical DVD drives (`/dev/sr0`), `.iso` images, or a `VIDEO_TS`
  folder (input bar, drag-free).

## Requirements (Arch Linux)

```sh
sudo pacman -S --needed mpv libdvdnav libdvdread libdvdcss python-pyqt6 uv
```

| Package | Purpose |
|---|---|
| `mpv` | libmpv: playback + `dvdnav://` demuxer |
| `libdvdnav` / `libdvdread` | DVD VM (menus, navigation) |
| `libdvdcss` | CSS decryption for commercial (encrypted) discs |
| `python-pyqt6` | GUI framework (Wayland-compatible via XWayland) |
| `uv` | project environment (Python 3.12+) |

`python-mpv` and the remaining Python deps are installed by `uv` from
`pyproject.toml`. Optional gamepad support: `uv sync --extra gamepad`
(installs the `inputs` package).

## Install (cross-platform)

One standard-library script checks Python, `uv`, the project environment,
imports and native libraries, then runs a per-OS setup branch. Pick the
launcher for your OS — it refuses to run on the wrong system:

```sh
git clone git@github.com:giancarlo-borreo-ln/CD_Player.git
cd CD_Player
./install-linux.sh      # Linux
./install-mac.sh        # macOS
install-windows.ps1     # Windows (PowerShell)
```

`install-windows.cmd` is a thin wrapper around the native
`install-windows.ps1`, and a compiled `install-windows.exe` is preferred when
present. From a PowerShell prompt the one-liner is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

`-ExecutionPolicy Bypass` applies only to that PowerShell process, so no
machine-wide policy or administrator rights are needed. Pass `-Ask`, `-Yes`,
`-Check`, `-Uninstall` or `-Purge`; cmd-style `--uninstall` flags forwarded by
the `.cmd` are accepted too.

Each launcher delegates to `scripts/install.py --platform <os> "$@"`, so extra
flags pass through, for example `./install-linux.sh --no-sync`. You can also
call the script directly:

```sh
python3 scripts/install.py            # detect the OS + `uv sync`
python3 scripts/install.py --no-sync  # verify an existing environment
python3 scripts/install.py --ask      # prompt before installing anything
python3 scripts/install.py --dvdcss PATH  # Windows: trust a local libdvdcss-2.dll
uv run dvd-player                     # auto-boots any disc in the drive
uv run dvd-player /path/to/movie.iso
uv run dvd-player /path/to/VIDEO_TS
uv run python -m dvd_player           # equivalent module entry point
```

Missing bootstrap dependencies (**Python 3.12+**, `uv` and `mpv`/libmpv) are
installed unattended by default, so double-clicking or piping the installer
works without answers. Pass `--ask` to be prompted before each install; `--yes`
is kept as a no-op alias. Uninstall and `--purge` never install anything.

Wrong-OS launchers exit `2` with a message naming the right one.

### Where the player looks for a disc

After the dependency checks, `scripts/install.py` prints a **Disc locations**
section: the optical candidates for your OS (with `disc ready` / `empty` /
`unknown`), and any playable `VIDEO_TS` folders or `.iso` files found
non-recursively in the current folder, `~/Videos`, `~/Downloads` and
`~/Desktop` (the listing is capped, with a count of hidden entries).

| OS | Optical candidates (default lookups) | Disc state probe | Other playable sources |
|---|---|---|---|
| Linux | existing `/dev/sr*`, `/dev/cdrom`, `/dev/dvd` nodes (deduplicated; block devices preferred) | `CDROM_DRIVE_STATUS` ioctl → ready / empty / unknown | `VIDEO_TS` folder (or its parent) and `*.iso` |
| macOS | `/dev/disk*` whole disks corroborated by `drutil status`; missing/failing `drutil` reports **unknown** (never a failure) | mount table | `VIDEO_TS` folder (or its parent) and `*.iso` |
| Windows | drive letters from `GetLogicalDrives` where `GetDriveTypeW == DRIVE_CDROM` (5) | `VIDEO_TS` folder on the drive root | `VIDEO_TS` folder (or its parent) and `*.iso` |

Probing is best-effort with a strict per-tool timeout (3 s for `drutil`,
5 s for other helpers; a hung tool reports `unknown` rather than blocking):
when no drive or probe tool exists the script says `unknown`/`none found`
with the remedy and keeps its exit code — it never crashes. Optical and
playable-source listings are each capped at 10 entries with a hidden count.
`--disc` resolves symlinks first, so a symlinked `VIDEO_TS` folder or `.iso`
is classified by its target (the printed line shows both paths). Use `--disc PATH` to check one source before launching:

```sh
python3 scripts/install.py --disc /dev/sr0             # optical device
python3 scripts/install.py --disc "~/Movies/My DVD"   # folder containing VIDEO_TS
python3 scripts/install.py --disc "~/Movies/My DVD/VIDEO_TS"
python3 scripts/install.py --disc ./movie.iso         # ISO image
```

`PATH` accepts `~`, quoted spaces and relative paths (the same forms the app
accepts). Success prints `will play: <kind> <path>` where kind is `device`,
`dvd-folder` or `iso`; an invalid path prints the reason and exits `1`.

### Windows: building `install-windows.exe`

`install-windows.exe` is a release artifact and is not committed. It must be
built **on Windows** — PyInstaller cannot cross-compile from Linux or macOS:

```bat
py -3 -m pip install pyinstaller
py -3 scripts\build_windows_exe.py
```

The result lands in `dist\install-windows.exe`; copy it beside
`install-windows.cmd`, which prefers the exe and otherwise delegates to
`install-windows.ps1`. The PowerShell installer locates a repo-relative Python
3.12+ (`py -3`, `python` or `%LOCALAPPDATA%\Programs\Python\Python31{2,3}`) and
installs a missing one with winget unattended; add `-Ask` to be prompted
first. The exe bakes in `--platform windows --pause`, so a double-clicked
window stays open.

| Platform | Branch behavior | Status |
|---|---|---|
| Linux | Checks the `optical` group and `sg` module, prints distro packages and the desktop entry | **Full** support |
| macOS | Checks Homebrew, prints `brew install mpv libdvdnav libdvdcss` and the `DYLD_FALLBACK_LIBRARY_PATH` hint | **Experimental** |
| Windows | Checks the Python/uv install and locates `libmpv-2.dll` and `dvdnav.dll` via `ctypes.util.find_library` and known DLL directories; scans the DLL for `DVDCSS_METHOD` | **Experimental** |

> **Experimental support (macOS/Windows):** ISO and `VIDEO_TS` folder playback
> work; the optical device/disc layer (drive discovery, eject, auto-boot on
> insert) is Linux-specific and is still being ported. On Windows the pinned
> `libmpv-2.dll` statically bundles libdvdcss, so encrypted/commercial DVDs
> are supported with libmpv alone; the installer confirms this by scanning the
> DLL for `DVDCSS_METHOD`. If you use a different libmpv, pass
> `--dvdcss PATH` to supply your own trusted `libdvdcss-2.dll`. Hardware
> combinations are not exhaustively tested, so treat Windows drive support as
> experimental.

Exit code is `0` when the player is ready and `1` when a hard dependency is
missing or `--disc` is invalid; `2` is reserved for wrong-platform launchers
and usage errors.

## Development

The package lives in `src/dvd_player/` (module boundaries mirror the SECTION
architecture — see `docs/AGENTS.md`). Install dev tools and check the tree
from the repo root:

```sh
uv sync --extra dev
uv run ruff check src/dvd_player
uv run mypy --strict src/dvd_player
```

The acceptance suites are plain scripts under `tests/`; run each from the
repo root (offscreen Qt, no display needed):

```sh
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_backend_smoke.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_verification.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_formal_exam.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_redteam.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_gui_smoke.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_verification_gui.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_blackbox.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_blackbox_async.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_blackbox_reset.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_loki_sync.py
QT_QPA_PLATFORM=offscreen uv run python tests/dvd_uat_driver.py
```

## Install as a desktop app (Rofi / app launcher)

```sh
uv tool install .                                          # installs `dvd-player` into ~/.local/bin
cp packaging/dvd-player.desktop ~/.local/share/applications/
uv run python packaging/make_icon.py                       # writes the app icon
gtk-update-icon-cache -f ~/.local/share/icons/hicolor 2>/dev/null || true
```

You can now launch it from Rofi by typing `dvd-player` (it appears under
AudioVideo). Updates: `uv tool install --force .`

## Device permissions

The app reads block devices and (for eject) uses raw ioctls, so your user
must be able to access the drive:

```sh
sudo usermod -aG optical $USER            # then log out and back in
```

The `sg` kernel module (SCSI passthrough) should be loaded for some drives:

```sh
sudo modprobe sg                          # load now
echo "sg" | sudo tee /etc/modules-load.d/sg.conf   # persist across reboots
```

If anything is missing, the app tells you exactly what when you load a disc —
no silent failures.

## Xbox 360 keybindings

| Controller | Key | Action |
|---|---|---|
| D-pad | ↑ ↓ ← → | Move DVD menu highlight |
| A | `Enter` | Menu: select · Playing: play/pause |
| Start | `Space` | Play/Pause |
| B | `Backspace` | Menu back |
| X | `X` | Root menu |
| Y | `Y` | Title menu |
| Back | `S` | Stop |
| FF | `F` | Fast-forward 2× → 4× → 8× → 16× → normal |
| RW | `R` | Rewind 2× → 4× → 8× → 16× → normal |
| LB / RB | `P` / `N` | Previous / next chapter |
| — | `I` / `O` | Next audio language / next subtitle |
| Vol | `+` / `-` | Volume up / down |
| Eject | `E` | Eject |
| Guide | `M` | Toggle OSD (auto-hides after 4 s; any key/mouse also reveals it) |
| Guide | `G` | Open home menu (DVD / USB / Storage blades) |
| — | `C` | Chapter grid (playing / paused) |
| — | `A` | Cycle aspect ratio (auto → 16:9 → 4:3) |
| — | `Ctrl+H` | Help overlay — every control on one screen |
| — | `Ctrl+L` | Load a disc image / VIDEO_TS folder |
| — | `Esc` | Close help/home/chapter overlays · hide OSD |
| — | `Ctrl+F` | Toggle fullscreen |

**Gamepad** (optional): the same mapping via evdev (`inputs` package) — D-pad
or hat switch for menus, A/B/X/Y, Start/Back, LB/RB. Polled at 50 ms,
edge-triggered; the app works identically without a gamepad.

## Usage

1. Insert a DVD. **It boots automatically** — no source selection needed.
2. Navigate the menu with the mouse or the D-pad keys; press `Enter`/click to
   select.
3. Move the mouse or press any key to bring up the OSD; it auto-hides after
   4 s.
4. Switch languages with `I`/`O` (shown live on the OSD bar).
5. `E` ejects. Re-inserting the disc offers to resume where you left off.
6. ISO / VIDEO_TS: type or browse the path in the bottom bar and click
   "Load / Play".

Resume data lives in `~/.config/dvd_player/resume.json` (16 most recent
discs, keyed by a per-disc hash).

## Troubleshooting

| Symptom | Fix |
|---|---|
| "no read permission on /dev/sr0 … add the user to the 'optical' group" | `sudo usermod -aG optical $USER`, re-login |
| "kernel module 'sg' is not loaded" | `sudo modprobe sg` (see above) |
| "libdvdcss not found: encrypted DVDs cannot be decrypted" | Linux: `sudo pacman -S libdvdcss`; Windows: the pinned libmpv bundles it, or pass `--dvdcss PATH` |
| "libdvdnav not found" — menus/navigation unavailable | `sudo pacman -S libdvdnav` (Debian/Ubuntu: `libdvdnav4`) |
| No device listed / disc not detected | Drive not seen by the kernel: check `lsblk`, `lsusb`, `dmesg \| grep -i cdrom`; for USB drives, replug; internal drives may be disabled in BIOS/UEFI |
| Tray won't eject | Permissions (optical group) or the drive is mounted: unmount it first |
| Region-locked discs | CSS/region is bypassed by libdvdcss on RPC-1 drives; RPC-2 drives are flashed by the drive firmware itself — set the region in the drive's firmware tools (e.g. `regionset`) |
| Scratching / unreadable disc | The app reports "playback did not start: disc unreadable or drive error" within 30 s instead of hanging |
| No window on Wayland | The app forces `QT_QPA_PLATFORM=xcb` (XWayland) automatically, because libmpv embeds via native window handles; override with `QT_QPA_PLATFORM=wayland` if you don't need video embedding |
| Black video / sound only on NVIDIA laptops (XWayland) | Hardware GL contexts fail on NVIDIA+XWayland (Mesa has no NVIDIA DRI driver, NVIDIA EGL dmabuf import fails, GLX is indirect). Launch with `DVD_PLAYER_SOFTWARE_RENDER=1 dvd-player` (llvmpipe software rendering — fine for DVD bitrates). The installed desktop entry already sets this on affected machines. |
| Blank video in a VM | libmpv needs a real or virtual GPU context (Xvfb + `LIBGL_ALWAYS_SOFTWARE=1` for software rendering) |

## Notes & limitations

- **Title menu (Y):** libmpv's dvdnav exposes the root menu; `Y` falls back
  to the root menu (logged once). Most DVDs reach the title menu from there.
- Resume positions are saved per source identity: the same disc hashed from
  a device vs an ISO counts as two entries.
- First run of `uv run dvd-player` may briefly show environment warnings in
  the status bar for missing optional pieces — the app proceeds anyway.
