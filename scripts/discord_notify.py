"""Reconcile trusted GitHub facts into Discord notifications; no Discord bot."""
import argparse
import copy
import datetime
import json
import os
import re
import sys
from pathlib import Path

import discord_transport as dt

REPO = dt.REPO
REPO_ID = dt.REPO_ID
SYNC_ID = 377351519
SYNC_PATH = ".github/workflows/notion-sync.yml"
NOTIFIER_PATH = ".github/workflows/discord-notify.yml"
PM = "Just-Simple0"
PM_ID = "97959897"
PROJECT_URL = "https://github.com/users/Just-Simple0/projects/4/views/1"
FAILURES = {"failure", "timed_out", "startup_failure", "action_required"}


def escape(text, limit=200):
    text = str(text)[:limit].replace("\r", " ").replace("\n", " ")
    text = text.replace("@", "＠").replace("<", "＜").replace(">", "＞")
    return re.sub(r"([\\`*_~|\[\]])", r"\\\1", text)


def user_map(raw):
    data = dt.json_data(raw)
    if not isinstance(data, dict) or len(data) > 100:
        raise dt.Error("Invalid Discord user mapping")
    normalized = {}
    for login, snowflake in data.items():
        if (not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or
                not isinstance(snowflake, str) or not re.fullmatch(r"[0-9]{17,20}", snowflake) or
                login.lower() in normalized):
            raise dt.Error("Invalid Discord user mapping")
        normalized[login.lower()] = snowflake
    return normalized


def payload(label, description, url, mapping, *, recipients=()):
    if not (re.fullmatch(r"https://github\.com/Aurelia-aurity/Replica/(?:issues|pull)/[1-9][0-9]*", url) or
            re.fullmatch(r"https://github\.com/Aurelia-aurity/Replica/actions/runs/[1-9][0-9]*", url)):
        raise dt.Error("Invalid notification source link")
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
        raise dt.Error("Notification content too large")
    return {"content": content, "allowed_mentions": {"parse": [], "users": list(dict.fromkeys(mentions)),
                                                     "replied_user": False}}


