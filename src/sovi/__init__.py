"""SOVI — Social Video Intelligence & Distribution Network."""

import os as _os

__version__ = "0.1.0"

# Ensure Homebrew binaries (ffmpeg, ffprobe, psql, iproxy) are on PATH
# for subprocess calls in non-interactive sessions (launchd, SSH).
_brew = "/opt/homebrew/bin"
if _brew not in _os.environ.get("PATH", ""):
    _os.environ["PATH"] = f"{_brew}:{_os.environ.get('PATH', '')}"
