"""Bounded HTTP, immutable report input and bot-owned state transport."""
import base64
import hashlib
import io
import json
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import notification_report as nr

REPO = "Aurelia-aurity/Replica"
REPO_ID = nr.REPO_ID
BRANCH = "automation/discord-state"
STATE_FILE = "discord-state.json"
BOT = {"name": "github-actions[bot]",
       "email": "41898282+github-actions[bot]@users.noreply.github.com"}
MAX_STATE = 8_000_000
CHANNEL_ID = "1558167686582894713"


class Error(Exception):
    """Only fixed diagnostics; never include credential-bearing URLs/responses."""


class HTTPError(Error):
    def __init__(self, status, headers=None, body=b""):
        super().__init__("Remote request rejected")
        self.status, self.headers, self.body = status, headers or {}, body


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def json_data(data):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise Error("Duplicate JSON key")
            result[k] = v
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Error("Invalid JSON data") from None


def validate_state(state):
    if (not isinstance(state, dict) or set(state) !=
            {"schema", "repository_id", "seen", "outbox", "incidents", "holds", "sync_cursor", "invalidated"} or
            type(state["schema"]) is not int or state["schema"] != 1 or state["repository_id"] != REPO_ID):
        raise Error("Invalid ledger schema")
    if (not isinstance(state["seen"], list) or len(state["seen"]) != len(set(state["seen"])) or
            any(not isinstance(k, str) or len(k) > 250 for k in state["seen"]) or
            not all(isinstance(state[k], dict) for k in ("outbox", "incidents", "holds"))):
        raise Error("Invalid ledger structure")
    if (not isinstance(state["invalidated"], list) or
            any(not isinstance(k, str) or k not in state["outbox"] for k in state["invalidated"]) or
            len(state["invalidated"]) != len(set(state["invalidated"]))):
        raise Error("Invalid retired notification references")
    cursor = state["sync_cursor"]
    if cursor is not None and (not isinstance(cursor, list) or len(cursor) != 3 or
                              any(type(n) is not int or n <= 0 for n in cursor)):
        raise Error("Invalid sync ledger cursor")
    confirmed_ids = set()
    for key, item in state["outbox"].items():
        if (not isinstance(key, str) or len(key) > 250 or not isinstance(item, dict) or
                set(item) != {"payload", "hash", "status", "message_id", "dependencies"} or
                item["status"] not in ("ready", "pending", "rejected", "delivered", "retired")):
            raise Error("Invalid outbox record")
        msg = item["payload"]
        if not isinstance(msg, dict) or set(msg) != {"content", "allowed_mentions"}:
            raise Error("Invalid outbox message")
        mentions = msg["allowed_mentions"]
        if (not isinstance(msg["content"], str) or not 0 < len(msg["content"]) < 1900 or
                not msg["content"].endswith("\n알림 ID: " + digest(key)) or
                not isinstance(mentions, dict) or set(mentions) != {"parse", "users", "replied_user"} or
                mentions["parse"] != [] or mentions["replied_user"] is not False or
                not isinstance(mentions["users"], list) or len(mentions["users"]) > 100 or
                any(not isinstance(n, str) or not re.fullmatch(r"[0-9]{17,20}", n) for n in mentions["users"]) or
                item["hash"] != digest(msg)):
            raise Error("Invalid outbox message integrity")
        mid = item["message_id"]
        if item["status"] == "delivered":
            if not isinstance(mid, str) or not re.fullmatch(r"[0-9]{17,20}", mid) or mid in confirmed_ids:
                raise Error("Invalid confirmed message ID")
            confirmed_ids.add(mid)
        elif mid is not None:
            raise Error("Invalid unconfirmed message ID")
        deps = item["dependencies"]
        if not isinstance(deps, list) or any(not isinstance(d, str) or d not in state["outbox"] or d == key for d in deps):
            raise Error("Invalid notification dependency")
    for item in state["incidents"].values():
        if not isinstance(item, dict) or set(item) != {"alert"} or item["alert"] not in state["outbox"]:
            raise Error("Invalid incident reference")
    for n, item in state["holds"].items():
        if (not re.fullmatch(r"[1-9][0-9]*", n) or not isinstance(item, dict) or
                set(item) != {"reasons"} or not isinstance(item["reasons"], dict)):
            raise Error("Invalid hold ledger")
        for code, reason in item["reasons"].items():
            if (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) or not isinstance(reason, dict) or
                    set(reason) != {"streak", "alert"} or type(reason["streak"]) is not int or
                    not 0 <= reason["streak"] <= 2 or
                    reason["alert"] is not None and reason["alert"] not in state["outbox"]):
                raise Error("Invalid hold reason ledger")
    return state


def request(url, *, method="GET", token=None, payload=None, limit=MAX_STATE):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Replica-notifications",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else canonical(payload)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise Error("Response size limit exceeded")
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        raise HTTPError(exc.code, dict(exc.headers), exc.read(10000)) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise Error("Remote request outcome unknown") from None