def new_state():
    return {"schema": 1, "repository_id": REPO_ID, "seen": [], "outbox": {},
            "incidents": {}, "holds": {}, "sync_cursor": None, "invalidated": []}


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def merge_fact(pull):
    if type(pull.get("merged")) is not bool or "merged_at" not in pull or pull.get("state") not in ("open", "closed"):
        raise dt.Error("Incomplete PR merge facts")
    at = pull["merged_at"]
    if not pull["merged"]:
        if at is not None:
            raise dt.Error("Inconsistent PR merge facts")
        return None
    if pull["state"] != "closed" or not isinstance(at, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", at):
        raise dt.Error("Invalid PR merge facts")
    try:
        datetime.datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise dt.Error("Invalid PR merge timestamp") from None
    return at


def invalidate(state, key):
    """Keep unknown delivery fences, but durably prohibit a later not_sent POST."""
    if key not in state["outbox"]:
        return
    state["invalidated"] = sorted(set(state["invalidated"]) | {key})
    if state["outbox"][key]["status"] == "ready":
        state["outbox"][key]["status"] = "retired"


def seal_message(key, message):
    sealed = copy.deepcopy(message)
    sealed["content"] += "\n알림 ID: " + dt.digest(key)
    if len(sealed["content"]) >= 1900:
        raise dt.Error("Notification content too large")
    return sealed


def queue(state, key, message, *, dependencies=()):
    sealed = seal_message(key, message)
    state["outbox"].setdefault(key, {"payload": sealed, "hash": dt.digest(sealed),
                                   "status": "ready", "message_id": None,
                                   "dependencies": list(dependencies)})


def request_identity(event):
    reviewer, team = event.get("requested_reviewer"), event.get("requested_team")
    if (reviewer is None) == (team is None):
        raise dt.Error("Invalid review request identity")
    if reviewer is not None:
        if not isinstance(reviewer, dict) or not isinstance(reviewer.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer["login"]):
            raise dt.Error("Invalid review request user")
        return "user", reviewer["login"]
    if not isinstance(team, dict) or not isinstance(team.get("slug"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", team["slug"]):
        raise dt.Error("Invalid review request team")
    return "team", team["slug"]


def current_reviewers(pull):
    result = []
    for key, attribute, pattern in (("requested_reviewers", "login", r"[A-Za-z0-9-]{1,39}"),
                                    ("requested_teams", "slug", r"[A-Za-z0-9_-]{1,100}")):
        if key not in pull or not isinstance(pull[key], list):
            raise dt.Error("Current review request list missing")
        names = set()
        for item in pull[key]:
            if (not isinstance(item, dict) or not isinstance(item.get(attribute), str) or
                    not re.fullmatch(pattern, item[attribute]) or item[attribute].casefold() in names):
                raise dt.Error("Invalid current review request identity")
            names.add(item[attribute].casefold())
        result.append(names)
    return result


def object_url(number, pull=False):
    if type(number) is not int or number <= 0:
        raise dt.Error("Invalid source number")
    return f"https://github.com/{REPO}/{'pull' if pull else 'issues'}/{number}"


def source_events(objects, timelines, mapping, backlog=()):
    """Return every semantic event, including suppressed requests for baseline IDs."""
    result = {}
    for obj in objects:
        pull = "head" in obj
        number = obj["number"]
        prefix = f"{'pr' if pull else 'issue'}:{number}"
        url = object_url(number, pull)
        heading = f"Replica · {'PR' if pull else 'Issue'} #{number} · {escape(obj['title'])}"
        created = "PR 생성" if pull else "Issue 생성"
        if not pull:
            created += (" · Project 백로그 등록 확인" if number in backlog else
                        " · Project 등록 미확인\nProject: " + PROJECT_URL)
        result[prefix + ":created:" + obj["created_at"]] = payload(heading, created, url, mapping)
        timeline = timelines[number]
        events = [x for x in timeline if x.get("event") in
                  {"closed", "reopened", "merged", "review_requested", "review_request_removed"}]
        events.sort(key=lambda x: (x["created_at"], x["id"]))
        latest_request = {}
        for event in events:
            if event["event"] in ("review_requested", "review_request_removed"):
                kind, name = request_identity(event)
                latest_request[(kind, name.casefold())] = event
        current_users, current_teams = current_reviewers(obj) if pull else (set(), set())
        merged_at = merge_fact(obj) if pull else None
        for event in events:
            action = event["event"]
            if type(event.get("id")) is not int or event["id"] <= 0:
                raise dt.Error("Invalid timeline identity")
            key = prefix + ":timeline:" + str(event["id"])
            if action == "merged" or pull and action == "closed" and merged_at and event["created_at"] >= merged_at:
                # Canonical REST merge below; keep timeline IDs consumed but silent.
                result[key] = None
            elif action in ("closed", "reopened"):
                label = ("병합 없이 PR 종료" if pull else "Issue 종료") if action == "closed" else "재오픈"
                result[key] = payload(heading, label, url, mapping)
            elif action == "review_requested" and pull:
                identity = request_identity(event)
                normalized = identity[0], identity[1].casefold()
                active = normalized[1] in (current_users if identity[0] == "user" else current_teams)
                if active and latest_request.get(normalized) == event:
                    names = [identity[1]] if identity[0] == "user" else []
                    description = "리뷰 요청 · 변경 사항을 확인하고 리뷰해주세요."
                    if identity[0] == "team":
                        description += " 팀: " + escape(identity[1], 80)
                    result[key] = payload(heading, description, url, mapping, recipients=names)
                else:
                    result[key] = None
            else:
                result[key] = None
        if merged_at:
            result[prefix + ":merged:" + merged_at] = payload(heading, "PR 병합", url, mapping)
    return result


def reconcile_events(state, events, *, bootstrap=False):
    seen = set(state["seen"])
    if not bootstrap:
        for key, message in events.items():
            if message is None and state["outbox"].get(key, {}).get("status") == "ready":
                # A crash before POST can leave a queued review request that was later cancelled.
                state["outbox"][key]["status"] = "retired"
            if key not in seen and message is not None:
                queue(state, key, message)
    state["seen"] = sorted(seen | events.keys())


def run_order(run):
    values = tuple(run[k] for k in ("run_number", "id", "run_attempt"))
    if any(type(x) is not int or x <= 0 for x in values):
        raise dt.Error("Invalid workflow run order")
    return values


def failure_episode(state, stream, failed, run, mapping, *, owner, label, remaining=""):
    incident = state["incidents"].get(stream)
    url = f"https://github.com/{REPO}/actions/runs/{run['id']}"
    if failed:
        if incident is None:
            for old in state["outbox"]:
                if old.startswith(f"recovery:failure:{stream}:"):
                    invalidate(state, old)
            key = f"failure:{stream}:{run['id']}:{run['run_attempt']}"
            queue(state, key, payload("Replica · " + label, "실패 · 실행 로그를 확인하고 조치해주세요.",
                                      url, mapping, recipients=[owner]))
            state["incidents"][stream] = {"alert": key}
    elif incident:
        invalidate(state, incident["alert"])
        key = "recovery:" + incident["alert"] + f":at:{run['id']}:{run['run_attempt']}"
        queue(state, key, payload("Replica · " + label,
                                  "실행 오류 복구" + remaining, url, mapping),
              dependencies=[incident["alert"]])
        del state["incidents"][stream]


def ci_verdict(run, pull, workflow, main_sha, *, historical=False):
    """True: proven association; False: proven mismatch; None: incomplete evidence."""
    if not isinstance(run, dict):
        return None
    # Exclude provably unrelated streams before considering missing evidence.
    if (type(run.get("workflow_id")) is int and run["workflow_id"] != workflow["id"] or
            isinstance(run.get("path"), str) and run["path"] != workflow["path"] or
            type((run.get("repository") or {}).get("id")) is int and run["repository"]["id"] != REPO_ID or
            isinstance(run.get("event"), str) and run["event"] != ("push" if pull is None else "pull_request") or
            pull is None and isinstance(run.get("head_branch"), str) and run["head_branch"] != "main" or
            pull is None and type((run.get("head_repository") or {}).get("id")) is int and run["head_repository"]["id"] != REPO_ID):
        return False
    associations = run.get("pull_requests")
    if pull is not None and isinstance(associations, list) and all(
            isinstance(a, dict) and type(a.get("number")) is int for a in associations):
        if not any(a["number"] == pull.get("number") for a in associations):
            return False
    if not valid_sha(run.get("head_sha")) or not valid_sha(main_sha):
        return None
    for field in ("workflow_id", "path", "event", "head_branch"):
        if field not in run or run[field] is None:
            return None
    for field in ("repository", "head_repository"):
        if type((run.get(field) or {}).get("id")) is not int:
            return None
    if (run["workflow_id"] != workflow["id"] or run["path"] != workflow["path"] or
            run["repository"]["id"] != REPO_ID):
        return False
    if pull is None:
        return (run["event"] == "push" and run["head_branch"] == "main" and
                (historical or run["head_sha"] == main_sha) and run["head_repository"]["id"] == REPO_ID)
    if pull.get("state") not in ("open", "closed"):
        return None
    if pull["state"] == "closed" or run["event"] != "pull_request":
        return False
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if (not valid_sha(head.get("sha")) or type((head.get("repo") or {}).get("id")) is not int or
            type((base.get("repo") or {}).get("id")) is not int or not isinstance(base.get("ref"), str)):
        return None
    if base["ref"] != "main" or base["repo"]["id"] != REPO_ID:
        return False
    matching = {head["sha"]}
    if valid_sha(pull.get("merge_commit_sha")):
        matching.add(pull["merge_commit_sha"])
    if not historical and run["head_sha"] not in matching:
        return False
    associations = run.get("pull_requests")
    if not isinstance(associations, list):
        return None
    incomplete = False
    for association in associations:
        if not isinstance(association, dict):
            incomplete = True
            continue
        ah, ab = association.get("head") or {}, association.get("base") or {}
        if (type(association.get("number")) is not int or not valid_sha(ah.get("sha")) or
                type((ah.get("repo") or {}).get("id")) is not int or
                type((ab.get("repo") or {}).get("id")) is not int or not isinstance(ab.get("ref"), str)):
            incomplete = True
            continue
        if (association["number"] == pull["number"] and ah["repo"]["id"] == head["repo"]["id"] and
                (historical or ah["sha"] == head["sha"]) and ab["repo"]["id"] == REPO_ID and ab["ref"] == "main"):
            return True
    return None if incomplete else False


def ci_matches(run, pull, workflow, main_sha):
    return ci_verdict(run, pull, workflow, main_sha) is True


def reconcile_ci(state, runs, pulls, workflows, main_sha, mapping):
    for workflow in workflows:
        for pull in [None] + pulls:
            stream = f"ci:{workflow['id']}:" + (f"pr:{pull['number']}" if pull else "main")
            if pull and pull.get("state") not in ("open", "closed"):
                continue
            if pull and pull["state"] == "closed":
                incident = state["incidents"].pop(stream, None)
                if incident:
                    invalidate(state, incident["alert"])
                for key, item in state["outbox"].items():
                    if key.startswith((f"failure:{stream}:", f"recovery:failure:{stream}:")):
                        invalidate(state, key)
                continue
            eligible = [r for r in runs if ci_matches(r, pull, workflow, main_sha)]
            if not eligible:
                continue
            latest = max(eligible, key=run_order)
            if any(ci_verdict(r, pull, workflow, main_sha) is None and run_order(r) >= run_order(latest)
                   for r in runs):
                continue
            if latest["status"] != "completed":
                continue
            conclusion = latest.get("conclusion")
            if conclusion in FAILURES:
                incident = state["incidents"].get(stream)
                if incident:
                    alert = incident["alert"]
                    item = state["outbox"].get(alert, {})
                    original_parts = alert.split(":")
                    old_id, old_attempt = int(original_parts[-2]), int(original_parts[-1])
                    original = next((r for r in runs if r["id"] == old_id), None)
                    obsolete = item.get("status") == "retired" or (original is not None and
                        (ci_verdict(original, pull, workflow, main_sha) is False or
                         original.get("run_attempt") != old_attempt))
                    if item.get("status") in ("ready", "retired") and obsolete:
                        invalidate(state, alert)
                        del state["incidents"][stream]
            if conclusion in FAILURES or conclusion == "success":
                failure_episode(state, stream, conclusion in FAILURES, latest, mapping,
                                owner=pull["user"]["login"] if pull else PM,
                                label=f"CI · {escape(workflow['name'], 100)}" +
                                      (f" · PR #{pull['number']}" if pull else " · main"))


def reconcile_sync(state, run, report, mapping, *, current_snapshot=True):
    """Consume all distinct runs, with unknowns interrupting unannounced streaks."""
    order = run_order(run)
    prior = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
    if prior and order <= prior:
        return
    same_run = prior and order[:2] == prior[:2]
    state["sync_cursor"] = list(order)
    full = bool(run.get("conclusion") == "success" and report and report["scan_complete"] and not report["dry_run"] and
                report["kind"] in ("complete", "partial"))
    failed = run.get("conclusion") in FAILURES
    if failed:
        failure_episode(state, "sync", True, run, mapping, owner=PM, label="Notion 동기화")
    if not full:
        for target in state["holds"].values():
            for reason in target["reasons"].values():
                reason["streak"] = 0
        return
    held = {}
    for item in report["holds"]:
        held.setdefault(str(item["issue_number"]), set()).add(item["reason_code"])
    if current_snapshot:
        failure_episode(state, "sync", False, run, mapping, owner=PM, label="Notion 동기화",
                        remaining=f" · 보류 {len(held)}건 남음" if held else " · 보류 없음")
    for number in list(state["holds"]):
        if number not in held:
            if not current_snapshot:
                for reason in state["holds"][number]["reasons"].values():
                    reason["streak"] = 0
                continue
            target = state["holds"].pop(number)
            alerts = [r["alert"] for r in target["reasons"].values() if r.get("alert")]
            if alerts:
                for alert in alerts:
                    invalidate(state, alert)
                queue(state, f"hold-recovery:{number}:{run['id']}:{run['run_attempt']}",
                      payload(f"Replica · Issue #{number}",
                              f"동기화 보류 해제 · 남은 보류 {len(held)}건",
                              object_url(int(number)), mapping), dependencies=alerts)
    for number, codes in held.items():
        for key in state["outbox"]:
            if key.startswith(f"hold-recovery:{number}:"):
                invalidate(state, key)
        target = state["holds"].setdefault(number, {"reasons": {}})
        for code, reason in target["reasons"].items():
            if code not in codes:
                reason["streak"] = 0
                if current_snapshot and reason["alert"]:
                    invalidate(state, reason["alert"])
        for code in sorted(codes):
            reason = target["reasons"].setdefault(code, {"streak": 0, "alert": None})
            # One workflow run is one observation, regardless of attempt count.
            if not same_run:
                reason["streak"] = min(2, reason["streak"] + 1)
            elif reason["streak"] == 0:
                reason["streak"] = 1
            if current_snapshot and reason["streak"] == 2 and not reason["alert"]:
                key = f"hold:{number}:{code}:{run['id']}"
                queue(state, key, payload(f"Replica · Issue #{number}",
                                          f"동기화 보류 · {escape(code, 80)}\n두 실행에서 계속 보류됐습니다. 원인을 확인해주세요.",
                                          object_url(int(number)), mapping, recipients=[PM]))
                reason["alert"] = key


def notice_active(state, key):
    if key in state["invalidated"]:
        return False
    if key.startswith("failure:"):
        stream = key[len("failure:"):].rsplit(":", 2)[0]
        return state["incidents"].get(stream, {}).get("alert") == key
    if key.startswith("recovery:failure:"):
        stream = key.split(":at:")[0][len("recovery:failure:"):].rsplit(":", 2)[0]
        return stream not in state["incidents"]
    if key.startswith("hold:"):
        _, number, code, _ = key.split(":")
        return state["holds"].get(number, {}).get("reasons", {}).get(code, {}).get("alert") == key
    if key.startswith("hold-recovery:"):
        return key.split(":")[1] not in state["holds"]
    return True


def deliver(state, ledger, discord, *, event_check=None, ci_check=None):
    errors = 0
    for key, item in state["outbox"].items():
        if item["hash"] != dt.digest(item["payload"]):
            raise dt.Error("Outbox integrity mismatch")
        if item["status"] != "ready":
            if item["status"] in ("pending", "rejected"):
                print("::warning::Discord delivery requires PM inspection: " + dt.digest(key)[:12])
                errors += 1
            continue
        if not notice_active(state, key):
            invalidate(state, key)
            ledger.save(state)
            continue
        if ci_check and key.startswith(("failure:ci:", "recovery:failure:ci:")):
            current = ci_check(key)
            if current is False:
                invalidate(state, key)
                ledger.save(state)
                continue
            if current is None:
                continue  # Incomplete/pending current CI evidence cannot authorize POST.
        deps = item["dependencies"]
        if deps and not any(state["outbox"].get(d, {}).get("status") == "delivered" for d in deps):
            continue
        if event_check and re.fullmatch(r"pr:[1-9][0-9]*:timeline:[1-9][0-9]*", key):
            current_message = event_check(key)
            if current_message is None:
                item["status"] = "retired"
                ledger.save(state)
                continue
            item["payload"] = seal_message(key, current_message)
            item["hash"] = dt.digest(item["payload"])
        item["status"] = "pending"
        ledger.save(state)  # A crash or ambiguous POST is fenced before sending.
        try:
            mid = discord.send(item["payload"])
            if any(other_key != key and other.get("message_id") == mid for other_key, other in state["outbox"].items()):
                raise dt.Error("Discord message ID already confirms another event")
        except dt.HTTPError:
            item["status"] = "rejected"
            ledger.save(state)
            errors += 1
            print("::warning::Discord rejected delivery: " + dt.digest(key)[:12])
            continue
        except dt.Error:
            errors += 1
            print("::warning::Discord delivery outcome unknown: " + dt.digest(key)[:12])
            continue
        item.update(status="delivered", message_id=mid)
        ledger.save(state)  # Failed save keeps the remote pending fence; abort run.
    return errors


def resolve(state, key, outcome, mid, discord):
    if key not in state["outbox"]:
        raise dt.Error("Unknown delivery key")
    item = state["outbox"][key]
    if item["status"] not in ("pending", "rejected"):
        raise dt.Error("Delivery does not require recovery")
    if outcome == "found":
        if any(other_key != key and other.get("message_id") == mid for other_key, other in state["outbox"].items()):
            raise dt.Error("Discord message ID already confirms another event")
        item.update(status="delivered", message_id=discord.find(mid, item["payload"]))
    elif outcome == "not_sent":
        item.update(status="ready", message_id=None)
    else:
        raise dt.Error("Delivery remains unknown")


def project_backlog(token, objects):
    if not token:
        return set()
    try:
        reader = dt.GitHub(token)
        _, headers = reader.call("/user")
        scopes = {x.strip() for x in headers.get("X-OAuth-Scopes", headers.get("x-oauth-scopes", "")).split(",")}
        if "read:project" not in scopes or "project" in scopes:
            return set()
        import github_project as gp
        graph = gp.GraphQLClient(token)
        # Existing verifier binds owner/project/status IDs, using reads only.
        repository_node = reader.repo("")["node_id"]
        project = gp.fetch_project(graph, project_id=gp.EXPECTED_PROJECT_ID,
                                   owner_id=gp.EXPECTED_PROJECT_OWNER_ID,
                                   status_field_id="PVTSSF_lAHOBda_2c4BmKKVzhk0RA4",
                                   status_options=gp.EXPECTED_STATUS_OPTIONS,
                                   repository_node_id=repository_node)
        return {i["number"] for i in objects if "head" not in i and
                (project["items"].get(i["id"]) or {}).get("status_option_id") == gp.EXPECTED_STATUS_OPTIONS["백로그"]}
    except Exception:
        return set()  # Optional observation must never claim unverified registration.


def collect_objects(gh):
    issues = [x for x in gh.pages("/issues?state=all") if "pull_request" not in x]
    # List PR response omits some review/merge facts; get authoritative full objects.
    pulls = [gh.repo(f"/pulls/{x['number']}") for x in gh.pages("/pulls?state=all")]
    for pull in pulls:
        if not isinstance(pull, dict) or "head" not in pull or "base" not in pull:
            raise dt.Error("Incomplete current PR response")
        current_reviewers(pull)
        merge_fact(pull)
    objects = issues + pulls
    timelines = {x["number"]: gh.pages(f"/issues/{x['number']}/timeline") for x in objects}
    return objects, pulls, timelines


def current_pr_event(gh, key, mapping):
    number = int(key.split(":")[1])
    pull = gh.repo(f"/pulls/{number}")
    if not isinstance(pull, dict) or "head" not in pull or "base" not in pull or pull.get("number") != number:
        raise dt.Error("Incomplete current PR response")
    timeline = gh.pages(f"/issues/{number}/timeline")
    events = source_events([pull], {number: timeline}, mapping)
    if key not in events:
        raise dt.Error("Current PR event not confirmed")
    return events[key]


def current_ci_notice(gh, key, workflows):
    """Revalidate delayed CI notices against current head and latest run before POST."""
    recovery = key.startswith("recovery:")
    parts = key.split(":at:")[0].removeprefix("recovery:").split(":")
    workflow = next((w for w in workflows if str(w["id"]) == parts[2]), None)
    if workflow is None:
        return False
    pull = gh.repo(f"/pulls/{parts[4]}") if parts[3] == "pr" else None
    if pull is not None and pull.get("state") == "closed":
        return False
    main_sha = gh.repo("/git/ref/heads/main")["object"]["sha"]
    if not valid_sha(main_sha):
        return None
    original = gh.repo(f"/actions/runs/{parts[-2]}")
    if not valid_sha(original.get("head_sha")) or pull is not None and not valid_sha((pull.get("head") or {}).get("sha")):
        return None
    verdict = ci_verdict(original, pull, workflow, main_sha, historical=recovery)
    if verdict is not True:
        return verdict
    if (type(original.get("id")) is not int or type(original.get("run_attempt")) is not int or
            original.get("id") != int(parts[-2]) or original.get("run_attempt") != int(parts[-1]) or
            original.get("status") != "completed" or original.get("conclusion") not in FAILURES):
        return None
    observed = gh.pages(f"/actions/workflows/{workflow['id']}/runs", "workflow_runs")
    fields = ("id", "run_attempt", "run_number", "head_sha", "status", "conclusion")
    listed_original = [r for r in observed if r.get("id") == original["id"]]
    if (len(listed_original) != 1 or
            ci_verdict(listed_original[0], pull, workflow, main_sha, historical=recovery) is not True or
            any(k not in original or k not in listed_original[0] or original[k] != listed_original[0][k] for k in fields)):
        return None
    eligible = [r for r in observed if ci_matches(r, pull, workflow, main_sha)]
    if not eligible:
        return None
    candidate = max(eligible, key=run_order)
    if any(ci_verdict(r, pull, workflow, main_sha) is None and run_order(r) >= run_order(candidate)
           for r in observed):
        return None
    latest = gh.repo(f"/actions/runs/{candidate['id']}")
    if any(k not in latest or k not in candidate or latest[k] != candidate[k] for k in fields):
        return None
    if recovery:
        if ":at:" not in key:
            return None
        bound_id, bound_attempt = (int(x) for x in key.split(":at:")[1].split(":"))
        boundary = gh.repo(f"/actions/runs/{bound_id}")
        listed_boundary = [r for r in observed if r.get("id") == bound_id]
        if (len(listed_boundary) != 1 or
                ci_verdict(listed_boundary[0], pull, workflow, main_sha, historical=True) is not True or
                any(k not in boundary or k not in listed_boundary[0] or boundary[k] != listed_boundary[0][k] for k in fields)):
            return None
        if (boundary.get("id") != bound_id or ci_verdict(boundary, pull, workflow, main_sha, historical=True) is not True or
                type(boundary.get("run_number")) is not int or boundary["run_number"] <= 0):
            return None
        if (boundary.get("run_attempt") != bound_attempt or boundary.get("status") != "completed" or
                boundary.get("conclusion") != "success"):
            return None
        bound_order = (boundary["run_number"], bound_id, bound_attempt)
        # Every later possibly related observation needs consistent detail proof.
        for prior in observed:
            if run_order(prior) <= bound_order:
                continue
            association = ci_verdict(prior, pull, workflow, main_sha, historical=True)
            if association is None:
                return None
            if association is False:
                continue
            detail = gh.repo(f"/actions/runs/{prior['id']}")
            if any(k not in detail or k not in prior or detail[k] != prior[k] for k in fields):
                return None
            if ci_verdict(detail, pull, workflow, main_sha, historical=True) is not True or detail.get("status") != "completed":
                return None
            if detail.get("conclusion") in FAILURES:
                return False
            if detail.get("conclusion") not in {"success", "cancelled", "neutral", "skipped"}:
                return None
    if main_sha != gh.repo("/git/ref/heads/main")["object"]["sha"]:
        return None
    if pull is not None:
        pull = gh.repo(f"/pulls/{parts[4]}")
        if pull.get("state") == "closed":
            return False
        if not valid_sha((pull.get("head") or {}).get("sha")):
            return None
        verdict = ci_verdict(original, pull, workflow, main_sha, historical=recovery)
        if verdict is not True:
            return verdict
    if not ci_matches(latest, pull, workflow, main_sha) or latest.get("status") != "completed":
        return None
    if latest.get("conclusion") not in FAILURES | {"success"}:
        return None
    return (latest["conclusion"] == "success") if recovery else (latest["conclusion"] in FAILURES)


def refresh_ci_runs(gh, runs, candidates):
    """A conflicting detail read must not change incident state before POST checks."""
    refreshed = {}
    fields = ("id", "run_attempt", "run_number", "head_sha", "status", "conclusion",
              "workflow_id", "path", "event", "head_branch", "repository", "head_repository")
    for rid, candidate in candidates.items():
        detail = gh.repo(f"/actions/runs/{rid}")
        compared = fields + (("pull_requests",) if candidate.get("event") == "pull_request" else ())
        if any(k not in detail or k not in candidate or detail[k] != candidate[k] for k in compared):
            refreshed[rid] = {**candidate, "status": "in_progress", "conclusion": None}
        else:
            refreshed[rid] = detail
    return [refreshed.get(r["id"], r) for r in runs]


def sync_context(gh, run, main_sha):
    if (run.get("workflow_id") != SYNC_ID or run.get("path") != SYNC_PATH or
            (run.get("repository") or {}).get("id") != REPO_ID or
            (run.get("head_repository") or {}).get("id") != REPO_ID or
            run.get("head_branch") != "main" or run.get("event") not in
            {"schedule", "issues", "pull_request_target", "workflow_dispatch"}):
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        return False
    if run["head_sha"] == main_sha:
        return True
    compare = gh.repo(f"/compare/{run['head_sha']}...{main_sha}")
    return (compare.get("status") in ("ahead", "identical") and
            (compare.get("base_commit") or {}).get("sha") == run["head_sha"])


def trusted_sync(gh, run, main_sha):
    if not sync_context(gh, run, main_sha):
        return False
    return run["event"] != "workflow_dispatch" or (
        (run.get("actor") or {}).get("id") == int(PM_ID) and
        (run.get("triggering_actor") or {}).get("id") == int(PM_ID) and run["run_attempt"] == 1)


def reconcile_sync_runs(state, gh, runs, main_sha, mapping):
    cursor = tuple(state["sync_cursor"]) if state["sync_cursor"] else None
    observed = sorted((r for r in runs if (cursor is None or run_order(r) > cursor) and
                       sync_context(gh, r, main_sha)), key=run_order)
    if not observed:
        return
    latest = observed[-1]
    # Startup observes only a current completed snapshot, not historical failures.
    candidates = ([latest] if latest["status"] == "completed" else []) if state["sync_cursor"] is None else observed
    for run in candidates:
        if run["status"] != "completed" or state["sync_cursor"] and run_order(run) <= tuple(state["sync_cursor"]):
            continue
        report = None
        if trusted_sync(gh, run, main_sha):
            try:
                report = dt.artifact_report(gh, run)
            except (dt.Error, ValueError):
                pass
        # Every completed gap/failure is consumed, even while a newer run is pending.
        # Only the latest completed current snapshot can announce success/hold recovery.
        reconcile_sync(state, run, report, mapping,
                       current_snapshot=run_order(run) == run_order(latest) and latest["status"] == "completed")


def workflows_config(raw, gh):
    data = dt.json_data(raw)
    if not isinstance(data, list) or len(data) > 20:
        raise dt.Error("Invalid CI workflow allowlist")
    result = []
    for item in data:
        if not isinstance(item, dict) or set(item) != {"id", "path"} or type(item["id"]) is not int or item["id"] <= 0:
            raise dt.Error("Invalid CI workflow configuration")
        if item["id"] == SYNC_ID or item["path"] in {SYNC_PATH, NOTIFIER_PATH}:
            raise dt.Error("Reserved workflow cannot be CI")
        actual = gh.repo(f"/actions/workflows/{item['id']}")
        if actual.get("id") != item["id"] or actual.get("path") != item["path"] or actual.get("state") != "active":
            raise dt.Error("CI workflow identity mismatch")
        result.append({**item, "name": actual["name"]})
    if len({x["id"] for x in result}) != len(result):
        raise dt.Error("Duplicate CI workflow")
    return result


def manual_guard(env):
    return (env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") == "refs/heads/main" and
            env.get("GITHUB_ACTOR_ID") == PM_ID and env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") and
            env.get("GITHUB_RUN_ATTEMPT") == "1" and env.get("DISCORD_APPROVED_SHA") == env.get("GITHUB_SHA") and
            bool(re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", ""))))


def diagnostic_reason(error):
    """Emit only fixed classifications, never exception text or response data."""
    if isinstance(error, dt.HTTPError):
        status = error.status
        return f"http_{status}" if type(status) is int and 100 <= status <= 599 else "http"
    fixed = {
        "Remote request outcome unknown": "request_unknown",
        "Response size limit exceeded": "response_limit",
        "Duplicate JSON key": "duplicate_json_key",
        "Incomplete paginated response": "pagination_count",
        "Source pagination incomplete": "pagination_incomplete",
        "Source pagination limit exceeded": "pagination_limit",
        "Main changed during observation": "main_changed",
    }
    return fixed.get(str(error), "unclassified") if isinstance(error, dt.Error) else "unclassified"


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".github/discord-notifications.json")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if env.get("DISCORD_NOTIFICATIONS_ENABLED") != "true":
        print("Discord 알림 비활성: 상태와 기준선을 변경하지 않습니다.")
        return 0
    stage = "configuration"
    try:
        config = dt.json_data(Path(args.config).read_bytes())
        if config != {"repository_id": REPO_ID, "sync_workflow_id": SYNC_ID, "ci_workflows": config.get("ci_workflows")}:
            raise dt.Error("Notification configuration mismatch")
        mapping = user_map(env.get("DISCORD_USER_MAP", "{}"))
        discord = dt.Discord(env.get("DISCORD_WEBHOOK_URL", ""), env.get("DISCORD_CHANNEL_ID", ""))
        stage = "destination_verify"
        discord.verify()  # Configuration/destination failures do not consume baseline/events.
        gh = dt.GitHub(env.get("GITHUB_TOKEN", ""))
        ledger = dt.Ledger(gh)
        workflows = workflows_config(json.dumps(config["ci_workflows"]), gh)
        stage = "ledger_load"
        state = ledger.load()
        bootstrap = state is None
        if bootstrap and not manual_guard(env):
            raise dt.Error("Initial baseline requires PM main dispatch")
        if env.get("DISCORD_RESOLVE_KEY"):
            if not manual_guard(env) or bootstrap:
                raise dt.Error("Delivery recovery requires PM main dispatch")
            resolve(state, env["DISCORD_RESOLVE_KEY"], env.get("DISCORD_RESOLVE_OUTCOME"),
                    env.get("DISCORD_RESOLVE_MESSAGE_ID"), discord)
            ledger.save(state)
        stage = "source_collect"
        objects, pulls, timelines = collect_objects(gh)
        backlog = project_backlog(env.get("DISCORD_PROJECT_READ_TOKEN"), objects)
        events = source_events(objects, timelines, mapping, backlog)
        if bootstrap:
            state = new_state()
            reconcile_events(state, events, bootstrap=True)
            stage = "baseline_save"
            ledger.save(state, bootstrap=True)
            # Durable activation boundary. A same-second unseen ID is still new.
            stage = "source_refresh"
            objects, pulls, timelines = collect_objects(gh)
            events = source_events(objects, timelines, mapping, backlog)
        before = copy.deepcopy(state)
        reconcile_events(state, events)
        main_sha = gh.repo("/git/ref/heads/main")["object"]["sha"]
        stage = "ci_observe"
        ci_runs = []
        for workflow in workflows:
            ci_runs.extend(gh.pages(f"/actions/workflows/{workflow['id']}/runs", "workflow_runs"))
        # List responses can race with a rerun. Re-read only each stream's latest candidate.
        candidates = {}
        for workflow in workflows:
            for pull in [None] + pulls:
                eligible = [r for r in ci_runs if ci_matches(r, pull, workflow, main_sha)]
                if eligible:
                    latest = max(eligible, key=run_order)
                    candidates[latest["id"]] = latest
        ci_runs = refresh_ci_runs(gh, ci_runs, candidates)
        refreshed = [gh.repo(f"/pulls/{p['number']}") for p in pulls]
        if main_sha != gh.repo("/git/ref/heads/main")["object"]["sha"]:
            raise dt.Error("Main changed during observation")
        reconcile_ci(state, ci_runs, refreshed, workflows, main_sha, mapping)
        stage = "sync_observe"
        sync_runs = gh.pages(f"/actions/workflows/{SYNC_ID}/runs", "workflow_runs")
        reconcile_sync_runs(state, gh, sync_runs, main_sha, mapping)
        if state != before:
            stage = "ledger_save"
            ledger.save(state)
        stage = "delivery"
        return 1 if deliver(state, ledger, discord,
                            event_check=lambda key: current_pr_event(gh, key, mapping),
                            ci_check=lambda key: current_ci_notice(gh, key, workflows)) else 0
    except Exception as error:
        # Fixed message only: no exception repr, API response, token or webhook URL.
        print("::error::Discord 알림 처리 실패: 설정·실행 기록·전송 보류 상태를 확인하세요. "
              + f"stage={stage}; reason={diagnostic_reason(error)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
