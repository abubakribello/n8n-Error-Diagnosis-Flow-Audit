---
name: n8n-flow-audit
description: Scheduled recon of all published n8n workflows (usage, errors, hidden failures, outdated AI models and nodes, security gaps, clutter) delivered as one Notion report with prioritized recommendations.
---

# n8n Flow Audit (routine, bi-weekly)

## Steps

1. **Collect** (read-only, takes a few minutes):
   `python3 scripts/audit_collect.py --days 14 --project "$N8N_PROJECT_ID" --out audit_data.json`
   If the routine runs monthly, use `--days 30`.

2. **Current models.** Look up the providers' current model lists and deprecation pages at run
   time (Anthropic, OpenAI, Google Gemini). Mark each id in `totals.models_in_use` as current,
   older-but-supported, or deprecated/retired, with the source URL. Do not rely on memory.

3. **Analyse** `audit_data.json`. For each workflow decide one of:
   *healthy*, *fix* (has open issues or silent failures), *harden* (security/retry/error handling),
   *upgrade* (models/nodes), *retire* (`unused: true`, i.e. no runs in the window and not called
   by another workflow; say "unpublish or archive").
   Use the error `issues` (already grouped by fingerprint) for the recurring-error section.

4. **Compare with the last audit.** Find the most recent page titled "n8n Audit" under
   `NOTION_AUDIT_PARENT` (Notion search) and list: fixed since last time, new, still open.
   Skip this section on the first run.

5. **Write** `audit.md` in this shape (plain markdown: `##`, `###`, `- ` only):
   ```
   ## Summary
   - 5 bullets with the window, runs, errors, unique issues, unused flows
   ## Top 5 actions
   - action, workflow, why (number from the data), effort (S/M/L)
   ## Recurring errors
   ## Hidden failures (successful runs that swallowed errors)
   ## Retire candidates
   ## Security
   ## AI models and node versions
   ## Per-workflow status
   - name: status, runs ok/err, one-line note
   ## Changes since last audit
   ```
   Then `python3 scripts/notion.py create-audit audit.md "n8n Audit YYYY-MM-DD"`.

6. Final message: the Notion URL of the report, the URL of the "Review n8n Audit …" task that
   `create-audit` also creates in Actions Business, and the top 3 actions.

## Rules
- Read-only against n8n.
- Every number must come from `audit_data.json` of this run; state the window.
- Recommendations name the workflow and node, and say what to change, not just "improve".
