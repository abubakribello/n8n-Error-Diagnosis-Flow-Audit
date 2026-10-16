"""Build a compact, redacted diagnosis context for one failed n8n execution.

Usage: N8N_URL=... N8N_API_KEY=... python3 -I build_context.py <executionId>
Prints JSON: fingerprint, workflow, failing node (config + input sample),
error, path taken, and the last successful run of the same node for comparison.
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from n8n_api import get, fingerprint, BASE  # noqa: E402

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w-])(?:\+|00)\d[\d\s/()-]{6,}\d(?![\w-])|(?<![\w:.-])0\d{2,4}[\s/-]?\d{5,9}(?![\w:.-])")
SECRET = re.compile(r"(sk-[\w-]{10,}|(?:Bearer|Token|Basic)\s+[\w.=+/-]{16,}|eyJ[\w-]{10,}\.[\w-]{10,}(\.[\w-]+)?)")

SECRET_KEYS = re.compile(r"(authorization|api[_-]?key|token|secret|password)", re.I)

def mask_secret_fields(o):
    if isinstance(o, dict):
        if isinstance(o.get("name"), str) and SECRET_KEYS.search(o["name"]) and isinstance(o.get("value"), str) and not o["value"].startswith("="):
            o = {**o, "value": "<SECRET>"}
        return {k: ("<SECRET>" if SECRET_KEYS.search(k) and isinstance(v, str) and not v.startswith("=") else mask_secret_fields(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [mask_secret_fields(x) for x in o]
    return o

def redact(obj):
    """Mask secrets, e-mails and phone numbers; keep shape so the model sees structure."""
    s = json.dumps(mask_secret_fields(obj), ensure_ascii=False)
    s = SECRET.sub("<SECRET>", s)
    s = EMAIL.sub("<EMAIL>", s)
    s = PHONE.sub("<PHONE>", s)
    return json.loads(s)

def trim(obj, depth=0, max_str=300, max_items=3):
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + f"…(+{len(obj)-max_str} chars)"
    if isinstance(obj, list):
        out = [trim(x, depth + 1, max_str, max_items) for x in obj[:max_items]]
        return out + [f"…(+{len(obj)-max_items} items)"] if len(obj) > max_items else out
    if isinstance(obj, dict):
        if depth > 6:
            return "{…}"
        return {k: trim(v, depth + 1, max_str, max_items) for k, v in obj.items()}
    return obj

def node_input(run_data, wf, node_name):
    """Items the failing node received = output of its parent(s) in this run."""
    parents = [src for src, outs in wf["connections"].items()
               for arr in outs.get("main", []) for c in (arr or []) if c["node"] == node_name]
    sample = {}
    for p in parents:
        runs = run_data.get(p) or []
        if runs:
            items = ((runs[-1].get("data") or {}).get("main") or [[]])
            flat = [it.get("json") for branch in items for it in (branch or [])]
            if flat:
                sample[p] = {"item_count": len(flat), "first_items": flat[:2]}
    return sample

REF = re.compile(r"\$\(['\"]([^'\"]+)['\"]\)|\$node\[['\"]([^'\"]+)['\"]\]")

def referenced_nodes(run_data, node):
    """Output in THIS run of every node the failing node's expressions point at.
    'not executed' is itself the most common root cause (wrong branch taken)."""
    out = {}
    for a, b in REF.findall(json.dumps(node.get("parameters", {}))):
        name = a or b
        runs = run_data.get(name)
        if not runs:
            out[name] = "NOT EXECUTED in this run"
            continue
        items = ((runs[-1].get("data") or {}).get("main") or [[]])
        flat = [it.get("json") for br in items for it in (br or [])]
        out[name] = {"item_count": len(flat), "first_item": flat[0] if flat else None}
    return out

def old_error_branch(wf, ex):
    """Did the workflow's own (legacy) Error Trigger branch also run for this failure?
    n8n runs it as an execution of the same workflow in mode "error". With the relay set
    as error workflow it should stay silent; if it ran, the user gets duplicate alerts."""
    has_trigger = any("errorTrigger" in n["type"] for n in wf["nodes"])
    if not has_trigger:
        return {"has_legacy_error_trigger": False, "fired": False}
    started = ex.get("startedAt") or ""
    recent = get(f"/executions?workflowId={wf['id']}&limit=25").get("data", [])
    import datetime
    t0 = datetime.datetime.fromisoformat(started.replace("Z", "+00:00")) if started else None
    def within(e, minutes=5):  # the legacy branch runs right after the failure, not later
        if not t0 or not e.get("startedAt"):
            return False
        t = datetime.datetime.fromisoformat(e["startedAt"].replace("Z", "+00:00"))
        return datetime.timedelta(0) <= t - t0 <= datetime.timedelta(minutes=minutes)
    runs = [e["id"] for e in recent if e.get("mode") == "error" and within(e)]
    return {"has_legacy_error_trigger": True, "fired": bool(runs), "executions": runs,
            "error_workflow_setting": (wf.get("settings") or {}).get("errorWorkflow")}


def main(exec_id):
    ex = get(f"/executions/{exec_id}?includeData=true")
    wf = get(f"/workflows/{ex['workflowId']}")
    rd = (ex.get("data") or {}).get("resultData") or {}
    err = rd.get("error") or {}
    node_name = rd.get("lastNodeExecuted") or (err.get("node") or {}).get("name")
    node = next((n for n in wf["nodes"] if n["name"] == node_name), {})
    run_data = rd.get("runData") or {}

    # Last successful execution of the same workflow where this node ran without error.
    last_ok = None
    ok = get(f"/executions?workflowId={wf['id']}&status=success&includeData=true&limit=5").get("data", [])
    for e in ok:
        runs = (((e.get("data") or {}).get("resultData") or {}).get("runData") or {}).get(node_name)
        if runs and not runs[-1].get("error"):
            last_ok = {"executionId": e["id"], "startedAt": e["startedAt"],
                       "input": node_input(e["data"]["resultData"]["runData"], wf, node_name)}
            break

    ctx = {
        "fingerprint": fingerprint(wf["id"], node_name, err.get("message")),
        "execution": {"id": ex["id"], "startedAt": ex["startedAt"], "mode": ex["mode"],
                      "url": f"{BASE[:-7]}/workflow/{wf['id']}/executions/{ex['id']}"},
        "workflow": {"id": wf["id"], "name": wf["name"], "updatedAt": wf.get("updatedAt"), "node_count": len(wf["nodes"]),
                     "path_taken": list(run_data.keys())},
        "referenced_nodes_this_run": None,
        "error": {"message": err.get("message"), "description": err.get("description"),
                  "httpCode": err.get("httpCode"), "node": node_name,
                  "stack_head": (err.get("stack") or "").split("\n")[:3]},
        "failing_node": {"type": node.get("type"), "typeVersion": node.get("typeVersion"),
                         "parameters": node.get("parameters"),
                         "settings": {k: node.get(k) for k in ("retryOnFail", "maxTries", "onError", "alwaysOutputData")},
                         "credential_types": list((node.get("credentials") or {}).keys())},
        "failing_node_input": node_input(run_data, wf, node_name),
        "last_successful_run_of_node": last_ok,
    }
    ctx["referenced_nodes_this_run"] = referenced_nodes(run_data, node)
    ctx["old_error_branch"] = old_error_branch(wf, ex)
    path = ctx["workflow"].pop("path_taken")
    ctx = trim(redact(ctx))
    ctx["workflow"]["path_taken"] = path  # keep full path untrimmed
    print(json.dumps(ctx, indent=1, ensure_ascii=False))

if __name__ == "__main__":
    main(sys.argv[1])
