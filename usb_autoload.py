# This file is part of the USB Auto-Load plugin for Linux Show Player.
#
# Copyright (C) 2026 Niels van der Hulst
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. There is NO WARRANTY, to the extent permitted by law.
# See the GNU General Public License (LICENSE) for details.

"""USB drive watcher for the LiSP Cart Layout.

While a Cart Layout session is active, a ``QTimer`` polls the desktop
auto-mount directory for newly inserted removable drives. When a new drive
carries loadable content the operator is prompted (never silently acted on,
since loading a session is destructive) to:

* **Load session** — replace the running show with a ``*.lsp`` off the stick
  (``app.window.open_session.emit``), or
* **Load audio files** — append one cart per audio file to the current Cart
  Layout (``GstBackend.add_cue_from_files``).

For a drive already present at startup the operator can skip the keyboard
entirely: a configured ``startup_action`` is applied automatically. With a
non-zero ``startup_timeout_s`` the dialog still appears with that action as a
counting-down default the operator can override; with a zero timeout it acts
immediately and silently. This is safe only at startup, where the Cart Layout
is freshly created and there is nothing to overwrite — drives inserted while a
show is running always fall through to the plain prompt.

Detection is poll-based on purpose: the show machine runs a desktop
auto-mounter (pcmanfm/udisks), so polling its mount dir needs no root, no extra
dependency, and no thread marshalling — the timer already runs on the Qt
thread, which is where every LiSP call and the dialog must happen.

Activation mirrors the APC Mini Cart plugin: there is no live layout-change
signal in LiSP, so we (re)evaluate the active layout on ``session_created`` /
``session_loaded`` and tear down on ``session_before_finalize``.
"""

import logging
import os

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QInputDialog, QMessageBox

from lisp.core.plugin import Plugin
from lisp.core.signal import Connection
from lisp.plugins import get_plugin
from lisp.plugins.cart_layout.layout import CartLayout
from lisp.ui.settings.app_configuration import AppConfigurationDialog
from lisp.ui.ui_utils import translate

from . import drive_scanner
from .settings import CONFIG_DEFAULTS, UsbAutoloadSettings

logger = logging.getLogger(__name__)


