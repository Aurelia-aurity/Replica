"""Small, stateless Discord delivery and read-only GitHub API helpers."""
import html
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "Aurelia-aurity/Replica"
PROJECT_URL = "https://github.com/users/Just-Simple0/projects/4/views/1"
MAX_DOWNLOAD = 4_000_000


class Error(RuntimeError):
    """A safe-to-log notification error (never contains a credential or URL)."""


def escape(text: str, limit: int = 200) -> str:
    text = str(text)[:limit].replace("\r", " ").replace("\n", " ")
    text = text.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    return re.sub(r"([\\`*_~|\[\]])", r"\\\1", text)


def user_map(raw: str) -> dict:
    data = json.loads(raw or "{}")
    if not isinstance(data, dict) or len(data) > 100:
        raise Error("Invalid Discord user mapping")
    normalized = {}
    for login, uid in data.items():
        if (not isinstance(login, str) or
                not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or
                not isinstance(uid, str) or not re.fullmatch(r"[0-9]{17,20}", uid) or
                login.lower() in normalized):
            raise Error("Invalid Discord user mapping")
        normalized[login.lower()] = uid
    return normalized


def payload(label: str, description: str, url: str, mapping: dict,
            *, recipients=()) -> dict:
    if not re.fullmatch(
            r"https://github\.com/Aurelia-aurity/Replica/"
            r"(?:issues|pull|actions/runs)/[1-9][0-9]*", url):
        raise Error("Invalid notification source link")
    mentions, names = [], []
    for login in dict.fromkeys(recipients):
        uid = mapping.get(login.lower())
        if uid:
            mentions.append(uid)
            names.append(f"<@{uid}>")
        else:
            names.append(escape(login, 80))
    content = f"{' '.join(names)}\n{label}\n{description}\n{url}".strip()
    if len(content) >= 1900:
        raise Error("Notification content too large")
    return {"content": content, "allowed_mentions": {
        "parse": [], "users": list(dict.fromkeys(mentions)), "replied_user": False}}


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    """Signed artifact URLs must never receive the GitHub bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise Error("Unsafe artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


class Client:
    """Only GETs the fixed repository; pagination and artifact reads are bounded."""

    def __init__(self, token: str = "", *, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(ArtifactRedirect()).open

    def download(self, path: str) -> bytes:
        if not path.startswith(f"/repos/{REPO}/"):
            raise Error("Invalid GitHub API path")
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener(request, timeout=30) as response:
                data = response.read(MAX_DOWNLOAD + 1)
        except (urllib.error.URLError, OSError):
            raise Error("GitHub API request failed") from None
        if len(data) > MAX_DOWNLOAD:
            raise Error("GitHub API response too large")
        return data

    def get(self, path: str):
        return json.loads(self.download(path))

    def pages(self, path: str, key: str | None = None):
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            data = self.get(f"{path}{separator}per_page=100&page={page}")
            rows = data[key] if key else data
            if not isinstance(rows, list):
                raise Error("Invalid GitHub API list")
            yield from rows
            if len(rows) < 100:
                return
            page += 1


def mode(env) -> str:
    value = env.get("DISCORD_EVENTS_MODE", "") or "off"
    if value not in {"off", "shadow", "live"}:
        raise Error("Unknown DISCORD_EVENTS_MODE")
    return value


def post(url: str, message: dict, *, opener=urllib.request.urlopen,
         sleep=time.sleep) -> None:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.netloc not in {"discord.com", "discordapp.com"} or
            not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+", parts.path)):
        raise Error("Invalid Discord webhook configuration")
    query = urllib.parse.parse_qsl(parts.query)
    query = [(k, v) for k, v in query if k != "wait"] + [("wait", "true")]
    target = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    for attempt in range(3):
        request = urllib.request.Request(target, method="POST",
                                         data=json.dumps(message).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=30) as response:
                response.read()
            return
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = 1.0
            if status == 429:
                try:
                    retry_after = float(json.loads(exc.read(4096)).get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    retry_after = 1.0
            exc.close()
            if attempt == 2 or not (status == 429 or 500 <= status < 600):
                raise Error(f"Discord delivery failed (HTTP {status})") from None
            delay = min(60.0, max(0.0, retry_after)) if math.isfinite(retry_after) else 60.0
            sleep(delay)
        except (urllib.error.URLError, OSError):
            raise Error("Discord delivery failed (network error)") from None


def deliver(message: dict, env, *, pm: bool = False,
            opener=urllib.request.urlopen, sleep=time.sleep) -> None:
    selected = mode(env)
    if selected == "off":
        print("Discord alerts disabled (off)")
        return
    if selected == "shadow":
        url = env.get("DISCORD_SHADOW_WEBHOOK_URL", "")
        if not url:
            summary = env.get("GITHUB_STEP_SUMMARY")
            if not summary:
                raise Error("Shadow summary path missing")
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write("<pre>" + html.escape(json.dumps(message, ensure_ascii=False)) +
                             "</pre>\n")
            print("Shadow message written to step summary")
            return
    else:
        url = ((env.get("DISCORD_PM_WEBHOOK_URL") if pm else "") or
               env.get("DISCORD_WEBHOOK_URL", ""))
    if not url:
        raise Error("Discord webhook not configured")
    post(url, message, opener=opener, sleep=sleep)


def load_event(env) -> dict:
    if env.get("GITHUB_REPOSITORY") != REPO:
        raise Error("Unexpected repository")
    with Path(env["GITHUB_EVENT_PATH"]).open(encoding="utf-8") as stream:
        event = json.load(stream)
    if event["repository"]["full_name"] != REPO:
        raise Error("Unexpected event repository")
    return event
