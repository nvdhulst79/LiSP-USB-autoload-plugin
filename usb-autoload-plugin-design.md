# LiSP USB Auto-Load Plugin — Design Doc

> **Status:** design only, not started. Standalone document — intended to seed a **new, separate plugin repo**, distinct from the APC Mini Cart plugin it was spun out of. Move/copy this file into that new repo (e.g. as `documentation/primer.md` or `DESIGN.md`).
>
> Origin: brainstormed 2026-05-23 while finishing the APC Mini Cart plugin. The LiSP-side facts below were verified against the LiSP **`develop`** branch source on that date (file:line refs are against that tree); re-verify if LiSP has moved on.

## Goal

A Linux Show Player plugin that watches for an inserted USB drive and offers to load its contents into the running show — either a saved LiSP session, or a grid of carts built from the drive's audio files. Targeted at a single-operator kiosk-style show machine (Raspberry Pi 5, RPi OS Trixie) where plugging in a stick should "just work" without navigating menus.

### Behavior (decided)

1. **Detection:** a `QTimer` on the Qt thread polls for newly-mounted removable drives (no `pyudev`, no DBus, no root). 1–2 s latency is fine for a show.
2. **Trigger:** only act while a **Cart Layout** is active (same gating model as the APC Mini Cart plugin). In any other layout the watcher is dormant.
3. **On a drive with loadable content, prompt the operator with a dialog** — never act silently (loading is destructive; see pitfalls). Dialog options depend on what's found:
   - **Session(s) `*.lsp` present *and* audio present:** offer **Load session** / **Load audio files** / **Cancel**.
   - **Only session(s):** offer **Load session** / **Cancel**.
   - **Only audio:** offer **Load audio files** / **Cancel**.
4. **Load session** → replaces the current show with the one on the stick. **Load audio files** → appends one cart per audio file to the current Cart Layout.

## Host / stack assumptions (carried over from the APC Mini Cart project)

These are LiSP-wide facts, not APC-specific — they apply to any plugin built against this host.

