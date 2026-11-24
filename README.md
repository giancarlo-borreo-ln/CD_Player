# DVD Player — Xbox 360-style DVD player for Arch Linux

Play physical commercial DVDs — interactive menus, chapters, audio tracks and
subtitles — with the exact look and feel of the **Xbox 360 DVD Player**:
translucent OSD transport bar, D-pad menu navigation, console auto-boot and
per-disc resume state.

A single-file application: `dvd_player.py` (PyQt6 GUI + libmpv `dvdnav://`
backend), packaged with `uv`.

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

## Install & run

```sh
git clone git@github.com:giancarlo-borreo-ln/CD_Player.git
cd CD_Player
uv sync
uv run dvd-player                          # auto-boots any disc in the drive
uv run dvd-player /path/to/movie.iso       # or play a specific source
uv run dvd-player /path/to/VIDEO_TS        # or a ripped DVD folder
```

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
| Guide | any key / mouse | Show OSD (auto-hides after 4 s) |
| — | `Esc` | Hide OSD |
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
| "libdvdcss not found: encrypted DVDs cannot be decrypted" | `sudo pacman -S libdvdcss` |
| No device listed / disc not detected | Drive not seen by the kernel: check `lsblk`, `lsusb`, `dmesg \| grep -i cdrom`; for USB drives, replug; internal drives may be disabled in BIOS/UEFI |
| Tray won't eject | Permissions (optical group) or the drive is mounted: unmount it first |
| Region-locked discs | CSS/region is bypassed by libdvdcss on RPC-1 drives; RPC-2 drives are flashed by the drive firmware itself — set the region in the drive's firmware tools (e.g. `regionset`) |
| Scratching / unreadable disc | The app reports "playback did not start: disc unreadable or drive error" within 30 s instead of hanging |
| No window on Wayland | The app forces `QT_QPA_PLATFORM=xcb` (XWayland) automatically, because libmpv embeds via native window handles; override with `QT_QPA_PLATFORM=wayland` if you don't need video embedding |
| Blank video in a VM | libmpv needs a real or virtual GPU context (Xvfb + `LIBGL_ALWAYS_SOFTWARE=1` for software rendering) |

## Notes & limitations

- **Title menu (Y):** libmpv's dvdnav exposes the root menu; `Y` falls back
  to the root menu (logged once). Most DVDs reach the title menu from there.
- Resume positions are saved per source identity: the same disc hashed from
  a device vs an ISO counts as two entries.
- First run of `uv run dvd-player` may briefly show environment warnings in
  the status bar for missing optional pieces — the app proceeds anyway.
