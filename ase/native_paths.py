"""Ordinary logical paths with Windows extended-length views for physical I/O.

Native views must never be persisted as receipt identities or containment roots.
State I/O keeps the existing no-reparse/no-shared-inode trust boundary.
"""
import os
from pathlib import Path, PureWindowsPath
import re
import stat


def _windows_components(value):
    path = PureWindowsPath(value)
    for part in path.parts[1:] if path.anchor else path.parts:
        base = part.split(".", 1)[0].upper()
        if ":" in part or part.endswith((" ", ".")) or base in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", base):
            raise ValueError("Reserved Windows state path component")


def logical_path(value):
    """Return an ordinary absolute path, accepting only disk/UNC namespaces."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("A nonempty filesystem path is required")
    windows = raw.replace("/", "\\")
    if windows.startswith(("\\\\.\\", "\\??\\", "\\\\??\\")):
        raise ValueError("Device namespaces are not state paths")
    if windows.startswith("\\\\?\\"):
        tail = windows[4:]
        if tail[:4].upper() == "UNC\\":
            windows = "\\\\" + tail[4:]
        elif re.match(r"^[A-Za-z]:\\", tail):
            windows = tail
        else:
            raise ValueError("Only drive and UNC extended paths are supported")
        if os.name != "nt":
            raise ValueError("Windows native paths require Windows")
    if windows.startswith(("\\\\.\\", "\\??\\", "\\\\??\\", "\\\\?\\")):
        raise ValueError("Nested device namespaces are not state paths")
    if os.name == "nt":
        if re.match(r"^[A-Za-z]:(?!\\)", windows):
            raise ValueError("Drive-relative state paths are not supported")
        if ".." in windows.split("\\"):
            raise ValueError("State path traversal is not permitted")
        # Win32 abspath may rewrite NUL to a device namespace or silently strip
        # a trailing dot/space. Validate the untouched components first.
        _windows_components(windows)
        path = Path(os.path.abspath(windows))
        if str(path).startswith(("\\\\.\\", "\\??\\", "\\\\??\\", "\\\\?\\")):
            raise ValueError("Normalized device namespaces are not state paths")
        drive = path.drive
        if not (re.fullmatch(r"[A-Za-z]:", drive) or
                (drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2 and all(drive[2:].split("\\")))):
            raise ValueError("An absolute drive or UNC path is required")
        _windows_components(str(path))
        return path
    if ".." in Path(raw).parts:
        raise ValueError("State path traversal is not permitted")
    return Path(os.path.abspath(raw))


def native_path(value):
    """Physical I/O view only; use logical_path for metadata and comparisons."""
    path = logical_path(value)
    if os.name != "nt":
        return path
    text = str(path)
    return Path("\\\\?\\UNC\\" + text[2:]) if text.startswith("\\\\") else Path("\\\\?\\" + text)


def state_io(value):
    """Validate physical ancestors and return a native view without aliases."""
    logical = logical_path(value)
    for candidate in [*reversed(logical.parents), logical]:
        try:
            info = native_path(candidate).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("State reparse points are not supported")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError("Hardlinked state files are not supported")
    physical = native_path(logical)
    resolved = logical_path(physical.resolve())
    if os.path.normcase(str(resolved)) != os.path.normcase(str(logical)):
        raise ValueError("State path resolves outside its logical location")
    return physical
