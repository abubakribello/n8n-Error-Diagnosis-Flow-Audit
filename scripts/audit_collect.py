"""Collect every fact the bi-weekly audit needs into one JSON file. Read-only.

Usage: python3 audit_collect.py --days 14 --out audit_data.json [--project <projectId>]
The routine (Claude) turns this JSON into recommendations; every number in the
report must come from this file.
"""
import argparse, collections, datetime, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from n8n_api import get, paged, fingerprint  # noqa: E402

SECRET = re.compile(r"(sk-[\w-]{20,}|(?:Bearer|Token|Basic)\s+[\w.=+/-]{16,}|AIza[\w-]{30,}|xox[bp]-[\w-]{10,}|eyJ[\w-]{20,}\.[\w-]{20,})")
MODEL_KEYS = ("model", "modelId", "modelName")
EXTERNAL = ("httpRequest", "openAi", "lmChat", "anthropic", "gemini", "airtable", "googleSheets",
            "gmail", "notion", "slack", "telegram", "highLevel", "googleDrive")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def models_in(node):
    """Model ids from node parameters, incl. resource-locator objects ({value: ...})."""
    found = []
    def walk(o, key=None):
        if isinstance(o, dict):
            if key in MODEL_KEYS and isinstance(o.get("value"), str):
                found.append(o["value"])
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and key in MODEL_KEYS and not o.startswith("="):
            found.append(o)
    walk(node.get("parameters", {}))
    return found


def static_checks(w):
    nodes = w["nodes"]
    conn = w.get("connections", {})
    targets = {c["node"] for outs in conn.values() for arr in outs.values() for a in (arr or []) for c in (a or [])}
    real = [n for n in nodes if "stickyNote" not in n["type"]]
    ext = [n for n in real if any(e in n["type"] for e in EXTERNAL)]
    return {
        "node_count": len(real),
        "has_error_handling": bool((w.get("settings") or {}).get("errorWorkflow")) or any("errorTrigger" in n["type"] for n in nodes),
        "error_workflow_setting": (w.get("settings") or {}).get("errorWorkflow"),
        "hardcoded_secret_nodes": [n["name"] for n in real if SECRET.search(json.dumps(n.get("parameters", {})))],
        "open_webhooks": [n["name"] for n in real if n["type"].endswith(".webhook") and not n.get("disabled")
                          and (n["parameters"].get("authentication") or "none") == "none"],
        "retry_off": [n["name"] for n in ext if not n.get("retryOnFail")],
        "external_nodes": len(ext),
        "swallow_errors": [n["name"] for n in real if n.get("onError") == "continueRegularOutput" or n.get("continueOnFail")],
        "disabled": [n["name"] for n in real if n.get("disabled")],
        "unconnected": [n["name"] for n in real if n["name"] not in targets and n["name"] not in conn
                        and not any(t in n["type"].lower() for t in ("trigger", "webhook"))],
        "pinned": list((w.get("pinData") or {}).keys()),
        "default_names": [n["name"] for n in real if re.fullmatch(
            r"(HTTP Request|Code|Code in JavaScript|Edit Fields|If|Switch|Merge|Set|Wait|Airtable|Google Sheets|AI Agent)\d*", n["name"])],
        "models": sorted({m for n in real for m in models_in(n)}),
        "node_versions": sorted({f"{n['type'].split('.')[-1]}@{n.get('typeVersion')}" for n in real}),
        "calls_workflows": sorted({str((n["parameters"].get("workflowId") or {}).get("value") if isinstance(n["parameters"].get("workflowId"), dict) else n["parameters"].get("workflowId"))
                                   for n in real if n["type"].endswith("executeWorkflow")}),
    }


def runs_since(workflow_id, status, since, legacy=None):
    """Count runs (no data) newest-first until older than `since`.
    `legacy` (a list) collects runs in mode "error": the workflow's own old Error Trigger branch."""
    count, last = 0, None
    for e in paged(f"/executions?workflowId={workflow_id}&status={status}", limit=100, max_pages=20):
        started = e.get("startedAt") or e.get("createdAt") or ""
        if started and started < since:
            break
        if e.get("mode") == "error" and legacy is not None:
            legacy.append(e["id"])
            continue
        count += 1
        last = last or started
    return count, last


