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
        """Start polling; snapshot current mounts so an already-inserted drive
        at startup doesn't immediately fire."""
        if self._active:
            return
        self._active = True

        base = drive_scanner.resolve_mount_base(self._cfg("mount_base"))
        self._seen = drive_scanner.list_mounts(base)
        self._pending = set()
        self._processed = set()

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

        action = self._prompt(contents)
        if action == "session":
            self._load_session(contents)
        elif action == "audio":
            self._load_audio(contents)
        # "cancel" -> already marked processed; do nothing.

    # ------------------------------------------------------------------ #
    # Dialog + dispatch                                                  #
    # ------------------------------------------------------------------ #

    def _prompt(self, contents):
        """Show the load dialog; return ``"session"`` / ``"audio"`` / ``"cancel"``.

        Buttons depend on what was found (sessions, audio, or both). Loading a
        session replaces the running show, so the dialog defaults to Cancel.
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

        box.exec()
        clicked = box.clickedButton()
        if clicked is session_btn:
            return "session"
        if clicked is audio_btn:
            return "audio"
        return "cancel"

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

    def _load_session(self, contents):
        """Replace the running show with a session off the drive.

        With more than one ``*.lsp`` present we let the operator choose;
        otherwise the single (newest) one is used. ``open_session`` runs the
        full migrate-and-load path and emits ``session_loaded`` at the end,
        which re-drives our activation gate.
        """
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
