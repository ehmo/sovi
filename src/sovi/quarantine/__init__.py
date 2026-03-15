"""Quarantined modules -- server-side code that violates clean-room rules.

These modules make HTTP requests to email providers and social platforms
from the Mac Studio. All persona-facing traffic must originate from
physical iPhones over cellular connections.

Importing from this package in device context will raise CleanRoomViolation.
"""

class CleanRoomViolation(ImportError):
    """Raised when quarantined code is imported in production device context."""
    pass