class GitHub:
    def __init__(self, token):
        if not token:
            raise Error("GitHub credential missing")
        self.token = token

    def call(self, path, *, method="GET", payload=None):
        raw_path = path.split("?", 1)[0]
        decoded = urllib.parse.unquote(raw_path)
        if (any(part in {".", ".."} for part in decoded.split("/")) or
                decoded.count("/") != raw_path.count("/") or "\\" in decoded or
                any(ord(char) < 32 or ord(char) == 127 for char in decoded) or
                re.search(r"%[0-9a-fA-F]{2}", decoded)):
            raise Error("Invalid API path")
        compare = re.match(r"^/repos/[^/]+/[^/]+/compare", decoded)
        exact_compare = re.fullmatch(r"/repos/" + re.escape(REPO) +
                                    r"/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}", path)
        if (not path.startswith("/") or "\n" in path or
                compare and (not exact_compare or method != "GET" or payload is not None) or
                not compare and ".." in path):
            raise Error("Invalid API path")
        _, headers, body = request("https://api.github.com" + path,
                                   method=method, token=self.token, payload=payload)
        return json_data(body), headers

    def repo(self, suffix, **kwargs):
        return self.call("/repos/" + REPO + suffix, **kwargs)[0]

    def pages(self, suffix, field=None):
        results = []
        separator = "&" if "?" in suffix else "?"
        total = None
        for page in range(1, 102):
            value, headers = self.call("/repos/" + REPO + suffix +
                                       f"{separator}per_page=100&page={page}")
            items = value.get(field) if field else value
            if not isinstance(items, list) or len(items) > 100:
                raise Error("Invalid paginated response")
            if field and "total_count" in value:
                if type(value["total_count"]) is not int or value["total_count"] > 10000:
                    raise Error("Source pagination limit exceeded")
                total = value["total_count"] if total is None else total
            results.extend(items)
            if len(results) > 10000:
                raise Error("Source pagination limit exceeded")
            link = headers.get("Link", headers.get("link", ""))
            if 'rel="next"' not in link:
                if total is not None and len(results) != total:
                    raise Error("Incomplete paginated response")
                return results
        raise Error("Source pagination incomplete")


