"""Private, data-only checkpoints for handled Notion status values.

This module does not authorize a run, write a page, or attest convergence. The
engine must supply its last handled status after verified readback. A collector
may recheck that checkpoint, but must not create one from an arbitrary latest
page. Workflow authentication and engine hooks are separate integration work.
"""

from dataclasses import asdict, dataclass
import hashlib

import github_project as gp
import sync_engine as se


@dataclass(frozen=True)
class EchoCheckpoint:
    page_id: str
    key: str
    repository_id: int
    project_id: str
    item_id: str | None
    status: str | None
    state_sha256: str
    outcome: str
    kind: str = "echo_observation"


def _validated_snapshot(page, source_id, project_id):
    se.bound_page(page, source_id)
    se.require(not se.is_archived(page), "관측 대상이 보관 상태입니다")
    se.require(se.read_select(page, "종류") == "Issue", "관측 대상 종류 오류")
    key = se.read_text(page, "동기화 키")
    se.require(se.valid_key(key), "관측 대상 키 결속 오류")
    match = se.KEY_RE.fullmatch(key)
    issue_id = int(match[2])
    status = se.read_select(page, "작업 상태")
    se.require(status is None or status in se.OPTIONS["작업 상태"],
               "관측 대상 상태 오류")
    raw = se.read_text(page, "동기화 내부 상태")
    state = se.decode_internal(raw, kind="issue", project_id=project_id,
                               object_id=issue_id)
    se.require(state is not None, "관측 대상 내부 상태가 없습니다")
    # Valid unresolved markers remain untouched. This proves only the engine's
    # confirmed handling of current input; it does not resolve an older write.
    # An unconfirmed handling must instead invalidate the checkpoint below.
    return {
        "page_id": se.identifier(page["id"]),
        "key": key,
        "repository_id": gp.REPOSITORY_ID,
        "project_id": project_id,
        "item_id": state.get("project_item_id"),
        "status": status,
        # Hash the exact stored text, not a canonicalized reconstruction.
        "state_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }, state


class RunObservations:
    """All handled rows in one run, including rows outside its receipt list."""

    def __init__(self, *, source_id, project_id):
        self.source_id = se.identifier(source_id)
        se.require(project_id == gp.EXPECTED_PROJECT_ID, "관측 Project 결속 오류")
        self.project_id = project_id
        self._rows = {}
        self._key_pages = {}
        self._invalid_keys = set()

    def discard(self, page_id):
        """Invalidate a stale checkpoint when a subsequent write is uncertain."""
        page_id = se.identifier(page_id)
        old = self._rows.pop(page_id, None)
        if old and self._key_pages.get(old.key) == page_id:
            self._key_pages.pop(old.key)

    def record_handled(self, page, *, page_id, key_at_handling, status_at_handling,
                       outcome, confirmed):
        """Record engine evidence only; mismatch invalidates prior evidence.

        The key and status come from the engine's decision, not from a later
        collector GET. The caller must first verify the operation/readback.
        Project holds can be echo observations, never reflection attestations.
        """
        page_id = se.identifier(page_id)
        self.discard(page_id)
        if (confirmed is not True or not isinstance(outcome, str) or
                outcome not in {"handled", "blocked"}):
            return False
        try:
            value, state = _validated_snapshot(page, self.source_id, self.project_id)
            if (value["page_id"] != page_id or value["key"] != key_at_handling or
                    value["status"] != status_at_handling):
                return False
            key = value["key"]
            if key in self._invalid_keys:
                return False
            existing_page = self._key_pages.get(key)
            if existing_page and existing_page != page_id:
                self.discard(existing_page)
                self._invalid_keys.add(key)
                return False
            if state.get("hold") is not None:
                outcome = "blocked"
            self._rows[page_id] = EchoCheckpoint(**value, outcome=outcome)
            self._key_pages[key] = page_id
            return True
        except (se.SyncError, KeyError, TypeError, AttributeError):
            return False

    def recheck(self, page):
        """Return matching evidence without learning a new latest value."""
        try:
            value, _ = _validated_snapshot(page, self.source_id, self.project_id)
            checkpoint = self._rows.get(value["page_id"])
            if checkpoint is None:
                return None
            if any(value[name] != getattr(checkpoint, name) for name in value):
                return None
            return asdict(checkpoint)
        except (se.SyncError, KeyError, TypeError, AttributeError):
            return None

    def snapshot(self):
        """Return copies of every checkpoint; the transport must paginate."""
        return [asdict(self._rows[page_id]) for page_id in sorted(self._rows)]
