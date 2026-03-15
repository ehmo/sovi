"""Clean-room enforcement -- blocks import of quarantined modules in device context.

Call enforce() at scheduler/seeder startup to prevent accidental use of
server-side email/account code in the device pipeline.
"""

import builtins
import logging

logger = logging.getLogger(__name__)

_QUARANTINED = frozenset({
    "sovi.quarantine.email_api",
    "sovi.quarantine.email_playwright",
    "sovi.quarantine.email_verifier",
    "sovi.persona.email_api",        # old paths still blocked
    "sovi.persona.email_playwright",
    "sovi.auth.email_verifier",
})

_original_import = None
_enforcing = False


class CleanRoomViolation(ImportError):
    """Importing quarantined code in device context."""


def enforce() -> None:
    """Install import hook that blocks quarantined modules."""
    global _original_import, _enforcing
    if _enforcing:
        return
    _original_import = builtins.__import__

    def _guarded(name, *args, **kwargs):
        if name in _QUARANTINED:
            raise CleanRoomViolation(
                f"CLEAN-ROOM VIOLATION: '{name}' makes server-side persona requests. "
                f"Use on-device alternatives (device/email_reader.py)."
            )
        return _original_import(name, *args, **kwargs)

    builtins.__import__ = _guarded
    _enforcing = True
    logger.info("Clean-room enforcement active -- %d modules quarantined", len(_QUARANTINED))
