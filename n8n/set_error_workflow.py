"""Point every published workflow's "Error workflow" setting at the relay, in one go.

Needs a TEMPORARY n8n API key with workflow write access (delete it afterwards).
Dry run by default; --apply to write. Backs up every workflow JSON first.

  N8N_URL=... N8N_API_KEY=<write key> python3 set_error_workflow.py <relayWorkflowId> [--project <id>] [--only id1,id2] [--apply]

Why the old in-workflow Error Trigger branches can stay for now: when a workflow has an
error workflow set, n8n runs that one instead of the workflow's own Error Trigger. Test with
one workflow first (see README), then remove the old branches at leisure.
"""
import json, os, subprocess, sys, time, pathlib

BASE = os.environ["N8N_URL"].rstrip("/") + "/api/v1"
KEY = os.environ["N8N_API_KEY"]
ALLOWED_SETTINGS = {"saveExecutionProgress", "saveManualExecutions", "saveDataErrorExecution",
                    "saveDataSuccessExecution", "executionTimeout", "errorWorkflow", "timezone",
                    "executionOrder", "callerPolicy", "callerIds"}


def api(method, path, body=None):
    cmd = ["curl", "-sf", "--max-time", "60", "--retry", "3", "--retry-all-errors", "-X", method, "-H", f"X-N8N-API-KEY: {KEY}",
           "-H", "Content-Type: application/json", BASE + path]
    if body is not None:
        cmd += ["--data-binary", "@-"]
    for attempt in range(5):
        r = subprocess.run(cmd, input=json.dumps(body).encode() if body is not None else None, capture_output=True)
        if r.returncode == 0:
            return json.loads(r.stdout or b"{}")
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{method} {path} failed: curl {r.returncode} {r.stderr[:200]}")


def main():
    args = sys.argv[1:]
    relay = args[0]
    only = args[args.index("--only") + 1].split(",") if "--only" in args else None
    apply = "--apply" in args
    project = args[args.index("--project") + 1] if "--project" in args else None
    backup = pathlib.Path("backup-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir()
    q = "/workflows?active=true&limit=250" + (f"&projectId={project}" if project else "")
    changed = 0
    for w in api("GET", q)["data"]:
        if w["id"] == relay or (only and w["id"] not in only):
            continue
        (backup / f"{w['id']}.json").write_text(json.dumps(w, indent=1, ensure_ascii=False))
        current = (w.get("settings") or {}).get("errorWorkflow")
        if current == relay:
            print(f"  ok        {w['name']}")
            continue
        print(f"  {'SET' if apply else 'would set'}  {w['name']}  (was: {current or 'none'})")
        if apply:
            # Keep every existing setting (e.g. binaryMode); only fall back to the documented
            # subset if the API rejects unknown keys.
            body = {"name": w["name"], "nodes": w["nodes"], "connections": w["connections"],
                    "settings": {**(w.get("settings") or {}), "errorWorkflow": relay},
                    "staticData": w.get("staticData")}
            try:
                api("PUT", f"/workflows/{w['id']}", body)
            except RuntimeError:
                body["settings"] = {k: v for k, v in body["settings"].items() if k in ALLOWED_SETTINGS}
                print(f"    retried with documented settings only (dropped: "
                      f"{sorted(set(w.get('settings') or {}) - ALLOWED_SETTINGS)})")
                api("PUT", f"/workflows/{w['id']}", body)
            after = api("GET", f"/workflows/{w['id']}")
            ok = (after.get("settings") or {}).get("errorWorkflow") == relay and after.get("active") == w.get("active")
            print(f"    verified: errorWorkflow={'ok' if ok else 'MISMATCH'} active={after.get('active')}")
            time.sleep(0.5)
        changed += 1
    print(f"{changed} workflow(s) {'updated' if apply else 'to update (dry run)'}; backups in {backup}/")


if __name__ == "__main__":
    main()
