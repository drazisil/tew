"""
Win32 error code constants used by emulated handlers.

Values match winerror.h.  Only codes that are actually referenced by handlers
are defined here — add more as new handlers are implemented.

Reference:
    https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes
"""

from __future__ import annotations

from enum import IntEnum


class Win32Error(IntEnum):
    """Named Win32 error codes (subset of winerror.h)."""
    ERROR_SUCCESS              = 0     # No error
    ERROR_FILE_NOT_FOUND       = 2     # The system cannot find the file/object
    ERROR_PATH_NOT_FOUND       = 3     # The system cannot find the path
    ERROR_ACCESS_DENIED        = 5     # Access denied
    ERROR_INVALID_HANDLE       = 6     # The handle is invalid
    ERROR_NOT_ENOUGH_MEMORY    = 8     # Not enough memory
    ERROR_INVALID_PARAMETER    = 87    # The parameter is incorrect
    ERROR_INSUFFICIENT_BUFFER  = 122   # The data area passed to a system call is too small
    ERROR_ALREADY_EXISTS       = 183   # Cannot create a file that already exists
