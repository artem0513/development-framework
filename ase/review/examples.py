"""Optional project-owned review examples, read by independent reviewers.

The framework defines only the location and how a reviewer receives it; the project owns
the content (short accepted/rejected examples in its own words). No file, no examples:
the framework ships no default taste.
"""
import hashlib
from pathlib import Path

PATH = '.devframework/review-examples.md'
PACKET_PATH = '_ase_context/review_examples.md'
# Short examples, not a style guide; a larger file would crowd the reviewed change out of the packet.
LIMIT = 32768


def packet_file(repo):
    """The examples as a packet file, a note when unusable, or None when the project has none."""
    path = Path(repo) / PATH
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) > LIMIT:
        content = f'{PATH} exceeds {LIMIT} bytes and was not supplied; keep it to short examples.'
    else:
        content = data.decode('utf-8', errors='replace')
    return {'path': PACKET_PATH, 'content': content, 'sha256': hashlib.sha256(content.encode()).hexdigest()}