- **Host:** Linux Show Player, tracking the **`develop` branch** (Python ≥3.10 / PyQt5 / GStreamer). Not `master`, not the v0.6.5 tag. `develop` is the only branch where `pyliblo3` is replaced by pure-Python `python-osc` ([PR #338](https://github.com/FrancescoCeruti/linux-show-player/pull/338)), which is what lets it build on Ubuntu 24+/Debian 13 / RPi OS Trixie.
- **Plugin discovery:** LiSP scans `lisp/plugins/` **only at startup** — there is no hot-reload. For dev, symlink the plugin repo root into `lisp/plugins/<plugin_name>` and restart LiSP after adding/removing the symlink. Editing files inside the symlinked folder while LiSP runs is fine (it wouldn't reload them anyway).
- **A plugin package** is a directory under `lisp/plugins/` with an `__init__.py` exporting the plugin class, a module with the logic, and a `default.json` (auto-loaded by `core/plugins_manager.py`, and what *enables* the plugin by default).
- **`Plugin` metadata fields** (`core/plugin.py`): `Name`, `Description`, `Authors`, `Depends`, `OptDepends`, `Settings`. Use `Depends = ('GstBackend',)` here — without the GStreamer backend the audio-import path can't work, so make it a hard dependency (the APC plugin similarly used `Depends = ('Midi',)`).
- **Other plugins are reached by class name:** `get_plugin("GstBackend")`, `get_plugin("Midi")`, etc.
- **`lisp.core.signal.Signal` uses weakrefs to slots** — connect **bound methods, not lambdas**, or the connection silently drops. Pass `Connection.QtQueued` to marshal a cross-thread emit onto the Qt thread. (Not needed here since the `QTimer` already lives on the Qt thread, but keep it in mind if any background work creeps in.)

## LiSP integration points (verified `develop`, 2026-05-23)

Everything the plugin needs on the LiSP side already exists as callable API. The plugin is mostly glue.

### Detecting the active layout / session lifecycle

- Activate the watcher the same way the APC plugin does: subscribe to `app.session_created` and `app.session_loaded`, and on each check `isinstance(app.layout, CartLayout)` (`from lisp.plugins.cart_layout.layout import CartLayout`). **There is no live layout-change signal** in LiSP — these two session signals are the only activation hooks.
- After a session load, `app.session_loaded` fires (`application.py:292`), so the watcher re-evaluates the active layout automatically.

### Option (a) — bulk-load audio files into carts

- **`GstBackend.add_cue_from_files(files: list[str])`** — `plugins/gst_backend/gst_backend.py:157`. Builds a `UriAudioCueFactory` cue per path, names each from the filename (sans extension), and inserts them via `LayoutAutoInsertCuesCommand(self.app.session.layout, *cues)`. Auto-insert drops cues into the **active** layout's next free slots, and the insert goes through `commands_stack` so it's **undoable**.
- **`GstBackend.supported_extensions()`** — `gst_backend.py:101`. Returns a dict `{"audio": [...], "video": [...]}` (see `add_cue_from_urls` at `gst_backend.py:143` for the shape). Use it to filter the drive scan so only playable files are added.
- **Cart overflow is handled by LiSP automatically.** `CartLayout.__cue_added` (`plugins/cart_layout/layout.py:441`) computes `page, row, column = self.to_3d_index(cue.index)` and, if `page >= self._cart_view.count()`, calls `self.add_page()` (`layout.py:454-456`). So adding more files than fit on existing pages spawns new pages — no capacity logic needed in the plugin. Grid is `CartLayout.Config["grid.rows"]` × `["grid.columns"]` (`layout.py:71-72`), default 8×8.

So option (a) ≈ `get_plugin("GstBackend").add_cue_from_files(filtered_paths)`.

### Option (b) — load a session off the stick

- Session files are **`*.lsp`** (`ui/mainwindow.py:283,302,306`).
- **Trigger the load via the public signal, not the private loader:** `app.window.open_session.emit(session_path)`. `open_session` is a `pyqtSignal(str)` on the main window (`ui/mainwindow.py:60`), wired to `Application.__load_from_file` (`application.py:89` → `:229`). That runs the full migrate-and-load path (`SessionMigrator`, `__new_session`, cue re-creation) and emits `session_loaded` at the end — which the watcher already listens for.
- `app.window` is accessible (the gst backend uses `self.app.window` for dialog parenting), so the emit target is reachable from a plugin.

So option (b) ≈ `app.window.open_session.emit(found_lsp_path)`.

## Plugin architecture sketch

```
usb_autoload/                 # new repo root == plugin package
  __init__.py                 # exports UsbAutoload
  usb_autoload.py             # plugin class: lifecycle + QTimer + dialog + dispatch
  drive_scanner.py            # pure: enumerate mounts, classify contents (session/audio)
  settings.py                 # app-level SettingsPage (mode, poll dir, enable)
  default.json                # enables plugin, holds config + _version_
```

Lifecycle, mirroring the APC plugin:

- `__init__`: register the settings page; create (but don't start) the `QTimer`.
- `_activate` (on `session_created` / `session_loaded` when `isinstance(app.layout, CartLayout)`): start the poll timer, snapshot the current set of mounts as "already seen" so an already-inserted drive at startup doesn't immediately fire.
- `_deactivate` (layout no longer Cart, or plugin/ session teardown): stop the timer.
- `_on_poll` (timer slot, bound method): diff current mounts vs. seen set → for each *new* mount, classify and possibly prompt.

## USB detection design (QTimer poll)

- **What to poll:** the desktop auto-mount directory. On RPi OS / most desktops with an auto-mounter (pcmanfm/udisks), removable media appears under `/media/$USER/<label>` (sometimes `/run/media/$USER/<label>`). Make the base path a config value with a sensible default and fall back across the known candidates.
- **Why this and not `pyudev`/UDisks2:** the show machine runs a desktop session (pcmanfm in the panel) that already auto-mounts. Polling the mount dir needs **no root, no extra dependency, and no thread-marshalling** — the timer runs on the Qt thread, so the dialog and all LiSP calls happen on the right thread for free. `pyudev` would give instant events but at the cost of a background thread + `QtQueued` marshalling, for latency we don't need.
- **Poll interval:** 1000–2000 ms.
- **Diff, don't event:** keep a `set` of currently-present mount dir names. Each tick, `set(os.listdir(base)) - seen` = newly-arrived drives; `seen - current` = removed (just drop from the set). Process arrivals only.
- **Debounce / single-fire:** a stick can present multiple partitions and settle over a couple of ticks. Track processed mountpoints so the same drive isn't re-prompted every tick. Consider requiring a mount to persist for one extra tick before acting (lets the filesystem finish mounting).

## Decision logic (classify → prompt → dispatch)

```
on new mount <m>:
  if not Cart Layout active: ignore (timer shouldn't even be running)
  sessions = glob <m>/**/*.lsp        # decide recursion depth — see open questions
  audio    = files under <m> whose ext in GstBackend.supported_extensions()["audio"]
  if not sessions and not audio: ignore (nothing loadable; e.g. a photo stick)
  else: show dialog:
     - sessions and audio -> [Load session] [Load audio files] [Cancel]
     - sessions only      -> [Load session] [Cancel]
     - audio only         -> [Load audio files] [Cancel]
  dispatch:
     Load session     -> if multiple .lsp, pick one (see open questions); app.window.open_session.emit(path)
     Load audio files -> get_plugin("GstBackend").add_cue_from_files(sorted(audio))
     Cancel           -> mark drive processed, do nothing
```

Dialog can be a `QMessageBox` with custom buttons, or a small `QDialog` if we want to show counts ("Found 1 session and 42 audio files"). Parent it to `app.window`.

## Pitfalls & decisions (read before building)

1. **Loading a session is destructive.** `__load_from_file` → `__new_session` **deletes the current session** (`application.py:241-244`). This is the whole reason the behavior is dialog-gated rather than automatic. Consider also: refuse / extra-confirm if the current session has unsaved changes.
2. **Media-path portability for option (b).** A loaded `.lsp` stores per-cue file URIs. If the session was authored elsewhere with absolute paths, the audio won't resolve off the stick and you'll get a session that loads but plays silence. Works best when **session + media live together on the drive** with portable/relative paths. LiSP has `core/session_uri.py` — **verify how it resolves relative media URIs** before relying on portable sessions (open question #4).
3. **Cart-only for option (a).** `add_cue_from_files` targets the *active* layout via `LayoutAutoInsertCuesCommand`; it only makes sense with a Cart Layout active, which the activation gate already guarantees. Option (b), by contrast, loads whatever layout the `.lsp` declares — it may switch the host **away** from Cart Layout, which then deactivates this plugin (and the APC plugin). That's acceptable, but be aware loading a non-cart session is a one-way trip out of cart mode for the watcher.
4. **Appending mutates an existing show.** Option (a) on a non-empty session appends carts to the current pages. It's undoable, but decide whether that's wanted, or whether (a) should only target a fresh/empty session. (Default: just append — operator asked for it via the dialog.)
5. **Ordering & paging for (a).** `add_cue_from_files` inserts in list order into the next free slots; decide the sort (alphabetical by path is the obvious default) and whether subfolders should map to pages. Overflow auto-creates pages (confirmed above), so no cap.
6. **Eject mid-show** breaks playback for any cue referencing the drive. Probably out of scope for v1 — but at minimum don't crash; the removed-mount branch should just drop the drive from the seen set.
7. **Safety framing.** Auto-prompting on insert means anyone with physical access can offer to replace the show. Fine for a trusted single-operator kiosk; document it. An app-level "enable USB auto-load" toggle (default on for kiosk, easy to disable) covers shared machines.

## Settings (app-level page)

Mirror the APC plugin's app-level `SettingsPage` registered from `__init__`. Suggested keys (persisted via `default.json` + `Plugin.Config`, with a `_version_`):

- `enabled` (bool, default true) — master on/off for the watcher.
- `mount_base` (string, default `/media/$USER`) — where to poll; allow override for distros using `/run/media`.
- `poll_ms` (int, default 1500).
- `scan_recursive` (bool) + optional `max_depth` — how deep to look for audio/sessions.
- `audio_sort` (enum: name / mtime) — insertion order for option (a).

## Deployment notes (reuse from the APC Mini Cart project)

The new plugin can fork the APC project's `deploy/install.sh` almost verbatim — same host, same install shape (symlink into `lisp/plugins/`, launcher, optional autostart). Hard-won, non-obvious fixes baked into that script that this plugin's deploy will need too:

- **No aarch64 PyQt5 wheels.** LiSP's `pyproject.toml` pins both `pyqt5` and `pyqt5-qt5`; neither has an arm64 PyPI wheel and `pyqt5-qt5` has no buildable sdist. On RPi, `sed` both pins out of `pyproject.toml` after clone, re-`poetry lock`, and let runtime PyQt5 come from apt `python3-pyqt5` via `--system-site-packages`.
- **`girepository-2.0`, not 1.0, on Trixie.** PyGObject 3.50+ needs apt `libgirepository-2.0-dev` (the old `libgirepository1.0-dev` no longer satisfies the meson build).
- **GI typelibs for GStreamer** must be installed explicitly (`gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0`), or the GStreamer backend silently disables itself (no audio) even though imports look fine.
- **poetry 2.x** removed `poetry lock --no-update` (probe `--help` for the flag), and `POETRY_NO_INTERACTION=1` does **not** suppress the keyring prompt under desktop autologin — export `PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`.
- **Autostart on labwc/Wayland:** `~/.config/autostart/*.desktop` is **not** honoured; put the launch line in `~/.config/labwc/autostart` and include *only* the app line (this labwc runs both the system and user autostart files, so duplicating panel/pcmanfm lines spawns a second taskbar). Auto-login via `sudo raspi-config nonint do_boot_behaviour B4`.
- **For this plugin specifically:** the install relies on the desktop auto-mounter being present (pcmanfm/udisks). If a future headless/kiosk variant drops the file manager, the polling approach needs a replacement mount source (udisks2/DBus or a udev+systemd mount rule) — flag in the deploy README.

## Open questions / verify on first build

1. **Mount base across environments** — confirm RPi OS Trixie mounts to `/media/$USER/<label>` (vs `/run/media`). Test with a real stick.
2. **Multiple `.lsp` on one drive** — pick newest by mtime? topmost? Or list them in the dialog and let the operator choose. (Likely: if >1, show a chooser.)
3. **Recursion depth for the scan** — top-level only, or recurse? Recursing a large stick every poll is wasteful; scan once per *new* mount, not per tick.
4. **`session_uri.py` relative-media resolution** — does a `.lsp` with relative URIs resolve media against the session-file directory? Determines whether portable sticks work for option (b). (Pitfall #2.)
5. **`open_session.emit` thread/timing** — confirm emitting from the timer slot mid-session is safe (it is the same call path as File→Open, which is user-triggered on the Qt thread, so should be fine).
6. **`add_cue_from_files` with an empty/filtered list** — confirm it no-ops cleanly rather than throwing.

## Confirmed-API cheat sheet

| Need | Call | Source (LiSP `develop`) |
|---|---|---|
| Is Cart Layout active? | `isinstance(app.layout, CartLayout)` | `plugins/cart_layout/layout.py` |
| Activation hooks | `app.session_created`, `app.session_loaded` | `application.py:57,292` |
| Add audio → carts | `get_plugin("GstBackend").add_cue_from_files(paths)` | `gst_backend.py:157` |
| Playable extensions | `get_plugin("GstBackend").supported_extensions()` → `{"audio":[...],"video":[...]}` | `gst_backend.py:101,143` |
| Cart overflow → new pages | automatic in `CartLayout.__cue_added` | `cart_layout/layout.py:441,454-456` |
| Grid dimensions | `CartLayout.Config["grid.rows" / "grid.columns"]` (default 8×8) | `cart_layout/layout.py:71-72` |
| Load a session | `app.window.open_session.emit(path)` | `ui/mainwindow.py:60` → `application.py:89,229` |
| Session file glob | `*.lsp` | `ui/mainwindow.py:283,306` |
| Reach another plugin | `get_plugin("GstBackend")` (by class name) | `core/plugins_manager.py` |
| Cross-thread signal (if needed) | `signal.connect(slot, Connection.QtQueued)` | `core/signal.py` |
