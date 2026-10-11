"""Bounded GitHub-Date clock readings for sync snapshots and consumers."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime, parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid
import time


DATE_PRECISION_SECONDS = 1
MAX_UNCERTAINTY_SECONDS = 60
MAX_LOCAL_CLOCK_STEP_SECONDS = 1


@dataclass(frozen=True)
class SnapshotMark:
    monotonic_ns: int
    utc: datetime


def _utc_now():
    return datetime.now(timezone.utc)


def mark_snapshot(*, monotonic_ns=time.monotonic_ns, utc_now=_utc_now):
    """Mark the instant a complete/partial sync snapshot has been finalized."""
    mono = monotonic_ns()
    wall = utc_now()
    if not isinstance(wall, datetime) or wall.tzinfo is None:
        raise ValueError("Snapshot clock must be timezone-aware")
    return SnapshotMark(mono, wall.astimezone(timezone.utc))


def _parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is None or parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        if format_datetime(parsed, usegmt=True) != value:
            return None
        return parsed
    except (TypeError, ValueError, OverflowError):
        return None


def _wall_step_ok(wall_delta, mono_delta):
    return abs(wall_delta.total_seconds() - mono_delta) <= MAX_LOCAL_CLOCK_STEP_SECONDS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_fresh_date(client):
    """Fetch Date and cache evidence through a dedicated no-cache API request.

    It deliberately bypasses the normal GitHub API wrappers so their unrelated
    request behavior and return shapes stay unchanged.
    """
    try:
        if hasattr(client, "headers") and hasattr(client, "opener"):
            headers = dict(client.headers)
            opener = client.opener
        elif isinstance(getattr(client, "token", None), str) and client.token:
            headers = {"Authorization": "Bearer " + client.token,
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"}
            opener = urllib.request.build_opener(_NoRedirect())
        else:
            return None
        headers.update({"Cache-Control": "no-cache, no-store, max-age=0",
                        "Pragma": "no-cache", "User-Agent": "Replica-observation-clock"})
        query = urllib.parse.urlencode({"_clock_nonce": uuid.uuid4().hex})
        request = urllib.request.Request(
            "https://api.github.com/rate_limit?" + query,
            headers=headers, method="GET")
        with opener.open(request, timeout=10) as response:
            if response.status != 200 or urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
                return None
            response.read(1024)
            reply = response.headers
            return {"date": reply.get("Date"), "cache_control": reply.get("Cache-Control"),
                    "age": reply.get("Age"), "x_cache": reply.get("X-Cache")}
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def verify_snapshot(mark, fetch_date, *, monotonic_ns=time.monotonic_ns, utc_now=_utc_now):
    """Return (UTC ISO timestamp, integer uncertainty) or (None, None).

    `fetch_date` performs a new GitHub request and returns Date plus freshness
    headers. A response Cache-Control `no-cache` directive is mandatory. The
    full request RTT, sample age, Date precision and local-clock consistency
    contribute to the conservative uncertainty bound.
    """
    if not isinstance(mark, SnapshotMark) or not callable(fetch_date):
        return None, None
    try:
        before_m = monotonic_ns()
        before_u = utc_now().astimezone(timezone.utc)
        if not _wall_step_ok(before_u - mark.utc, (before_m - mark.monotonic_ns) / 1e9):
            return None, None
        evidence = fetch_date()
        after_m = monotonic_ns()
        after_u = utc_now().astimezone(timezone.utc)
        if after_m < before_m or not _wall_step_ok(after_u - before_u, (after_m - before_m) / 1e9):
            return None, None
        if not isinstance(evidence, dict) or set(evidence) != {
                "date", "cache_control", "age", "x_cache"}:
            return None, None
        age, cache = evidence["age"], evidence["x_cache"]
        cache_control = evidence["cache_control"]
        directives = {item.strip().lower() for item in cache_control.split(",")} \
            if isinstance(cache_control, str) else set()
        if "no-cache" not in directives:
            return None, None
        cache_tokens = {part.strip().upper() for part in cache.split(",")} if isinstance(cache, str) else set()
        cache_tokens |= {part.strip().upper() for part in cache.split()} if isinstance(cache, str) else set()
        if age is not None and (not isinstance(age, str) or not age.isdigit() or int(age) != 0):
            return None, None
        if any("HIT" in token or "STALE" in token for token in cache_tokens):
            return None, None
        server_date = _parse_date(evidence["date"])
        if server_date is None:
            return None, None
        midpoint_m = (before_m + after_m) // 2
        delta = (mark.monotonic_ns - midpoint_m) / 1e9
        estimate = server_date + timedelta(seconds=delta)
        rtt = (after_m - before_m) / 1e9
        age = abs(delta)
        skew = abs((mark.utc - estimate).total_seconds())
        bound = DATE_PRECISION_SECONDS + rtt + age + skew
        uncertainty = int(bound) if bound.is_integer() else int(bound) + 1
        if uncertainty > MAX_UNCERTAINTY_SECONDS:
            return None, None
        # Keep the projected fractional second: dropping it here would add up
        # to almost another second of output error that the bound omits.
        return estimate.isoformat(timespec="auto").replace("+00:00", "Z"), uncertainty
    except Exception:
        # Clock evidence is optional; losing it must not invalidate a hold snapshot.
        return None, None
