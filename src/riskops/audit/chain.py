"""Forward hash chain over the audit log.

Each entry's hash covers the previous entry's hash as well as its own contents,
so editing entry *N* invalidates *N* and every entry after it. A standalone
digest - hashing a record and storing the digest beside it - cannot do this:
whoever edits the record recomputes the digest. The chain forces an edit to
rewrite the whole tail.

**What this proves and does not prove**, stated plainly because an auditor will
ask: it makes a *partial* edit detectable. Someone who can rewrite the entire
table can still recompute every link. Real non-repudiation needs the head
anchored somewhere the editor does not control - signed, or published
externally. This project does not do that, and says so rather than implying a
guarantee it has not earned.

Verification returns three states rather than a boolean, because "this record
predates chaining" and "this record was tampered with" are opposite findings and
collapsing them reports a legacy row as an attack.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64


def canonical(entry: dict[str, Any]) -> str:
    """Canonical serialisation. Field order is fixed here, not left to a dict.

    `json.dumps` on a dict would encode whatever insertion order the caller
    happened to use, so two identical records could hash differently.
    """
    return json.dumps(
        [
            str(entry.get("entry_id", "")),
            str(entry.get("occurred_at", "")),
            str(entry.get("actor_role", "")),
            str(entry.get("actor_id", "")),
            str(entry.get("action", "")),
            str(entry.get("object_type", "")),
            str(entry.get("object_id", "")),
            str(entry.get("summary", "")),
            str(entry.get("payload_json", "")),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def link_hash(previous_hash: str, entry: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update((previous_hash or GENESIS).encode("utf-8"))
    digest.update(canonical(entry).encode("utf-8"))
    return digest.hexdigest()


@dataclass
class ChainVerification:
    """`verified` every link recomputes; `broken` one does not; `unverifiable`
    the log carries no hashes at all because it predates chaining."""

    status: str
    valid: bool
    head: str
    entries_checked: int
    broken_at: int | None = None
    broken_entry_id: str | None = None

    def summary(self) -> str:
        if self.status == "verified":
            return (
                f"{self.entries_checked} entries verified; head "
                f"{self.head[:12]}...  A partial edit would break this chain."
            )
        if self.status == "unverifiable":
            return "the log carries no hashes; it cannot be verified either way"
        return (
            f"chain broken at entry {self.broken_at} ({self.broken_entry_id}); "
            f"every entry from there on is unverifiable"
        )


def verify(entries: list[dict[str, Any]]) -> ChainVerification:
    """Recompute every link in order."""
    if not entries:
        return ChainVerification("verified", True, GENESIS, 0)
    if all(not entry.get("entry_hash") for entry in entries):
        return ChainVerification("unverifiable", False, GENESIS, len(entries))

    previous = GENESIS
    for index, entry in enumerate(entries):
        stored = str(entry.get("entry_hash") or "")
        if not stored:
            return ChainVerification(
                "broken", False, previous, len(entries), index, str(entry.get("entry_id"))
            )
        expected = link_hash(previous, entry)
        if expected != stored:
            return ChainVerification(
                "broken", False, previous, len(entries), index, str(entry.get("entry_id"))
            )
        previous = stored
    return ChainVerification("verified", True, previous, len(entries))
