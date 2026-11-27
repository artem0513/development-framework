"""Native local reporting time and explicitly selected IANA timezone rules."""
from datetime import timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimezoneDataUnavailable(ValueError):
    code = "TZ_DATA_UNAVAILABLE"

    def __init__(self, detail):
        super().__init__(self.code + ": " + str(detail))


# Preserve the stdlib file reader even when the optional host lookup is absent.
_from_file = ZoneInfo.from_file


def resolve_timezone(name, *, tzif_path=None):
    """Resolve local, UTC or IANA rules without caching a current fixed offset.

    ``None`` is deliberate for ``local``: datetime.astimezone(None) uses the
    operating system's rules for that instant, and naive midnight boundaries
    convert from local time with the rules for their own date. This also works
    on Windows without installing an IANA database. An explicit named zone
    still requires its actual rules and never silently falls back to local.

    An explicit TZif is checked against the bundled zone's provenance too. This
    permits relocation without silently accepting a different or corrupt file.
    Other IANA zones can use the execution host's installed database.
    """
    if name == "UTC" and tzif_path is None:
        return timezone.utc
    if name == "local" and tzif_path is None:
        return None
    if not isinstance(name, str) or not name or "\0" in name:
        raise TimezoneDataUnavailable("Invalid timezone name")
    root = Path(__file__).parent / "data" / "timezones"
    if name == "Europe/Bucharest" or tzif_path is not None:
        try:
            provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
            entry = provenance["zones"][name]
            path = Path(tzif_path) if tzif_path is not None else root / entry["path"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("TZif checksum differs from verified provenance")
            return _from_file(BytesIO(data), key=name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise TimezoneDataUnavailable(name + ": " + str(error)) from error
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError) as error:
        raise TimezoneDataUnavailable(name) from error
