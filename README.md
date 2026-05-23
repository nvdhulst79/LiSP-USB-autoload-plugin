# USB Auto-Load — Linux Show Player plugin

Watches for an inserted USB drive and offers to load its contents into the
running show — either a saved LiSP session, or a grid of carts built from the
drive's audio files. Built for a single-operator, kiosk-style show machine
(e.g. a Raspberry Pi 5) where plugging in a stick should "just work".

> **Status:** scaffold. The structure and the load path are in place; the
> open questions in [usb-autoload-plugin-design.md](usb-autoload-plugin-design.md)
> (mount base across distros, recursion depth, portable-media resolution)
> should be confirmed against a real stick on the target machine.

## How it works

- A `QTimer` (1–2 s) polls the desktop auto-mount directory
  (`/media/$USER/<label>` on most desktops) for newly inserted drives. No
  `pyudev`, no DBus, no root — the timer runs on the Qt thread, so the dialog
  and every LiSP call happen on the right thread for free.
- The watcher is **only active while a Cart Layout session is loaded**. Any
  other layout leaves it dormant.
- On a drive with loadable content the operator is **prompted** (loading a
  session is destructive — never silent):
  - **Load session** → replaces the running show with a `*.lsp` off the stick.
    If more than one is present you pick which.
  - **Load audio files** → appends one cart per audio file to the current
    Cart Layout (overflow auto-creates pages; the insert is undoable).

## Requirements

- **Linux Show Player**, `develop` branch (Python ≥ 3.10, PyQt5, GStreamer).
  The v0.6.5 tag and `master` are **not** supported.
- LiSP's built-in **GStreamer backend** (a hard dependency — it does the audio
  import).
- A desktop **auto-mounter** (pcmanfm/udisks) so removable media appears under
  `/media/$USER`. A headless variant would need a different mount source.

## Installation

LiSP scans `lisp/plugins/` only at startup (no hot-reload). Symlink this repo
into the LiSP source tree and restart LiSP:

```sh
ln -s /path/to/this/repo /path/to/linux-show-player/lisp/plugins/usb_autoload
```

## Preferences

`Preferences → USB Auto-Load`:

| Setting | Default | Notes |
|---|---|---|
| Watch for inserted USB drives | on | master enable for the watcher |
| Mount directory | `/media/$USER` | falls back to `/run/media/$USER` |
| Poll interval | 1500 ms | |
| Scan subfolders | on | with a maximum depth (0 = unlimited) |
| Audio order | Name | cart insertion order (name or mtime) |

## Safety note

Auto-prompting on insert means anyone with physical access can offer to replace
the show. That's fine for a trusted single-operator kiosk; turn the watcher off
in Preferences on shared machines.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). This matches Linux Show Player's own license.
