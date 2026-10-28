"""Optional run-scoped readback observation; never authorizes an API write."""

import copy

import sync_engine as se


class ObservedNotion:
    def __init__(self, client, observations, index):
        self.client = client
        self.observations = observations
        self.known = {}
        self.keys = {}
        self.pending = {}
        self.touched = set()
        for row in index.values():
            if se.read_select(row, "종류") == "Issue":
                self.known[se.identifier(row["id"])] = (
                    se.read_text(row, "동기화 키"), se.read_select(row, "작업 상태"))
                self.keys[se.identifier(row["id"])] = se.read_text(row, "동기화 키")

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _expectation(self, page_id, properties):
        # A stable page/key binding is not evidence of its current status.
        # Only an explicit new status operation can restore invalidated trust.
        if page_id is not None and page_id not in self.known and "작업 상태" not in properties:
            return None
        key, status = self.known.get(page_id, (self.keys.get(page_id), None))
        if "동기화 키" in properties:
            key = se._notion_value({"properties": properties}, "동기화 키", "rich_text")
        if "작업 상태" in properties:
            status = se._notion_value({"properties": properties}, "작업 상태", "select")
        return key, status, copy.deepcopy(properties)

    def request(self, method, path, payload=None, **kwargs):
        is_patch = method == "PATCH" and path.startswith("/pages/")
        is_create = method == "POST" and path == "/pages"
        page_id = None
        expectation = None
        if is_patch:
            page_id = se.identifier(path[len("/pages/"):])
            self.touched.add(page_id)
            self.observations.discard(page_id)
            self.pending.pop(page_id, None)
            expectation = self._expectation(page_id, (payload or {}).get("properties", {}))
            self.known.pop(page_id, None)
        elif is_create:
            expectation = self._expectation(None, (payload or {}).get("properties", {}))
        try:
            result = self.client.request(method, path, payload, **kwargs)
        except Exception:
            if method == "GET" and path.startswith("/pages/"):
                page_id = se.identifier(path[len("/pages/"):])
            if page_id:
                self.pending.pop(page_id, None)
                self.known.pop(page_id, None)
                self.observations.discard(page_id)
            raise
        if is_create:
            page_id = se.identifier(result.get("id"))
            self.touched.add(page_id)
            self.observations.discard(page_id)
        if expectation is not None:
            key, status, _ = expectation
            if isinstance(key, str) and se.valid_key(key):
                self.pending[page_id] = expectation
        elif method == "GET" and path.startswith("/pages/"):
            page_id = se.identifier(path[len("/pages/"):])
            expected = self.pending.pop(page_id, None)
            if expected:
                key, status, properties = expected
                try:
                    se.bound_page(result, self.observations.source_id)
                    se.require(se.identifier(result.get("id")) == page_id and
                               not se.is_archived(result), "Observation readback identity mismatch")
                    se._verify_properties(result, properties)
                    se.require(se.read_text(result, "동기화 키") == key and
                               se.read_select(result, "작업 상태") == status,
                               "Observation status/key readback mismatch")
                    # Status handling may be confirmed before the engine has
                    # persisted a recordable internal state on a new page.
                    self.known[page_id] = (key, status)
                    self.keys[page_id] = key
                    self.observations.record_handled(result, page_id=page_id,
                        key_at_handling=key, status_at_handling=status,
                        outcome="handled", confirmed=True)
                except (se.SyncError, KeyError, TypeError, AttributeError):
                    self.observations.discard(page_id)
        return result

    def record_untouched(self, rows):
        """Engine-selected no-op rows only; never rehabilitate an uncertain write."""
        for row, state in rows:
            page_id = se.identifier(row["id"])
            if page_id in self.touched:
                continue
            key = se.read_text(row, "동기화 키")
            status = se.read_select(row, "작업 상태")
            try:
                latest = self.client.request("GET", f"/pages/{page_id}")
                # This full state digest must still match the engine's handled row.
                se.require(se.read_text(latest, "동기화 내부 상태") ==
                           se.read_text(row, "동기화 내부 상태"),
                           "Observation no-op state drift")
                self.observations.record_handled(latest, page_id=page_id,
                    key_at_handling=key, status_at_handling=status,
                    outcome="blocked" if state and state.get("hold") else "handled",
                    confirmed=True)
            except Exception:
                # Observation failures do not alter the ordinary sync's outcome.
                self.observations.discard(page_id)
