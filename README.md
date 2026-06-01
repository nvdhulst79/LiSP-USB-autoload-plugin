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

### Drives present at startup

- By default the watcher only reacts to drives inserted *after* LiSP is running;
  a stick already in the machine at launch is ignored. Turn on **"Also check for
  drives already mounted at startup"** to have it picked up on the first poll —
  handy for a kiosk that powers on with the show stick already plugged in.
- For that startup drive the prompt can be skipped entirely, so a normal show
  start needs no keyboard. The **"On startup, automatically"** policy chooses
  what to do (load a session, load audio, or prefer a session and fall back to
  audio), and **"Auto-confirm after"** controls how:
  - With a countdown (e.g. 5 s) the usual dialog still appears with that action
    pre-selected and counting down in its label, auto-confirming when it reaches
    zero. Clicking any button overrides it and cancels the countdown.
  - Set to *Immediately* (0 s) it acts at once with no dialog.

  This is safe only at startup, where the Cart Layout is freshly created and
  there is nothing to overwrite. Drives inserted while a show is running always
  fall through to the plain prompt regardless of this setting.

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
| Also check for drives already mounted at startup | off | pick up a stick left in the machine across a power cycle |
| On startup, automatically | Ask | startup action: Ask / Load session / Load audio files / Prefer session, else audio (only applies to a startup drive) |
| Auto-confirm after | 5 s | countdown before the startup action fires; 0 = act immediately with no dialog |
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
