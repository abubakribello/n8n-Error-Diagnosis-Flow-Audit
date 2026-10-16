---
name: n8n-error-diagnosis
description: Fired by the n8n Error Relay for each new failing issue. Diagnoses the failed execution read-only, writes the diagnosis to n8n Error Runs and a linked "New n8n error" review task in Actions Business.
---

# n8n Error Diagnosis (routine)

The trigger text starts with `N8N_ERROR ` followed by JSON:
`executionId, runUrl, workflowId, workflowName, node, message, description, httpCode, mode,
failedAt, isTriggerFailure, fingerprint, suppressedSinceLastReport`.

Target: Notion task within ~3 minutes of firing. Do not explore beyond what the steps need.

## Steps

1. **Parse** the JSON after `N8N_ERROR `. Run all commands from the repo root.

2. **Dedupe in Notion:** `python3 scripts/notion.py find-open <fingerprint>`
   - Prints a page id → `python3 scripts/notion.py bump <page_id> <runUrl>` and stop.
     (n8n already suppresses repeats for 24 h, so this is an issue that is still happening.)
   - Prints nothing → continue.

3. **Build context** (masked; never fetch raw execution data yourself):
   `python3 scripts/build_context.py <executionId> > ctx.json`
   Skip if `isTriggerFailure` is true or `executionId` is null; then diagnose from the payload only.

4. **Classify** into exactly one class:
   | Class | Evidence | Severity default |
   |---|---|---|
   | transient-upstream | 5xx, "Service unavailable", connection reset, trigger poll error | low |
   | rate-limit | 429, "quota", "too many requests" | medium |
   | credential | 401/403, `account_inactive`, invalid/expired token | high |
   | bad-input-data | 4xx naming a field or limit ("phone … invalid", "exceeds 2200 characters", "title must NOT have fewer…") | medium |
   | branch-wiring | "Node X hasn't been executed"; `referenced_nodes_this_run` shows `NOT EXECUTED` | medium |
   | empty-lookup | id/value `null`/empty; upstream search returned `{}` or 0 items | medium |
   | ai-output | AI node answer is a refusal, off-schema, or a downstream parse error | medium |
   | config | wrong URL/parameter/resource, 404 for something that should exist | medium |
   | unknown | none fits | medium |
   Raise severity to high when the workflow handles payments, leads or invoices and data was lost.

5. **Diagnose** from `ctx.json`:
   - Root cause: one or two sentences that cite the exact field/value from the context.
   - Compare `failing_node_input` with `last_successful_run_of_node.input`: what differs?
   - Fix: numbered steps naming the node and the exact expression/setting to change.
     Prefer copy-paste-ready expressions, e.g. `{{ $json.text.slice(0, 2190) }}`.
   - Prevention: one line (retry, validation, If-guard, error output).
   - **Paste-ready node** (when the fix is inside one node): copy that node from
     `failing_node` in `ctx.json` (type, typeVersion, parameters, settings), apply the fix, rename it
     `<old name> (fixed)`, and put it in `patched_node` as an n8n node object
     (`name, type, typeVersion, position: [0,0], parameters`, plus `retryOnFail/maxTries/onError` if changed).
     Leave out `credentials` and any `<SECRET>` placeholder field; the user reconnects credentials.
     Only include it when you are confident the parameters are complete.
   - If `workflow.updatedAt` is later than `failedAt`, the flow was edited after the failure:
     status `Likely resolved – verify`.

6. **Confidence gate:**
   - high: the cause is directly visible in the context.
   - medium: one assumption; write it in `assumption`.
   - low: a guess → status `Needs human`, `fix_steps` become "what to check".
   Never state medium/low as fact.

7. **Write** `diagnosis.json` and run `python3 scripts/notion.py create-task diagnosis.json`:
   ```json
   {"fingerprint": "", "workflow_name": "", "node": "", "run_url": "", "failed_at": "",
    "error_message": "<message + description>", "title": "<6-10 word cause>",
    "class": "", "severity": "high|medium|low", "confidence": "high|medium|low",
    "status": "Open|Needs human|Likely resolved – verify",
    "root_cause": "", "evidence": ["<field = value>", "..."], "fix_steps": ["1. ...", "2. ..."],
    "prevention": "", "assumption": "", "occurrences": 1, "patched_node": null}
   ```
   Set `occurrences` to `1 + suppressedSinceLastReport`.
   **Test mode:** if the payload has `"test": true` (the relay sets it for workflows named
   `[TEST] …`), prefix `title` with `[TEST] ` and run
   `NOTION_MENTION_IDS= python3 scripts/notion.py create-task diagnosis.json`, so only the main
   owner gets the task.

8. If node parameters in `ctx.json` show `<SECRET>` inside a literal header/URL, append to
   `fix_steps`: "Move the secret in node X into an n8n credential" (never the value).

9. Final message: both Notion URLs (Error Runs page and the Actions Business task) and the one-line cause.

## Rules
- Read-only against n8n: never edit, retry, activate or deactivate anything.
- No customer data in Notion beyond what `build_context.py` left unmasked and what the fix needs.
- If a script fails, still create the task from the payload alone with confidence low and
  say in Evidence which step failed.
