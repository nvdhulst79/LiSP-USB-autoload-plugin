# This file is part of the USB Auto-Load plugin for Linux Show Player.
#
# Copyright (C) 2026 Niels van der Hulst
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. There is NO WARRANTY, to the extent permitted by law.
# See the GNU General Public License (LICENSE) for details.

"""App-level preferences page for the USB Auto-Load plugin.

One page is exposed: :class:`UsbAutoloadSettings`, reached from
``Preferences -> USB Auto-Load``. It controls the master enable toggle, the
mount directory to poll, the poll interval, scan depth, and audio insertion
order.

``CONFIG_DEFAULTS`` lives here (rather than in the plugin module) because both
this page and the plugin need it, and the plugin already imports this module —
keeping it here avoids a circular import. The values must match
``default.json``, which is what LiSP actually loads into ``Plugin.Config``.
"""

import logging

from PyQt5.QtCore import Qt, QT_TRANSLATE_NOOP
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from lisp.ui.settings.pages import SettingsPage
from lisp.ui.ui_utils import translate

logger = logging.getLogger(__name__)

# Fallback defaults for ``Config.get(key, default)``. Keep in sync with
# default.json (that file is the source of truth LiSP persists).
CONFIG_DEFAULTS = {
    "enabled": True,
    "check_at_startup": False,
    "startup_action": "ask",
    "startup_timeout_s": 5,
    "mount_base": "/media/$USER",
    "poll_ms": 1500,
    "scan_recursive": True,
    "max_depth": 4,
    "audio_sort": "name",
}

# Bounds for the poll-interval spinbox (milliseconds). The design recommends
# 1000-2000 ms; the wider range leaves room without inviting a busy-loop.
POLL_MS_MIN = 500
POLL_MS_MAX = 10000

# 0 = unlimited depth (only meaningful while "scan subfolders" is on).
MAX_DEPTH_MAX = 16

# Upper bound for the startup auto-confirm countdown (seconds); 0 means act
# immediately with no dialog at all.
STARTUP_TIMEOUT_MAX = 60

AUDIO_SORT_CHOICES = [
    ("Name (A→Z)", "name"),
    ("Modification time (oldest first)", "mtime"),
]

# What to do automatically for a drive already present at startup. "ask" keeps
# the interactive prompt; the others auto-apply (falling back to the other
# content type when the preferred one isn't on the stick).
STARTUP_ACTION_CHOICES = [
    ("Ask", "ask"),
    ("Load session", "session"),
    ("Load audio files", "audio"),
    ("Prefer session, else audio", "prefer_session"),
]


def _build_combo(parent, choices):
    """Build a ``QComboBox`` from ``(label, value)`` tuples (value in userData)."""
    combo = QComboBox(parent)
    for label, value in choices:
        combo.addItem(label, userData=value)
    return combo


def _select_combo_value(combo, value):
    """Select the item whose ``userData`` equals ``value`` (fall back to 0)."""
    idx = combo.findData(value)
    if idx < 0:
        idx = 0
    combo.setCurrentIndex(idx)


