"""Notion writer for the n8n ops routines (Notion REST API, no SDK).

Env: NOTION_TOKEN (internal integration, shared with both databases),
     NOTION_ERRORS_DB (the "n8n Error Runs" database id),
     NOTION_AUDIT_PARENT (page id the audit reports are created under),
     NOTION_ASSIGNEE_ID (Notion user id to assign + @mention),
     NOTION_TASKS_DB (optional: "Actions Business"; each NEW diagnosis also gets a short
       "New n8n error" task there that links the Error Runs page. Existing fields only:
       title, Do Date, Due Date, Owner, Priority. Repeats (bump) create no new task.)

CLI:
  python3 notion.py ensure-schema                 # add missing properties (idempotent)
  python3 notion.py find-user <email>             # look up NOTION_ASSIGNEE_ID once
  python3 notion.py find-open <fingerprint>       # -> page id or ""
  python3 notion.py bump <page_id> <run_url>      # occurrences +1, last seen, add run link
  python3 notion.py create-task <diagnosis.json>  # -> page url
  python3 notion.py create-audit <audit.md> <title>
"""
import json, os, sys, subprocess, datetime

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

# Properties this system relies on. Existing ones in "n8n Error Runs" are kept.
SCHEMA = {
    "Fingerprint": {"rich_text": {}},
    "Node": {"rich_text": {}},
    "Workflow Name": {"rich_text": {}},
    "n8n Run URL": {"url": {}},
    "Error Message": {"rich_text": {}},
    "Executed At": {"date": {}},
    "Last Seen": {"date": {}},
    "Occurrences": {"number": {}},
    "Class": {"select": {"options": [{"name": n} for n in (
        "transient-upstream", "rate-limit", "credential", "bad-input-data",
        "branch-wiring", "empty-lookup", "ai-output", "config", "unknown")]}},
    "Severity": {"select": {"options": [{"name": n} for n in ("high", "medium", "low")]}},
    "Confidence": {"select": {"options": [{"name": n} for n in ("high", "medium", "low")]}},
    "Diagnosis Status": {"select": {"options": [{"name": n} for n in (
        "Open", "Needs human", "Likely resolved – verify", "Fixed", "Wrong diagnosis")]}},
    "Assignee": {"people": {}},
}


def call(method, path, body=None):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("Set NOTION_TOKEN")
    cmd = ["curl", "-s", "-X", method, API + path,
           "-H", f"Authorization: Bearer {token}",
           "-H", f"Notion-Version: {VERSION}", "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    r = subprocess.run(cmd, input=json.dumps(body).encode() if body is not None else None,
                       capture_output=True, timeout=60)
    out = json.loads(r.stdout or b"{}")
    if out.get("object") == "error":
        raise RuntimeError(f"Notion {method} {path}: {out.get('code')} {out.get('message')}")
    return out


def db_id():
    return os.environ["NOTION_ERRORS_DB"]


def text(s, limit=1900):
    """Notion caps a rich_text chunk at 2000 chars."""
    s = "" if s is None else str(s)
    return [{"type": "text", "text": {"content": s[i:i + limit]}} for i in range(0, min(len(s), limit * 3), limit)] or []


def title_property():
    props = call("GET", f"/databases/{db_id()}")["properties"]
    return next(k for k, v in props.items() if v["type"] == "title"), props


def ensure_schema():
    _, props = title_property()
    missing = {k: v for k, v in SCHEMA.items() if k not in props}
    if missing:
        call("PATCH", f"/databases/{db_id()}", {"properties": missing})
    print("added:", ", ".join(missing) or "nothing (schema ok)")


def find_user(email):
    cursor = None
    while True:
        page = call("GET", "/users?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        for u in page["results"]:
            if (u.get("person") or {}).get("email", "").lower() == email.lower():
                print(u["id"])
                return
        if not page.get("has_more"):
            break
        cursor = page["next_cursor"]
    raise SystemExit("user not found (integration needs 'Read user information including email')")


def find_open(fp):
    q = {"filter": {"and": [
        {"property": "Fingerprint", "rich_text": {"equals": fp}},
        {"property": "Diagnosis Status", "select": {"does_not_equal": "Fixed"}},
        {"property": "Diagnosis Status", "select": {"does_not_equal": "Wrong diagnosis"}},
    ]}, "page_size": 1}
    res = call("POST", f"/databases/{db_id()}/query", q)["results"]
    print(res[0]["id"] if res else "")


def bump(page_id, run_url):
    page = call("GET", f"/pages/{page_id}")
    n = (page["properties"].get("Occurrences", {}).get("number") or 1) + 1
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    call("PATCH", f"/pages/{page_id}", {"properties": {
        "Occurrences": {"number": n}, "Last Seen": {"date": {"start": now}}}})
    call("PATCH", f"/blocks/{page_id}/children", {"children": [
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [
            {"type": "text", "text": {"content": f"Again at {now[:16]} UTC: ", }},
            {"type": "text", "text": {"content": run_url, "link": {"url": run_url}}}]}}]})
    print(f"bumped to {n}")


def heading(t):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": text(t)}}


def para(t):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": text(t)}}


def bullets(items):
    return [{"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": text(i)}} for i in items]


def paste_ready(node):
    """The corrected node as n8n clipboard JSON: copy it, click the canvas, Cmd+V,
    reconnect, delete the old node. No write access to n8n needed."""
    if not node:
        return []
    clip = json.dumps({"nodes": [node], "connections": {}}, indent=1, ensure_ascii=False)
    chunks = [clip[i:i + 1900] for i in range(0, len(clip), 1900)][:20]
    return [heading("Paste-ready fixed node"),
            para("Copy the block, click empty canvas in the workflow, paste (Cmd+V), reconnect it in place of the old node."),
            {"object": "block", "type": "code", "code": {"language": "json",
             "rich_text": [{"type": "text", "text": {"content": c}} for c in chunks]}}]