def errors_with_data(workflow_id, since, cap=40):
    out = []
    for e in paged(f"/executions?workflowId={workflow_id}&status=error&includeData=true", limit=10, max_pages=cap // 10):
        started = e.get("startedAt") or e.get("createdAt") or ""
        if started and started < since:
            break
        rd = ((e.get("data") or {}).get("resultData") or {})
        err = rd.get("error") or {}
        node = rd.get("lastNodeExecuted") or (err.get("node") or {}).get("name")
        out.append({"id": e["id"], "at": started, "node": node, "message": (err.get("message") or "")[:200],
                    "description": str(err.get("description") or "")[:200],
                    "fingerprint": fingerprint(workflow_id, node, err.get("message"))})
    return out


def silent_failures(workflow_id, nodes, since, max_runs=30):
    """Successful runs where a 'continue on error' node emitted an item containing `error`."""
    hits = collections.Counter()
    seen = 0
    for e in paged(f"/executions?workflowId={workflow_id}&status=success&includeData=true", limit=5, max_pages=max_runs // 5):
        if (e.get("startedAt") or "") < since:
            break
        seen += 1
        run = ((e.get("data") or {}).get("resultData") or {}).get("runData") or {}
        for name in set(nodes) & set(run):
            for r in run[name]:
                if r.get("error"):
                    hits[name] += 1
                for branch in ((r.get("data") or {}).get("main") or []):
                    for item in branch or []:
                        if (item.get("json") or {}).get("error"):
                            hits[name] += 1
    return {"runs_scanned": seen, "errors_by_node": dict(hits)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--out", default="audit_data.json")
    ap.add_argument("--project")
    a = ap.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    since = iso(now - datetime.timedelta(days=a.days))
    q = "/workflows?active=true" + (f"&projectId={a.project}" if a.project else "")
    workflows = list(paged(q, limit=100))
    called = set()
    report = {"generated_at": iso(now) + "Z", "window_days": a.days, "since": since + "Z", "workflows": []}
    for w in workflows:
        s = static_checks(w)
        called |= set(s["calls_workflows"])
        legacy = []
        ok, last_ok = runs_since(w["id"], "success", since, legacy)
        err, last_err = runs_since(w["id"], "error", since, legacy)
        entry = {"id": w["id"], "name": w["name"], "updatedAt": w.get("updatedAt"),
                 "runs_success": ok, "runs_error": err, "last_success": last_ok, "last_error": last_err,
                 "error_rate": round(err / (ok + err), 3) if ok + err else None,
                 "legacy_error_branch_runs": len(legacy), **s}
        if err:
            entry["errors"] = errors_with_data(w["id"], since)
            groups = collections.defaultdict(list)
            for e in entry["errors"]:
                groups[e["fingerprint"]].append(e)
            entry["issues"] = [{"fingerprint": fp, "count": len(v), "node": v[0]["node"], "message": v[0]["message"],
                                "description": v[0]["description"], "last": v[0]["at"]} for fp, v in groups.items()]
        if s["swallow_errors"] and ok:
            entry["silent_failures"] = silent_failures(w["id"], s["swallow_errors"], since)
        report["workflows"].append(entry)
        print(f"  {w['name'][:40]:40} ok={ok:<4} err={err:<3}", file=sys.stderr)
    for e in report["workflows"]:
        e["called_by_other_workflow"] = e["id"] in called
        e["unused"] = e["runs_success"] + e["runs_error"] == 0 and not e["called_by_other_workflow"]
    totals = {
        "published": len(report["workflows"]),
        "unused": sum(e["unused"] for e in report["workflows"]),
        "runs_success": sum(e["runs_success"] for e in report["workflows"]),
        "runs_error": sum(e["runs_error"] for e in report["workflows"]),
        "unique_issues": sum(len(e.get("issues", [])) for e in report["workflows"]),
        "hardcoded_secret_nodes": sum(len(e["hardcoded_secret_nodes"]) for e in report["workflows"]),
        "open_webhooks": sum(len(e["open_webhooks"]) for e in report["workflows"]),
        "no_error_handling": sum(not e["has_error_handling"] for e in report["workflows"]),
        "legacy_error_branch_runs": sum(e["legacy_error_branch_runs"] for e in report["workflows"]),
        "silent_failure_items": sum(sum(e.get("silent_failures", {}).get("errors_by_node", {}).values()) for e in report["workflows"]),
        "models_in_use": dict(collections.Counter(m for e in report["workflows"] for m in e["models"])),
    }
    report["totals"] = totals
    json.dump(report, open(a.out, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(totals, indent=1))


if __name__ == "__main__":
    main()
