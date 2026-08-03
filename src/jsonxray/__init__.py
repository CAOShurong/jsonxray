"""jsonxray -- see what is really inside a JSON Lines file.

Point it at a file that is too big to open and it tells you, in one streaming
pass, which fields exist and how often, what types they hold, which lines
disagree with the rest, and which records do not look like the others.

    from jsonxray import Profile, scan

    profile = Profile(source="events.jsonl")
    with open("events.jsonl", encoding="utf-8") as handle:
        scan(handle, profile)

    for node in profile.conflicts():
        print(node.path, node.non_null_types, node.examples)
"""

from .compare import Comparison, compare
from .profile import FieldNode, Profile
from .scan import ScanError, scan

__version__ = "0.1.1"

__all__ = [
    "Comparison",
    "FieldNode",
    "Profile",
    "ScanError",
    "__version__",
    "compare",
    "scan",
]