class UsbAutoloadSettings(SettingsPage):
    """App-level preferences page for the plugin."""

    Name = QT_TRANSLATE_NOOP("SettingsPageName", "USB Auto-Load")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setLayout(QVBoxLayout())
        self.layout().setAlignment(Qt.AlignTop)

        self._build_general_group()
        self._build_scan_group()

        self.retranslateUi()

    # -- group construction ------------------------------------------------

    def _build_general_group(self):
        """Master enable, mount base, and poll interval."""
        self.generalGroup = QGroupBox(self)
        self.generalGroup.setLayout(QFormLayout())
        self.layout().addWidget(self.generalGroup)

        self.enabledCheck = QCheckBox(self.generalGroup)
        self.checkAtStartupCheck = QCheckBox(self.generalGroup)
        self.checkAtStartupCheck.toggled.connect(self._on_startup_deps_changed)

        self.startupActionCombo = _build_combo(
            self.generalGroup, STARTUP_ACTION_CHOICES
        )
        self.startupActionCombo.currentIndexChanged.connect(
            self._on_startup_deps_changed
        )
        self.startupActionLabel = QLabel()

        self.startupTimeoutSpin = QSpinBox(self.generalGroup)
        self.startupTimeoutSpin.setRange(0, STARTUP_TIMEOUT_MAX)
        self.startupTimeoutSpin.setSuffix(" s")
        self.startupTimeoutSpin.setSpecialValueText(
            translate("UsbAutoload", "Immediately (no dialog)")
        )
        self.startupTimeoutLabel = QLabel()

        self.mountBaseEdit = QLineEdit(self.generalGroup)
        self.mountBaseLabel = QLabel()

        self.pollSpin = QSpinBox(self.generalGroup)
        self.pollSpin.setRange(POLL_MS_MIN, POLL_MS_MAX)
        self.pollSpin.setSingleStep(100)
        self.pollSpin.setSuffix(" ms")
        self.pollLabel = QLabel()

        form = self.generalGroup.layout()
        form.addRow(self.enabledCheck)
        form.addRow(self.checkAtStartupCheck)
        form.addRow(self.startupActionLabel, self.startupActionCombo)
        form.addRow(self.startupTimeoutLabel, self.startupTimeoutSpin)
        form.addRow(self.mountBaseLabel, self.mountBaseEdit)
        form.addRow(self.pollLabel, self.pollSpin)

    def _on_startup_deps_changed(self, *_):
        """Keep the startup-action widgets' enabled state coherent.

        The auto-action is only ever consulted for a drive already mounted at
        startup, so it's meaningless unless "check at startup" is on; the
        countdown in turn only applies once an action other than "Ask" is
        picked.
        """
        at_startup = self.checkAtStartupCheck.isChecked()
        self.startupActionCombo.setEnabled(at_startup)
        self.startupActionLabel.setEnabled(at_startup)
        acts = at_startup and self.startupActionCombo.currentData() != "ask"
        self.startupTimeoutSpin.setEnabled(acts)
        self.startupTimeoutLabel.setEnabled(acts)

    def _build_scan_group(self):
        """Recursion, depth limit, and audio insertion order."""
        self.scanGroup = QGroupBox(self)
        self.scanGroup.setLayout(QFormLayout())
        self.layout().addWidget(self.scanGroup)

        self.recursiveCheck = QCheckBox(self.scanGroup)
        self.recursiveCheck.toggled.connect(self._on_recursive_toggled)

        self.maxDepthSpin = QSpinBox(self.scanGroup)
        self.maxDepthSpin.setRange(0, MAX_DEPTH_MAX)
        self.maxDepthSpin.setSpecialValueText(translate("UsbAutoload", "Unlimited"))
        self.maxDepthLabel = QLabel()

        self.audioSortCombo = _build_combo(self.scanGroup, AUDIO_SORT_CHOICES)
        self.audioSortLabel = QLabel()

        form = self.scanGroup.layout()
        form.addRow(self.recursiveCheck)
        form.addRow(self.maxDepthLabel, self.maxDepthSpin)
        form.addRow(self.audioSortLabel, self.audioSortCombo)

    def _on_recursive_toggled(self, checked):
        """Depth only applies while subfolders are scanned."""
        self.maxDepthSpin.setEnabled(checked)
        self.maxDepthLabel.setEnabled(checked)

    # -- translation -------------------------------------------------------

    def retranslateUi(self):
        """Re-apply translated labels. Called on construction and language change."""
        self.generalGroup.setTitle(translate("UsbAutoload", "General"))
        self.enabledCheck.setText(
            translate("UsbAutoload", "Watch for inserted USB drives")
        )
        self.checkAtStartupCheck.setText(
            translate(
                "UsbAutoload",
                "Also check for drives already mounted at startup",
            )
        )
        self.startupActionLabel.setText(
            translate("UsbAutoload", "On startup, automatically:")
        )
        self.startupTimeoutLabel.setText(
            translate("UsbAutoload", "Auto-confirm after:")
        )
        self.mountBaseLabel.setText(translate("UsbAutoload", "Mount directory:"))
        self.mountBaseEdit.setPlaceholderText("/media/$USER")
        self.pollLabel.setText(translate("UsbAutoload", "Poll interval:"))

        self.scanGroup.setTitle(translate("UsbAutoload", "Drive scan"))
        self.recursiveCheck.setText(
            translate("UsbAutoload", "Scan subfolders")
        )
        self.maxDepthLabel.setText(translate("UsbAutoload", "Maximum depth:"))
        self.audioSortLabel.setText(translate("UsbAutoload", "Audio order:"))

    # -- load / save -------------------------------------------------------

    def loadSettings(self, settings):
        """Populate widgets from the plugin's persisted config dict."""
        settings = settings or {}
        self.enabledCheck.setChecked(
            settings.get("enabled", CONFIG_DEFAULTS["enabled"])
        )
        self.checkAtStartupCheck.setChecked(
            settings.get("check_at_startup", CONFIG_DEFAULTS["check_at_startup"])
        )
        _select_combo_value(
            self.startupActionCombo,
            settings.get("startup_action", CONFIG_DEFAULTS["startup_action"]),
        )
        self.startupTimeoutSpin.setValue(
            settings.get("startup_timeout_s", CONFIG_DEFAULTS["startup_timeout_s"])
        )
        self._on_startup_deps_changed()
        self.mountBaseEdit.setText(
            settings.get("mount_base", CONFIG_DEFAULTS["mount_base"])
        )
        self.pollSpin.setValue(
            settings.get("poll_ms", CONFIG_DEFAULTS["poll_ms"])
        )

        recursive = settings.get("scan_recursive", CONFIG_DEFAULTS["scan_recursive"])
        self.recursiveCheck.setChecked(recursive)
        self.maxDepthSpin.setValue(
            settings.get("max_depth", CONFIG_DEFAULTS["max_depth"])
        )
        self._on_recursive_toggled(recursive)

        _select_combo_value(
            self.audioSortCombo,
            settings.get("audio_sort", CONFIG_DEFAULTS["audio_sort"]),
        )

    def getSettings(self):
        """Serialize the page's widgets back into a config dict."""
        return {
            "enabled": self.enabledCheck.isChecked(),
            "check_at_startup": self.checkAtStartupCheck.isChecked(),
            "startup_action": self.startupActionCombo.currentData(),
            "startup_timeout_s": self.startupTimeoutSpin.value(),
            "mount_base": self.mountBaseEdit.text().strip()
            or CONFIG_DEFAULTS["mount_base"],
            "poll_ms": self.pollSpin.value(),
            "scan_recursive": self.recursiveCheck.isChecked(),
            "max_depth": self.maxDepthSpin.value(),
            "audio_sort": self.audioSortCombo.currentData(),
        }
