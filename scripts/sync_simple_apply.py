"""Verified, ordered per-item effects; no rollback checkpoint is ever overwritten."""
import hashlib

import github_project as gp
import notion_api as na
from sync_simple_metadata import differences, minute, text
from sync_simple_state import CHECKPOINT, checkpoint_text, key_for


class Changed(gp.SyncError):
    """A fresh user edit invalidated this scan; re-evaluate on the next run."""


def checked_page(notion, config, page, *, compare=True):
    fresh = notion.request("GET", f"/pages/{na.identifier(page['id'])}")
    na.bound_page(fresh, config["notion_source_id"])
    gp.require(na.identifier(fresh["id"]) == na.identifier(page["id"]) and
               not any(fresh.get(name) for name in ("archived", "in_trash", "is_archived")),
               "Notion readback identity invalid")
    for name in ("동기화 키", "종류", "번호"):
        expected = {name: page["properties"][name]}
        # Read objects contain id/type fields; compare the actual property payload.
        kind = {"동기화 키": "rich_text", "종류": "select", "번호": "number"}[name]
        gp.require(not differences(fresh, {name: {kind: expected[name][kind]}}),
                   "Notion canonical identity changed")
    if compare and (na.read_select(fresh, "작업 상태") != na.read_select(page, "작업 상태") or
                    na.read_text(fresh, CHECKPOINT) != na.read_text(page, CHECKPOINT)):
        raise Changed("Notion status or checkpoint changed")
    return fresh


def patch_page(notion, config, page, properties):
    before = checked_page(notion, config, page)
    changes = differences(before, properties)
    if not changes:
        return before
    notion.request("PATCH", f"/pages/{na.identifier(page['id'])}", {"properties": changes})
    confirmed = checked_page(notion, config, before, compare=False)
    gp.require(not differences(confirmed, changes), "Notion write readback mismatch")
    # Metadata-only writes must not silently consume an intervening status edit.
    for name, kind in (("작업 상태", "select"), (CHECKPOINT, "rich_text")):
        expected = changes.get(name, {kind: before["properties"][name][kind]})
        if differences(confirmed, {name: expected}):
            raise Changed("Notion changed during write")
    return confirmed


def project_item(client, config, item_id, issue):
    item = gp.fetch_project_item(client, config["project_id"], item_id,
                                config["status_field_id"], status_options=config["status_options"])
    gp.require(item["content_id"] == issue["id"], "Project Issue readback mismatch")
    return item


def project_snapshot(client, config, facts, *, refresh=False):
    result = gp.fetch_project(client, project_id=config["project_id"], owner_id=config["owner_id"],
                             status_field_id=config["status_field_id"],
                             status_options=config["status_options"],
                             repository_node_id=facts["repository_node_id"])
    gp.require((set(result["items"]) | set(result["archived_items"])).issubset(facts["issues"]),
               "Project source missing")
    for issue_id, item in result["items"].items():
        gp.require(item["content_id"] == facts["issues"][issue_id]["id"],
                   "Project Issue ID mismatch")
        if refresh:
            result["items"][issue_id] = project_item(client, config, item["id"], facts["issues"][issue_id])
    return result


def create_page(notion, config, metadata, status, now):
    properties = {**metadata, "동기화 시각": {"date": {"start": minute(now)}}}
    if metadata["종류"]["select"]["name"] == "Issue":
        properties.update({"작업 상태": {"select": {"name": status}},
                           CHECKPOINT: text("")})
        properties.setdefault("확인 필요", text(""))
    created = notion.request("POST", "/pages", {
        "parent": {"data_source_id": config["notion_source_id"]}, "properties": properties})
    page_id = na.identifier(created.get("id"))
    confirmed = notion.request("GET", f"/pages/{page_id}")
    na.bound_page(confirmed, config["notion_source_id"])
    gp.require(na.identifier(confirmed["id"]) == page_id and
               not any(confirmed.get(name) for name in ("archived", "in_trash", "is_archived")) and
               not differences(confirmed, properties), "Notion create readback mismatch")
    return confirmed


def apply_issue(notion, client, config, work, now):
    """Project -> Notion display -> equality readbacks -> durable checkpoint."""
    issue, page, item = work.source, work.page, work.item
    plan, properties = work.plan, work.properties
    if plan["archived_project"]:
        # Archive protects Project membership/status, not GitHub-owned metadata.
        return apply_metadata(notion, config, work, now), item, None
    target = plan["target"]
    if page:
        page = checked_page(notion, config, page)
    if item:
        current = project_item(client, config, item["id"], issue)
        if current["status_option_id"] != item["status_option_id"]:
            raise Changed("Project status changed")
        item = current
    if target is not None and plan["note"] != "초기 정렬":
        if item is None:
            item_id = gp.add_project_issue(client, config["project_id"], issue["id"])
            item = project_item(client, config, item_id, issue)
        option = config["status_options"][target]
        if item["status_option_id"] != option and plan["write_project"]:
            gp.set_project_status(client, config["project_id"], item["id"],
                                  config["status_field_id"], option)
            item = project_item(client, config, item["id"], issue)
            gp.require(item["status_option_id"] == option, "Project status readback mismatch")
        if plan["write_notion"]:
            properties = {**properties, "작업 상태": {"select": {"name": target}}}
    properties = {**properties, "확인 필요": text(
        "확인 필요" if plan["note"] else "")}
    if page is None:
        gp.require(target is not None, "Missing Notion row has no initial status")
        page = create_page(notion, config, properties, target, now)
    else:
        changes = differences(page, properties)
        if changes:
            page = patch_page(notion, config, page,
                              {**changes, "동기화 시각": {"date": {"start": minute(now)}}})
    if target is None or plan["note"] == "초기 정렬":
        return page, item, None
    # Reject only the side moved to review. An independent valid edit on the
    # other side is preserved and reconciled by the next scan, not discarded.
    if ((not plan["write_project"] and item["status_option_id"] != config["status_options"][target]) or
            (not plan["write_notion"] and na.read_select(page, "작업 상태") != target)):
        return page, item, None
    raw = checkpoint_text(target, work.facts)
    if na.read_text(page, CHECKPOINT) != raw:
        # The initial fresh pair already verifies an unchanged checkpoint. Only a
        # new checkpoint needs another equality check after possible display writes.
        item = project_item(client, config, item["id"], issue)
        gp.require(item["status_option_id"] == config["status_options"][target],
                   "Project changed before checkpoint")
        page = checked_page(notion, config, page)
        gp.require(na.read_select(page, "작업 상태") == target, "Notion changed before checkpoint")
        page = patch_page(notion, config, page, {CHECKPOINT: text(raw)})
    # Only confirmed engine handling produces relay echo evidence.
    entry = {"page_id": na.identifier(page["id"]), "key": key_for(issue["databaseId"]),
             "repository_id": gp.REPOSITORY_ID, "project_id": config["project_id"],
             "item_id": item["id"], "status": target,
             "state_sha256": hashlib.sha256(raw.encode()).hexdigest(),
             "outcome": "handled", "kind": "echo_observation", "reflection": True}
    return page, item, entry


def apply_metadata(notion, config, work, now):
    page, properties = work.page, work.properties
    if page is None:
        return create_page(notion, config, properties, None, now)
    if differences(page, properties):
        return patch_page(notion, config, page,
                          {**properties, "동기화 시각": {"date": {"start": minute(now)}}})
    return page
