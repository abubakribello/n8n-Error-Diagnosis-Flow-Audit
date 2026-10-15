// n8n Error Relay: keep only what the diagnosis routine needs, dedupe, and cap daily fires.
// The fingerprint must match scripts/n8n_api.py fingerprint() exactly.
const DEDUPE_HOURS = 24;     // same issue fires the routine at most once per window
const MAX_FIRES_PER_DAY = 20; // stay under the routine plan's daily run cap

const e = $input.first().json;
const ex = e.execution || {};
const wf = e.workflow || {};
const trig = e.trigger || null;            // poll/trigger failures have no execution
const err = ex.error || (trig && trig.error) || {};
const node = ex.lastNodeExecuted || (err.node && err.node.name) || (trig ? 'trigger' : 'unknown');
const message = String(err.message || '');

function normalise(m) {
  let s = (m || '').toLowerCase().replace(/[0-9a-f]{8,}|\d+/g, '#');
  s = s.replace(/"[^"]{0,80}"/g, '"…"');
  return Array.from(s).slice(0, 160).join('');
}
function utf8(str) {
  if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(str);
  return Buffer.from(str, 'utf8');
}
function fnv1a(str, seed) {
  let h = seed >>> 0;
  for (const b of utf8(str)) { h ^= b; h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(16).padStart(8, '0');
}
const key = `${wf.id}|${node}|${normalise(message)}`;
const fingerprint = (fnv1a(key, 0x811C9DC5) + fnv1a(key, 0x01000193)).slice(0, 12);

const sd = $getWorkflowStaticData('global');
sd.seen = sd.seen || {};
sd.suppressed = sd.suppressed || {};
const now = Date.now();
const today = new Date().toISOString().slice(0, 10);
if (sd.day !== today) { sd.day = today; sd.fires = 0; }
for (const [fp, t] of Object.entries(sd.seen)) {          // forget old fingerprints
  if (now - t > 7 * 24 * 3600e3) { delete sd.seen[fp]; delete sd.suppressed[fp]; }
}

let action = 'fire';
if (sd.seen[fingerprint] && now - sd.seen[fingerprint] < DEDUPE_HOURS * 3600e3) action = 'suppress';
else if (sd.fires >= MAX_FIRES_PER_DAY) action = 'cap';

const payload = {
  executionId: ex.id || null,
  runUrl: ex.url || null,
  workflowId: wf.id,
  workflowName: wf.name,
  node,
  message: message.slice(0, 500),
  description: String(err.description || (err.messages || [])[0] || '').slice(0, 500),
  httpCode: err.httpCode || null,
  mode: ex.mode || (trig ? 'trigger' : null),
  failedAt: (err.timestamp ? new Date(err.timestamp) : new Date()).toISOString(),
  isTriggerFailure: !!trig && !ex.id,
  fingerprint,
  suppressedSinceLastReport: sd.suppressed[fingerprint] || 0,
};

if (action === 'fire') { sd.seen[fingerprint] = now; sd.fires += 1; sd.suppressed[fingerprint] = 0; }
else if (action === 'suppress') { sd.suppressed[fingerprint] = (sd.suppressed[fingerprint] || 0) + 1; }

return [{ json: { action, fingerprint, payload, routineText: 'N8N_ERROR ' + JSON.stringify(payload) } }];