class Ledger:
    def __init__(self, github):
        self.gh, self.sha = github, None
        info = github.repo("")
        if info.get("id") != REPO_ID or info.get("full_name") != REPO:
            raise Error("Repository identity mismatch")

    def load(self):
        try:
            value = self.gh.repo(f"/contents/{STATE_FILE}?ref={BRANCH}")
        except HTTPError as exc:
            if exc.status != 404:
                raise
            # Missing file on an existing branch is corruption, never bootstrap.
            try:
                self.gh.repo(f"/git/ref/heads/{BRANCH}")
            except HTTPError as missing:
                if missing.status == 404:
                    return None
                raise
            raise Error("Activated ledger file missing") from None
        if value.get("type") != "file" or value.get("path") != STATE_FILE or value.get("encoding") != "base64":
            raise Error("Invalid ledger file")
        try:
            data = base64.b64decode(value["content"], validate=False)
        except (ValueError, KeyError):
            raise Error("Invalid ledger encoding") from None
        if len(data) > MAX_STATE or not re.fullmatch(r"[0-9a-f]{40}", value.get("sha", "")):
            raise Error("Invalid ledger size or SHA")
        self.sha = value["sha"]
        state = json_data(data)
        return validate_state(state)

    def save(self, state, *, bootstrap=False):
        validate_state(state)
        data = canonical(state)
        if len(data) > MAX_STATE:
            raise Error("Ledger size limit exceeded")
        if self.sha is None:
            if not bootstrap:
                raise Error("Ledger bootstrap requires explicit dispatch")
            blob = self.gh.repo("/git/blobs", method="POST",
                                payload={"content": base64.b64encode(data).decode(), "encoding": "base64"})
            tree = self.gh.repo("/git/trees", method="POST", payload={"tree": [
                {"path": STATE_FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
            commit = self.gh.repo("/git/commits", method="POST", payload={
                "message": "Initialize Discord notification state", "tree": tree["sha"],
                "parents": [], "author": BOT, "committer": BOT})
            try:
                self.gh.repo("/git/refs", method="POST", payload={"ref": "refs/heads/" + BRANCH,
                                                               "sha": commit["sha"]})
            except Error:
                # Ref POST response loss may have committed; verify exact intended state.
                pass
        else:
            self.gh.repo("/contents/" + STATE_FILE, method="PUT", payload={
                "message": "Update Discord notification state", "branch": BRANCH,
                "content": base64.b64encode(data).decode(), "sha": self.sha,
                "author": BOT, "committer": BOT})
        if canonical(self.load()) != data:
            raise Error("Ledger readback mismatch")


class Discord:
    def __init__(self, url, channel_id):
        p = urllib.parse.urlsplit(url)
        if (p.scheme != "https" or p.netloc != "discord.com" or p.query or p.fragment or
                not re.fullmatch(r"/api/webhooks/[0-9]{17,20}/[A-Za-z0-9._-]{20,200}", p.path)):
            raise Error("Invalid Discord webhook configuration")
        if channel_id != CHANNEL_ID:
            raise Error("Discord channel differs from approved destination")
        self.url, self.channel_id = url, channel_id

    def verify(self):
        _, _, body = request(self.url, limit=100000)
        info = json_data(body)
        if info.get("guild_id") != "1554806404320075786" or info.get("channel_id") != self.channel_id:
            raise Error("Discord destination mismatch")

    def send(self, payload, *, retry_rate_limit=True):
        for attempt in range(3 if retry_rate_limit else 1):
            try:
                _, _, body = request(self.url + "?wait=true", method="POST", payload=payload, limit=100000)
                return self.validate_message(json_data(body), payload)
            except HTTPError as exc:
                if retry_rate_limit and exc.status == 429 and attempt < 2:
                    try:
                        seconds = float(json_data(exc.body)["retry_after"])
                    except (KeyError, ValueError, TypeError, Error):
                        raise Error("Discord rate limit outcome unresolved") from None
                    if not 0 <= seconds <= 10:
                        raise Error("Discord rate limit retry bound exceeded")
                    time.sleep(seconds)
                    continue
                if 400 <= exc.status < 500:
                    raise HTTPError(exc.status) from None
                raise Error("Discord delivery outcome unknown") from None
        raise Error("Discord delivery outcome unknown")

    def validate_message(self, message, payload):
        mid = message.get("id", "")
        if (not isinstance(mid, str) or not re.fullmatch(r"[0-9]{17,20}", mid) or
                message.get("channel_id") != self.channel_id or
                message.get("content") != payload["content"] or
                str(message.get("webhook_id")) != self.url.split("/")[-2]):
            raise Error("Discord confirmation mismatch")
        return mid

    def find(self, mid, payload):
        if not re.fullmatch(r"[0-9]{17,20}", mid or ""):
            raise Error("Invalid Discord message ID")
        _, _, body = request(self.url + "/messages/" + mid, limit=100000)
        return self.validate_message(json_data(body), payload)


def report_zip(data):
    if len(data) > nr.MAX_BYTES:
        raise Error("Artifact size limit exceeded")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise Error("Invalid artifact members")
            member = members[0]
            mode = member.external_attr >> 16
            if (member.filename != nr.FILE_NAME or member.is_dir() or member.flag_bits & 1 or
                    member.file_size > nr.MAX_BYTES or member.compress_size > nr.MAX_BYTES or
                    stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise Error("Invalid artifact member")
            return nr.validate(json_data(archive.read(member)))
    except (zipfile.BadZipFile, ValueError, RuntimeError):
        raise Error("Invalid notification artifact") from None


def artifact_report(gh, run):
    rid, attempt = run["id"], run["run_attempt"]
    latest = gh.repo(f"/actions/runs/{rid}")
    if latest.get("run_attempt") != attempt or latest.get("status") != "completed":
        raise Error("Sync attempt changed")
    artifacts = gh.pages(f"/actions/runs/{rid}/artifacts", "artifacts")
    matches = [a for a in artifacts if a.get("name") == f"notion-notification-{rid}-{attempt}"]
    if len(matches) != 1:
        raise Error("Current sync artifact missing or ambiguous")
    a = matches[0]
    identity = a.get("workflow_run") or {}
    if (a.get("expired") is not False or not 0 < a.get("size_in_bytes", 0) <= nr.MAX_BYTES or
            identity.get("id") != rid or identity.get("head_sha") != run["head_sha"] or
            identity.get("repository_id") != REPO_ID or identity.get("head_repository_id") != REPO_ID):
        raise Error("Sync artifact identity mismatch")
    aid = a["id"]
    if type(aid) is not int or aid <= 0:
        raise Error("Invalid artifact ID")
    try:
        _, _, data = request(f"https://api.github.com/repos/{REPO}/actions/artifacts/{aid}/zip",
                              token=gh.token, limit=nr.MAX_BYTES)
    except HTTPError as redirect:
        if redirect.status != 302:
            raise
        location = redirect.headers.get("Location", redirect.headers.get("location", ""))
        parsed = urllib.parse.urlsplit(location)
        # GitHub's signed artifact storage; never forward the GitHub credential.
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or
                not (parsed.hostname or "").endswith(".blob.core.windows.net")):
            raise Error("Untrusted artifact download destination")
        _, _, data = request(location, limit=nr.MAX_BYTES)
    report = nr.validate(report_zip(data), run_id=rid, attempt=attempt, sha=run["head_sha"])
    again = gh.repo(f"/actions/runs/{rid}")
    if any(again.get(k) != latest.get(k) for k in ("run_attempt", "status", "conclusion", "head_sha")):
        raise Error("Sync attempt changed during download")
    return report
