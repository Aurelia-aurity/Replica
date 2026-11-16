"""GitHub-owned Notion properties, preserving the legacy display contract."""
from datetime import timezone

import github_project as gp


def text(value, kind="rich_text"):
    gp.require(isinstance(value, str) and len(value) <= 20000, "Notion text invalid")
    return {kind: [{"type": "text", "text": {"content": value[i:i + 2000]}}
                   for i in range(0, len(value), 2000)]}


def minute(value):
    return gp.parse_time(value).astimezone(timezone.utc).replace(
        second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def metadata(kind, row, key):
    number = row["number"]
    gp.require(type(number) is int and number > 0, "GitHub number invalid")
    assignees = sorted({value["login"] for value in row["assignees"]["nodes"]})
    labels = sorted({value["name"] for value in row["labels"]["nodes"]})
    gp.require(all(isinstance(value, str) for value in assignees + labels),
               "GitHub metadata invalid")
    title = f"#{number} {row['title']}" if kind == "Issue" else row["title"]
    gp.require(isinstance(title, str) and title.strip(), "GitHub title missing")
    path = "issues" if kind == "Issue" else "pull"
    gp.require(row["url"] == f"https://github.com/{gp.REPOSITORY}/{path}/{number}",
               "GitHub URL mismatch")
    status = "Open" if row["state"] == "OPEN" else "Closed"
    if kind == "PR":
        status = ("Merged" if row["state"] == "MERGED" or row.get("mergedAt") else
                  "Draft" if row["state"] == "OPEN" and row["isDraft"] else status)
    return {
        "제목": text(title, "title"), "종류": {"select": {"name": kind}},
        "번호": {"number": number}, "GitHub URL": {"url": row["url"]},
        "GitHub 상태": {"select": {"name": status}},
        "작성자": text((row.get("author") or {}).get("login") or ""),
        "담당자": text(", ".join(assignees)), "라벨": text(", ".join(labels)),
        "GitHub 수정": {"date": {"start": minute(row["updatedAt"])}},
        "동기화 키": text(key),
    }


def closure(issue):
    reason = ({"COMPLETED": "완료", "NOT_PLANNED": "미계획", "DUPLICATE": "중복"}.get(
        issue.get("stateReason"), "확인 필요") if issue["state"] == "CLOSED" else None)
    duplicate = issue.get("duplicateOf") if reason == "중복" else None
    representative = duplicate.get("url") if duplicate else None
    gp.require(representative is None or
               isinstance(representative, str) and representative.startswith("https://github.com/"),
               "GitHub duplicate URL invalid")
    return {"종료 사유": {"select": {"name": reason} if reason else None},
            "대표 이슈": {"url": representative}}


def property_value(kind, value):
    if kind in {"title", "rich_text"}:
        return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                       for part in value)
    if kind == "select":
        return value.get("name") if value else None
    if kind == "date":
        if value is None:
            return None
        gp.require(value.get("end") is None and value.get("time_zone") is None,
                   "Notion display date range/timezone invalid")
        return gp.parse_time(value["start"])
    return value


def differences(page, properties):
    result = {}
    for name, expected in properties.items():
        kind, body = next(iter(expected.items()))
        actual = page["properties"].get(name)
        gp.require(isinstance(actual, dict) and kind in actual, "Notion property missing")
        if property_value(kind, actual[kind]) != property_value(kind, body):
            result[name] = expected
    return result
