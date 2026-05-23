# This file is part of the USB Auto-Load plugin for Linux Show Player.
#
# Copyright (C) 2026 Niels van der Hulst
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. There is NO WARRANTY, to the extent permitted by law.
# See the GNU General Public License (LICENSE) for details.

"""Pure filesystem helpers for the USB Auto-Load plugin.

Nothing in this module touches Qt or LiSP — it only enumerates mount points
and classifies a drive's contents into loadable sessions and audio files, so
it can be unit-tested without a running show. The plugin
(:mod:`usb_autoload`) wires these into a ``QTimer`` poll and the load dialog.
"""

import logging
import os

logger = logging.getLogger(__name__)

# LiSP session files (``ui/mainwindow.py``). Extension only, no leading dot.
SESSION_EXT = "lsp"

# Directories never worth scanning on a removable drive: dotfiles/.Trash and
# the Windows-created bookkeeping folder. Pruned during the walk.
_SKIP_DIRS = frozenset({"System Volume Information", "$RECYCLE.BIN"})


class DriveContents:
    """The loadable content found on one mounted drive.

    ``sessions`` is newest-first (so a single-session auto-pick lands on the
    most recent), ``audio`` is ordered per the requested sort. Both hold
    absolute paths.
    """

    __slots__ = ("mount_path", "sessions", "audio")

    def __init__(self, mount_path, sessions, audio):
        self.mount_path = mount_path
        self.sessions = sessions
        self.audio = audio

    @property
    def is_loadable(self):
        """True if there is anything the plugin could offer to load."""
        return bool(self.sessions or self.audio)

    def __repr__(self):
        return (
            f"DriveContents(mount_path={self.mount_path!r}, "
            f"sessions={len(self.sessions)}, audio={len(self.audio)})"
        )


def expand(path):
    """Expand ``$USER``/``~`` in a configured base path.

    ``/media/$USER`` is the documented default, so environment-variable
    expansion is what makes it resolve to the real per-user mount dir.
    """
    return os.path.expanduser(os.path.expandvars(path)) if path else path


def resolve_mount_base(configured):
    """Pick the first existing mount-base directory to poll.

    The configured value wins when it exists; otherwise we fall back across
    the known auto-mounter locations (``/media/$USER`` on pcmanfm/udisks,
    ``/run/media/$USER`` on some distros). Returns ``None`` if nothing exists
    yet — the caller should treat that as "no drives" rather than an error,
    since the directory can appear once a drive is inserted.
    """
    candidates = []
    if configured:
        candidates.append(expand(configured))
    candidates += [
        expand("/media/$USER"),
        expand("/run/media/$USER"),
        "/media",
        "/run/media",
    ]
    for base in candidates:
        if base and os.path.isdir(base):
            return base
    return None


def list_mounts(base):
    """Return the set of mount-point *names* directly under ``base``.

    Names (not full paths) so they can be diffed cheaply tick-to-tick. Symlinks
    and non-directories are ignored; an unreadable/absent base yields an empty
    set rather than raising.
    """
    if not base:
        return set()
    try:
        entries = os.listdir(base)
    except OSError as error:
        logger.debug("USB Auto-Load: cannot list %s: %s", base, error)
        return set()
    return {
        name
        for name in entries
        if os.path.isdir(os.path.join(base, name))
    }


def _safe_mtime(path):
    """``os.path.getmtime`` that returns 0 instead of raising."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def scan_drive(mount_path, audio_exts, recursive=True, max_depth=None, sort="name"):
    """Classify the files on a mounted drive into sessions and audio.

    ``audio_exts`` is an iterable of lowercase extensions without the dot
    (typically ``GstBackend.supported_extensions()["audio"]``). ``recursive``
    and ``max_depth`` bound the walk — ``max_depth`` counts directory levels
    below ``mount_path`` and is ignored when ``recursive`` is false (top level
    only). ``sort`` is ``"name"`` or ``"mtime"`` and orders the audio list,
    which becomes the cart insertion order.

    Scan once per *new* mount, not per poll tick — walking a large stick every
    second would be wasteful (design open question #3).
    """
    audio_exts = {ext.lower().lstrip(".") for ext in audio_exts}
    sessions = []
    audio = []

    base_depth = mount_path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(mount_path):
        # Prune junk and hidden dirs before descending.
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in _SKIP_DIRS
        ]

        for fname in files:
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext == SESSION_EXT:
                sessions.append(os.path.join(root, fname))
            elif ext in audio_exts:
                audio.append(os.path.join(root, fname))

        depth = root.rstrip(os.sep).count(os.sep) - base_depth
        if not recursive:
            dirs[:] = []
        elif max_depth is not None and depth >= max_depth:
            dirs[:] = []

    sessions.sort(key=_safe_mtime, reverse=True)
    if sort == "mtime":
        audio.sort(key=_safe_mtime)
    else:
        audio.sort(key=lambda p: os.path.basename(p).lower())

    return DriveContents(mount_path, sessions, audio)