def create_task(path):
    d = json.load(open(path))
    title_key, _ = title_property()
    assignee = os.environ.get("NOTION_ASSIGNEE_ID")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    props = {
        title_key: {"title": text(f"[{d['class']}] {d['workflow_name']} → {d['node']}: {d['title']}", 200)},
        "Fingerprint": {"rich_text": text(d["fingerprint"])},
        "Node": {"rich_text": text(d["node"])},
        "Workflow Name": {"rich_text": text(d["workflow_name"])},
        "n8n Run URL": {"url": d.get("run_url") or None},
        "Error Message": {"rich_text": text(d.get("error_message"))},
        "Executed At": {"date": {"start": d.get("failed_at") or now}},
        "Last Seen": {"date": {"start": now}},
        "Occurrences": {"number": int(d.get("occurrences") or 1)},
        "Class": {"select": {"name": d["class"]}},
        "Severity": {"select": {"name": d["severity"]}},
        "Confidence": {"select": {"name": d["confidence"]}},
        "Diagnosis Status": {"select": {"name": d["status"]}},
    }
    if assignee:
        props["Assignee"] = {"people": [{"object": "user", "id": assignee}]}
    intro = [{"type": "mention", "mention": {"user": {"id": assignee}}}] if assignee else []
    intro += [{"type": "text", "text": {"content": f" {d['workflow_name']} failed at node \"{d['node']}\". "}}]
    if d.get("run_url"):
        intro += [{"type": "text", "text": {"content": "Open the failed run", "link": {"url": d["run_url"]}}}]
    children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": intro}},
                heading("Error"), para(d.get("error_message")),
                heading("Root cause"), para(d["root_cause"]),
                heading("Evidence"), *bullets(d.get("evidence", [])),
                heading("Recommended fix"), *bullets(d.get("fix_steps", [])),
                *paste_ready(d.get("patched_node")),
                heading("Prevention"), para(d.get("prevention", "")),
                para(f"Confidence: {d['confidence']}. {d.get('assumption') or ''}".strip())]
    page = call("POST", "/pages", {"parent": {"database_id": db_id()}, "properties": props,
                                   "children": children})
    print(page["url"])
    if os.environ.get("NOTION_TASKS_DB"):
        print(create_review_task(d, page))


# Actions Business uses these two priority options today; high severity gets the urgent one.
PRIORITY = {"high": "🔥❗️", "medium": "1st Priority 🚀", "low": "1st Priority 🚀"}


def create_review_task(d, error_page):
    """Short task in Actions Business so the owner gets a Notion notification.
    Writes only properties that already exist there; never changes its schema."""
    tasks_db = os.environ["NOTION_TASKS_DB"]
    owner = os.environ.get("NOTION_ASSIGNEE_ID")
    props_schema = call("GET", f"/databases/{tasks_db}")["properties"]
    title_key = next(k for k, v in props_schema.items() if v["type"] == "title")
    today = datetime.date.today().isoformat()
    props = {title_key: {"title": text(f"New n8n error: {d['workflow_name']} → {d['node']}", 200)}}
    wanted = {"Do Date": {"date": {"start": today}}, "Due Date": {"date": {"start": today}},
              "Priority": {"select": {"name": PRIORITY.get(d["severity"], "1st Priority 🚀")}}}
    if owner:
        wanted["Owner"] = {"people": [{"object": "user", "id": owner}]}
    props.update({k: v for k, v in wanted.items() if k in props_schema})
    line = [{"type": "mention", "mention": {"user": {"id": owner}}}] if owner else []
    line += [{"type": "text", "text": {"content": " review the diagnosis: "}},
             {"type": "mention", "mention": {"page": {"id": error_page["id"]}}}]
    task = call("POST", "/pages", {"parent": {"database_id": tasks_db}, "properties": props, "children": [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": line}},
        para(f"{d['title']} (severity {d['severity']}, confidence {d['confidence']}, status {d['status']})")]})
    return task["url"]


def create_audit(md_path, title):
    """One page per audit; each markdown line becomes a block (headings and bullets kept)."""
    blocks = []
    for line in open(md_path, encoding="utf-8").read().splitlines():
        s = line.rstrip()
        if not s:
            continue
        if s.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": text(s[3:])}})
        elif s.startswith("### "):
            blocks.append(heading(s[4:]))
        elif s.startswith(("- ", "* ")):
            blocks += bullets([s[2:]])
        else:
            blocks.append(para(s))
    assignee = os.environ.get("NOTION_ASSIGNEE_ID")
    if assignee:
        blocks.insert(0, {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "mention", "mention": {"user": {"id": assignee}}},
            {"type": "text", "text": {"content": " new n8n audit is ready."}}]}})
    page = call("POST", "/pages", {
        "parent": {"page_id": os.environ["NOTION_AUDIT_PARENT"]},
        "properties": {"title": {"title": text(title, 200)}},
        "children": blocks[:100]})  # Notion accepts 100 blocks per request
    for i in range(100, len(blocks), 100):
        call("PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i:i + 100]})
    print(page["url"])


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"ensure-schema": ensure_schema, "find-user": find_user, "find-open": find_open,
     "bump": bump, "create-task": create_task, "create-audit": create_audit}[cmd](*args)
