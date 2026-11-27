"""Conservative structural alternatives; no semantic contradiction inference."""
from pathlib import Path


def scopes_overlap(left, right):
    """Compare declared applicability, with normalized absolute local paths.

    Missing applicability dimensions are unbounded. Claim IDs and source IDs
    select records/evidence and cannot establish disjoint applicability.
    """
    for key in ("project_id", "workspace_id"):
        if left.get(key) and right.get(key) and left[key] != right[key]:
            return False
    left_paths, right_paths = left.get("paths", []), right.get("paths", [])
    if left_paths and right_paths and not any(
        Path(a).is_relative_to(Path(b)) or Path(b).is_relative_to(Path(a))
        for a in left_paths for b in right_paths
    ):
        return False
    left_resources, right_resources = left.get("resource_ids", []), right.get("resource_ids", [])
    if left_resources and right_resources and not set(left_resources).intersection(right_resources):
        return False
    return True


def conflicting_alternatives(claims):
    """Return peer IDs for unsuperseded, distinct same-topic statements.

    Identical statement text is a legitimate duplicate. Distinct text is only
    a review trigger: it does not prove that the statements contradict.
    """
    topics, peers = {}, {}
    for claim in claims:
        if claim["superseded_by"] is None:
            topics.setdefault(claim["topic"], []).append(claim)
    for alternatives in topics.values():
        for index, left in enumerate(alternatives):
            for right in alternatives[index + 1:]:
                if left["statement"] != right["statement"] and scopes_overlap(left["scope"], right["scope"]):
                    peers.setdefault(left["id"], []).append(right["id"])
                    peers.setdefault(right["id"], []).append(left["id"])
    return {claim_id: sorted(ids) for claim_id, ids in peers.items()}