class UsbAutoload(Plugin):
    """Watch for inserted USB drives and offer to load their contents."""

    Name = "USB Auto-Load"
    Authors = ("Niels van der Hulst",)
    Description = (
        "Watches for an inserted USB drive and offers to load its session "
        "or audio files into the active Cart Layout."
    )
    # GstBackend is required: without it the audio-import path can't work.
    Depends = ("GstBackend",)

    def __init__(self, app):
        super().__init__(app)

        # Runtime state.
        self._active = False     # bound to a CartLayout session?
        self._seen = set()       # mount names present as of the last tick
        self._pending = set()    # arrived but not yet settled (one extra tick)
        self._processed = set()  # already prompted; don't re-prompt
        self._startup_mounts = set()  # names present at startup -> auto-action eligible
        self._startup_done = False  # one-shot: have we handled the first activation?

        # Poll timer (created but not started; lives on the Qt thread).
        self._timer = QTimer()
        self._timer.setTimerType(0)  # Qt.PreciseTimer is overkill; coarse is fine
        self._timer.timeout.connect(self._on_poll)

        # Settings page.
        AppConfigurationDialog.registerSettingsPage(
            "plugins.usb_autoload",
            UsbAutoloadSettings,
            UsbAutoload.Config,
        )

        # Session lifecycle. QtQueued so handlers always run on the Qt thread
        # regardless of which thread fired the signal.
        self.app.session_created.connect(self._on_session_change, Connection.QtQueued)
        self.app.session_loaded.connect(self._on_session_change, Connection.QtQueued)
        self.app.session_before_finalize.connect(
            self._on_session_finalize, Connection.QtQueued
        )

        # React to preference changes (enable toggle, poll interval, ...).
        UsbAutoload.Config.changed.connect(self._on_config_changed)
        UsbAutoload.Config.updated.connect(self._on_config_changed)

    # ------------------------------------------------------------------ #
    # Config helpers                                                     #
    # ------------------------------------------------------------------ #

    def _cfg(self, key):
        """Read a config value, falling back to the hardcoded default."""
        return self.Config.get(key, CONFIG_DEFAULTS[key])

    # ------------------------------------------------------------------ #
    # Session / layout lifecycle                                         #
    # ------------------------------------------------------------------ #

    def _on_session_change(self, *_):
        """Activate or deactivate depending on the current layout type."""
        if self._cfg("enabled") and isinstance(self.app.layout, CartLayout):
            self._activate()
        else:
            self._deactivate()

    def _on_session_finalize(self, *_):
        """Always tear down before a session is unloaded."""
        self._deactivate()

    def _activate(self):
        """Start polling.

        Normally we snapshot current mounts into ``_seen`` so an already-inserted
        drive doesn't immediately fire. With ``check_at_startup`` enabled, the
        very first activation starts from an empty snapshot instead, so a drive
        left in the machine across a power cycle gets picked up. The flag is
        one-shot: later reactivations (layout switches) won't re-prompt for the
        same stick.
        """
        if self._active:
            return
        self._active = True

        base = drive_scanner.resolve_mount_base(self._cfg("mount_base"))
        present = drive_scanner.list_mounts(base)
        if self._cfg("check_at_startup") and not self._startup_done:
            # Treat everything already mounted as a fresh arrival so it gets
            # picked up, and remember those names: only they are eligible for
            # the keyboard-free startup auto-action.
            self._seen = set()
            self._startup_mounts = set(present)
        else:
            self._seen = present
            self._startup_mounts = set()
        self._pending = set()
        self._processed = set()
        self._startup_done = True

        self._timer.setInterval(self._cfg("poll_ms"))
        self._timer.start()
        logger.info(
            "USB Auto-Load: activated (Cart Layout detected); watching %s.",
            base or "(no mount dir yet)",
        )

    def _deactivate(self):
        """Stop polling and reset state."""
        if not self._active:
            return
        self._timer.stop()
        self._seen = set()
        self._pending = set()
        self._processed = set()
        self._startup_mounts = set()
        self._active = False
        logger.info("USB Auto-Load: deactivated.")

    def _on_config_changed(self, *_):
        """Re-evaluate activation and refresh the poll interval on pref changes."""
        was_active = self._active
        # Re-run the activation gate (handles the enable toggle flipping).
        self._on_session_change()
        if self._active:
            self._timer.setInterval(self._cfg("poll_ms"))
        elif was_active:
            logger.info("USB Auto-Load: disabled via preferences.")

    # ------------------------------------------------------------------ #
    # Polling: diff mounts, debounce, dispatch                           #
    # ------------------------------------------------------------------ #

    def _on_poll(self):
        """Timer slot: diff current mounts vs. seen and act on settled arrivals.

        A stick can present multiple partitions and settle over a couple of
        ticks, so a freshly-seen mount waits one extra tick before we act on
        it (lets the filesystem finish mounting). Removed mounts are simply
        dropped from the tracking sets.
        """
        base = drive_scanner.resolve_mount_base(self._cfg("mount_base"))
        current = drive_scanner.list_mounts(base)

        # Drop state for anything that's gone (eject is otherwise out of scope).
        for name in self._seen - current:
            self._processed.discard(name)
            self._pending.discard(name)
            self._startup_mounts.discard(name)

        to_process = []
        for name in current:
            if name in self._processed:
                continue
            if name in self._pending:
                # Settled for one extra tick — act now.
                to_process.append(name)
                self._pending.discard(name)
            elif name not in self._seen:
                # First sighting — wait one tick for the mount to settle.
                self._pending.add(name)

        self._seen = current

        for name in to_process:
            self._processed.add(name)
            self._handle_new_mount(base, name)

    def _handle_new_mount(self, base, name):
        """Scan a newly-settled mount and, if loadable, prompt + dispatch."""
        # Re-check the gate: a slow scan/dialog could overlap a layout change.
        if not isinstance(self.app.layout, CartLayout):
            return

        mount_path = os.path.join(base, name)
        try:
            audio_exts = get_plugin("GstBackend").supported_extensions()["audio"]
        except Exception:
            logger.exception("USB Auto-Load: could not read supported extensions.")
            return

        recursive = self._cfg("scan_recursive")
        max_depth = self._cfg("max_depth") or None  # 0 -> unlimited
        contents = drive_scanner.scan_drive(
            mount_path,
            audio_exts,
            recursive=recursive,
            max_depth=max_depth if recursive else None,
            sort=self._cfg("audio_sort"),
        )

        if not contents.is_loadable:
            logger.debug("USB Auto-Load: %s has no loadable content.", mount_path)
            return

        # A drive present at startup may be auto-loaded without the keyboard;
        # any later insertion always goes through the plain prompt. Consume the
        # startup flag either way so a mid-show re-insert isn't treated as one.
        is_startup = name in self._startup_mounts
        self._startup_mounts.discard(name)

        auto_action = self._resolve_startup_action(contents) if is_startup else None
        if auto_action is not None:
            timeout_s = self._cfg("startup_timeout_s")
            if timeout_s > 0:
                action, auto_fired = self._prompt(
                    contents, default_action=auto_action, timeout_s=timeout_s
                )
            else:
                action, auto_fired = auto_action, True
        else:
            action, auto_fired = self._prompt(contents)

        if action == "session":
            # An auto-fired choice skips the multi-session picker (it would just
            # reintroduce the keyboard); an explicit click still gets to choose.
            self._load_session(contents, auto=auto_fired)
        elif action == "audio":
            self._load_audio(contents)
        # "cancel" -> already marked processed; do nothing.

    def _resolve_startup_action(self, contents):
        """Map the configured startup policy onto what the drive actually holds.

        Returns ``"session"`` / ``"audio"`` (the action to auto-apply) or
        ``None`` to fall back to the interactive prompt — either because the
        policy is ``"ask"`` or the drive carries nothing the policy can act on.
        Both session-leaning policies prefer a session and fall back to audio;
        the audio policy is the mirror image.
        """
        policy = self._cfg("startup_action")
        if policy == "ask":
            return None

        preferred = "audio" if policy == "audio" else "session"
        present = {
            "session": bool(contents.sessions),
            "audio": bool(contents.audio),
        }
        fallback = "session" if preferred == "audio" else "audio"
        for choice in (preferred, fallback):
            if present[choice]:
                return choice
        return None

    # ------------------------------------------------------------------ #
    # Dialog + dispatch                                                  #
    # ------------------------------------------------------------------ #

    def _prompt(self, contents, default_action=None, timeout_s=0):
        """Show the load dialog; return ``(action, auto_fired)``.

        ``action`` is ``"session"`` / ``"audio"`` / ``"cancel"``. Buttons depend
        on what was found (sessions, audio, or both). Loading a session replaces
        the running show, so the dialog normally defaults to Cancel.

        When ``default_action`` is given with ``timeout_s > 0`` (the keyboard-free
        startup path), that button becomes the default and counts down in its
        label; left untouched it auto-clicks at zero and ``auto_fired`` returns
        True. Clicking any button cancels the countdown and counts as a manual
        choice. The ``QTimer`` keeps ticking inside ``exec()``'s nested event
        loop, so no extra threading is needed.
        """
        box = QMessageBox(self.app.window)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(translate("UsbAutoload", "USB drive detected"))
        box.setText(self._summary(contents))

        session_btn = audio_btn = None
        if contents.sessions:
            session_btn = box.addButton(
                translate("UsbAutoload", "Load session"), QMessageBox.AcceptRole
            )
        if contents.audio:
            audio_btn = box.addButton(
                translate("UsbAutoload", "Load audio files"), QMessageBox.AcceptRole
            )
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel_btn)

        target_btn = session_btn if default_action == "session" else (
            audio_btn if default_action == "audio" else None
        )
        auto = {"fired": False}
        if target_btn is not None and timeout_s > 0:
            self._attach_countdown(box, target_btn, int(timeout_s), auto)

        box.exec()
        clicked = box.clickedButton()
        if clicked is session_btn:
            return "session", auto["fired"]
        if clicked is audio_btn:
            return "audio", auto["fired"]
        return "cancel", auto["fired"]

    @staticmethod
    def _attach_countdown(box, target_btn, seconds, auto):
        """Make ``target_btn`` the default and have it auto-click after a countdown.

        The remaining seconds are shown in the button label; ``auto["fired"]`` is
        set just before the automatic click so the caller can tell an auto-load
        from a manual one. Any button click stops the timer.
        """
        box.setDefaultButton(target_btn)
        base_text = target_btn.text()
        remaining = [seconds]

        def label(n):
            return translate("UsbAutoload", "%s (%d)") % (base_text, n)

        target_btn.setText(label(remaining[0]))

        countdown = QTimer(box)
        countdown.setInterval(1000)

        def tick():
            remaining[0] -= 1
            if remaining[0] <= 0:
                countdown.stop()
                auto["fired"] = True
                target_btn.click()
            else:
                target_btn.setText(label(remaining[0]))

        countdown.timeout.connect(tick)
        box.buttonClicked.connect(lambda _btn: countdown.stop())
        countdown.start()

    @staticmethod
    def _summary(contents):
        """Human-readable count of what was found on the drive."""
        parts = []
        if contents.sessions:
            n = len(contents.sessions)
            parts.append(
                translate("UsbAutoload", "%d session(s)") % n
            )
        if contents.audio:
            n = len(contents.audio)
            parts.append(
                translate("UsbAutoload", "%d audio file(s)") % n
            )
        found = translate("UsbAutoload", " and ").join(parts)
        return translate("UsbAutoload", "Found %s on the inserted drive.") % found

    def _load_session(self, contents, auto=False):
        """Replace the running show with a session off the drive.

        With more than one ``*.lsp`` present we normally let the operator choose;
        on the auto-load path we silently take the newest (sessions are sorted
        newest-first) so the keyboard-free flow isn't broken by a picker.
        ``open_session`` runs the full migrate-and-load path and emits
        ``session_loaded`` at the end, which re-drives our activation gate.
        """
        if auto:
            path = contents.sessions[0] if contents.sessions else None
        else:
            path = self._choose_session(contents.sessions)
        if path is None:
            return
        logger.info("USB Auto-Load: loading session %s.", path)
        self.app.window.open_session.emit(path)

    def _choose_session(self, sessions):
        """Return the session path to load, prompting if there's more than one."""
        if len(sessions) == 1:
            return sessions[0]

        labels = [os.path.basename(p) for p in sessions]
        label, ok = QInputDialog.getItem(
            self.app.window,
            translate("UsbAutoload", "Choose a session"),
            translate("UsbAutoload", "Multiple sessions found — which to load?"),
            labels,
            0,
            False,
        )
        if not ok:
            return None
        return sessions[labels.index(label)]

    def _load_audio(self, contents):
        """Append one cart per audio file to the active Cart Layout."""
        if not contents.audio:
            return
        logger.info(
            "USB Auto-Load: importing %d audio file(s) as carts.",
            len(contents.audio),
        )
        get_plugin("GstBackend").add_cue_from_files(contents.audio)
