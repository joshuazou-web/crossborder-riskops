"""The append-only audit log.

Every consequential act writes here: a policy decision, an AI invocation, a human
decision, an appeal, a refresh. Entries are appended and hash-chained; nothing is
updated and nothing is deleted. When a case outcome changes, that is a *new*
entry recording the change, not an edit of the old one - which is exactly why
"the analyst overrode the AI at 02:14 and reversed it at 09:30" is answerable.

The actor is always recorded, and `ai_copilot` is a valid actor for a
*suggestion* and never for a *decision*. `record_decision` enforces that rather
than trusting its caller.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

import duckdb
import pandas as pd

from ..taxonomy import (
    DECIDING_ROLES,
    DECISION_ACTION,
    HUMAN_ACTIONS,
    REASON_CODE,
    SYSTEM_ONLY_REASON_CODES,
)
from .chain import GENESIS, ChainVerification, link_hash, verify

LOGGER = logging.getLogger(__name__)


class AuditError(ValueError):
    """Raised when an act would violate the audit contract."""


def _entry_id(seq: int, action: str, object_id: str, occurred_at: datetime) -> str:
    seed = f"{seq}|{action}|{object_id}|{occurred_at.isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


class AuditLog:
    """Append-only, hash-chained log over one DuckDB connection."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    # -- writing -----------------------------------------------------------

    def _head(self) -> tuple[int, str]:
        row = self.con.execute(
            "SELECT coalesce(max(seq), 0) FROM audit.audit_log"
        ).fetchone()
        seq = int(row[0]) if row else 0
        if seq == 0:
            return 0, GENESIS
        head = self.con.execute(
            "SELECT entry_hash FROM audit.audit_log WHERE seq = ?", [seq]
        ).fetchone()
        return seq, str(head[0]) if head else GENESIS

    def append(
        self,
        *,
        actor_role: str,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        seq, previous = self._head()
        seq += 1
        when = occurred_at or datetime.now().replace(microsecond=0)
        entry = {
            "seq": seq,
            "entry_id": _entry_id(seq, action, object_id, when),
            "occurred_at": when,
            "actor_role": actor_role,
            "actor_id": actor_id,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "summary": summary,
            "payload_json": json.dumps(payload or {}, ensure_ascii=False, sort_keys=True,
                                       default=str),
        }
        entry["prev_hash"] = previous
        entry["entry_hash"] = link_hash(previous, entry)
        self.con.execute(
            """
            INSERT INTO audit.audit_log
                (seq, entry_id, occurred_at, actor_role, actor_id, action, object_type,
                 object_id, summary, payload_json, prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [entry["seq"], entry["entry_id"], entry["occurred_at"], entry["actor_role"],
             entry["actor_id"], entry["action"], entry["object_type"], entry["object_id"],
             entry["summary"], entry["payload_json"], entry["prev_hash"], entry["entry_hash"]],
        )
        return entry

    def append_many(self, entries: list[dict[str, Any]]) -> int:
        """Append a batch in order. Slower than a bulk insert, and correct: the
        chain is sequential by definition, so there is nothing to parallelise."""
        for entry in entries:
            self.append(**entry)
        return len(entries)

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self,
        *,
        case_id: str,
        transaction_id: str,
        actor_role: str,
        actor_id: str,
        action: str,
        reason_code: str,
        note: str = "",
        ai_recommended_action: str = "",
        ai_confidence: float | None = None,
        previous_case_state: str = "",
        new_case_state: str = "",
        decided_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Commit one decision. Refuses anything the role is not allowed to do.

        This is the enforcement point for the product's central claim. It is a
        function, not a policy document, so the claim is testable.
        """
        if actor_role not in DECIDING_ROLES:
            raise AuditError(
                f"role {actor_role!r} may not commit a decision. Deciding roles are "
                f"{DECIDING_ROLES}; the AI copilot is deliberately not among them."
            )
        if actor_role != "system" and action not in HUMAN_ACTIONS:
            raise AuditError(
                f"{action!r} is not a decision a person may commit; choose one of {HUMAN_ACTIONS}. "
                "`abstain` exists only for the copilot."
            )
        if action not in DECISION_ACTION.names:
            raise AuditError(f"{action!r} is not in the decision taxonomy")
        if reason_code not in REASON_CODE.names:
            raise AuditError(f"{reason_code!r} is not in the reason-code taxonomy")
        if reason_code in SYSTEM_ONLY_REASON_CODES and actor_role != "system":
            raise AuditError(
                f"{reason_code} may only be used by the deterministic policy; a person must "
                "name a substantive reason"
            )
        if action == "request_information" and reason_code != "RC_INSUFFICIENT_EVIDENCE":
            raise AuditError(
                "requesting information must be recorded as RC_INSUFFICIENT_EVIDENCE so the "
                "reason a case is waiting is never ambiguous"
            )

        when = decided_at or datetime.now().replace(microsecond=0)
        agreed = (
            bool(ai_recommended_action) and ai_recommended_action == action
            if ai_recommended_action else None
        )
        decision_id = hashlib.sha1(
            f"{case_id}|{actor_id}|{action}|{when.isoformat()}".encode()
        ).hexdigest()[:20]

        self.con.execute(
            """
            INSERT INTO audit.decisions
                (decision_id, case_id, transaction_id, actor_role, actor_id, action,
                 reason_code, note, ai_recommended_action, ai_confidence, agreed_with_ai,
                 previous_case_state, new_case_state, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (decision_id) DO NOTHING
            """,
            [decision_id, case_id, transaction_id, actor_role, actor_id, action, reason_code,
             note, ai_recommended_action, ai_confidence, agreed, previous_case_state,
             new_case_state, when],
        )
        self.append(
            actor_role=actor_role,
            actor_id=actor_id,
            action=f"decision.{action}",
            object_type="case",
            object_id=case_id,
            summary=f"{actor_role} {actor_id} decided {action} ({reason_code})",
            payload={
                "transaction_id": transaction_id,
                "reason_code": reason_code,
                "note": note,
                "ai_recommended_action": ai_recommended_action,
                "ai_confidence": ai_confidence,
                "agreed_with_ai": agreed,
                "previous_case_state": previous_case_state,
                "new_case_state": new_case_state,
            },
            occurred_at=when,
        )
        return {
            "decision_id": decision_id,
            "case_id": case_id,
            "action": action,
            "reason_code": reason_code,
            "agreed_with_ai": agreed,
            "decided_at": when,
        }

    # -- reading -----------------------------------------------------------

    def entries(self, limit: int | None = None) -> pd.DataFrame:
        query = "SELECT * FROM audit.audit_log ORDER BY seq"
        if limit:
            query += f" LIMIT {int(limit)}"
        return self.con.execute(query).fetch_df()

    def verify_chain(self) -> ChainVerification:
        frame = self.entries()
        return verify(frame.to_dict("records"))

    def decisions(self) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM audit.decisions ORDER BY decided_at"
        ).fetch_df()

    def ai_invocations(self) -> pd.DataFrame:
        return self.con.execute(
            "SELECT * FROM audit.ai_invocations ORDER BY created_at"
        ).fetch_df()
