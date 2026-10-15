# n8n ops routines

Two Claude Code routines for the DigitalXShift n8n instance (jkdxs.app.n8n.cloud):

1. **Error diagnosis**: a workflow fails, the n8n relay fires the routine, and the routine writes a
   Notion task that assigns and @mentions you. The task has the error, root cause, evidence, fix,
   a paste-ready fixed node and the run link. It usually takes a few minutes.
2. **Flow audit**: on the 1st and 15th of each month, a recon of every published workflow
   ends as one Notion report with prioritized recommendations.

```
workflow fails ─► [UTIL] Error Relay (n8n) ─► routine /fire ─► error-diagnosis/SKILL.md ─► Notion task (@you)
                   fingerprint, 24 h dedupe,                    build_context.py (masked)
                   20 fires/day cap, Slack fallback             notion.py create-task / bump

cron 1st+15th ─► audit routine ─► audit_collect.py ─► audit/SKILL.md ─► Notion "n8n Audit YYYY-MM-DD"
```

Both routines are read-only against n8n. Only Notion is written to.

## Files

| Path | What it does |
|---|---|
| `n8n/error-relay.workflow.json` | Import into n8n. Error Trigger → fingerprint + dedupe → fire routine, with a Slack fallback |
| `n8n/relay_code.js` | Source of the relay's Code node (kept in sync with the JSON) |
| `n8n/set_error_workflow.py` | Points every published workflow's error setting at the relay in one go (dry run by default) |
| `error-diagnosis/SKILL.md` | What the diagnosis routine does |
| `audit/SKILL.md` | What the audit routine does |
| `scripts/build_context.py` | Masked diagnosis context for one execution (path, input, referenced nodes, last good run) |
| `scripts/audit_collect.py` | All audit facts in one JSON (usage, issues, hidden failures, models, security) |
| `scripts/notion.py` | Notion schema setup, dedupe lookup, task and audit page writer |
| `scripts/n8n_api.py` | Shared API helper with backoff and the fingerprint (the same algorithm as the relay JS) |

## Setup (about 30 minutes, once)

### 1. Notion
1. Create an internal integration at notion.so/profile/integrations. Turn on the capabilities
   *Read content*, *Update content*, *Insert content* and *Read user information including email*.
2. Share the **n8n Error Runs** database and an **n8n Audits** parent page with the integration.
3. Locally, once:
   ```bash
   export NOTION_TOKEN=secret_... NOTION_ERRORS_DB=2df9e268-c9cb-4145-b4f4-032c163106bf
   python3 scripts/notion.py ensure-schema          # adds Fingerprint, Class, Severity, Confidence, ...
   python3 scripts/notion.py find-user you@example.com  # -> NOTION_ASSIGNEE_ID
   ```

### 2. GitHub
Create a private repo `abubakribello/n8n-ops-routines` and push this folder.

### 3. Claude environment
At claude.ai/code, create an environment named `n8n-ops`.
- **Network access:** allow `jkdxs.app.n8n.cloud`, `api.notion.com`, and the model providers' docs (for the audit's model check).
- **Environment variables:**
  ```
  N8N_URL=https://jkdxs.app.n8n.cloud
  N8N_API_KEY=<read-only n8n key>
  N8N_PROJECT_ID=4sp9QkFI4paUSDqk
  NOTION_TOKEN=<integration secret>
  NOTION_ERRORS_DB=2df9e268-c9cb-4145-b4f4-032c163106bf
  NOTION_AUDIT_PARENT=<page id of "n8n Audits">
  NOTION_ASSIGNEE_ID=<your Notion user id>
  ```

### 4. Routine: n8n Error Diagnosis (API trigger)
New routine → repo `n8n-ops-routines`, environment `n8n-ops`, model Sonnet, trigger **API**. Prompt:
```
You diagnose failed n8n workflow runs. Read and follow error-diagnosis/SKILL.md in this
repo exactly, using the incoming trigger text (it starts with "N8N_ERROR ") as the input.
Always follow the SKILL.md from the repo, not from memory. Finish fast: one Notion task or
one bump, then stop.
```
Copy the routine ID and the API token.

### 5. Routine: n8n Flow Audit (schedule)
Same repo, environment and model. Schedule: 1st and 15th at 07:00 Africa/Lagos
(cron `0 6 1,15 * *` UTC). Prompt:
```
You audit all published n8n workflows. Read and follow audit/SKILL.md in this repo exactly.
Always follow the SKILL.md from the repo, not from memory.
```

### 6. n8n relay
1. Import `n8n/error-relay.workflow.json`.
2. In "Fire diagnosis routine", replace `REPLACE_WITH_ROUTINE_ID`. Create a **Header Auth**
   credential named "Diagnosis routine token": name `Authorization`, value `Bearer <routine API token>`.
3. Check that "Fallback Slack alert" uses the working Slack credential, then **publish** the
   workflow. Static-data dedupe only persists in production runs.

### 7. Point workflows at the relay
- **One-off test first:** in one workflow, open Settings → Error workflow, choose the relay, and
  make that workflow fail once (for example, a webhook test with a bad payload). Check that a Notion task appears and that the old
  in-workflow error branch did *not* fire.
- **Then all at once.** Make a temporary n8n key with write access and run:
  ```bash
  N8N_URL=https://jkdxs.app.n8n.cloud N8N_API_KEY=<write key> \
    python3 n8n/set_error_workflow.py <relayWorkflowId> --project 4sp9QkFI4paUSDqk          # dry run
  N8N_URL=... N8N_API_KEY=<write key> python3 n8n/set_error_workflow.py <relayWorkflowId> --project 4sp9QkFI4paUSDqk --apply
  ```
  Delete the write key afterwards. The old Error Trigger branches can be removed later. They stop
  firing once the setting points elsewhere.

## Limits and behaviour
- Errors only reach the relay from production runs (webhook, trigger, schedule), not from manual test runs.
- The same fingerprint fires the routine at most once per 24 h. Repeats are counted and reported on the
  next fire, and a repeat after 24 h bumps the open Notion task instead of creating a new one.
- More than 20 fires in a day go to Slack only, so the routine plan's daily cap isn't exhausted.
- If the routine can't be reached, the error goes to Slack as a plain alert.
