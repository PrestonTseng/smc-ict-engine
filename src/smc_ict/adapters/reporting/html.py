"""Deterministic self-contained HTML projection of canonical backtest evidence."""

# ruff: noqa: E501 -- embedded HTML/JavaScript is intentionally kept byte-stable and compact.

from __future__ import annotations

import json


def render_report(
    *, manifest: dict[str, object], summary: dict[str, object], traces: list[dict[str, object]]
) -> bytes:
    """Render local-only evidence without recalculating strategy outcomes."""

    embedded = json.dumps(
        {"manifest": manifest, "summary": summary, "traces": traces},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).replace("<", "\\u003c")
    title = f"Backtest {manifest['backtest_id']}"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font:14px system-ui,sans-serif;margin:2rem;color:#172033}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccd3df;padding:.45rem;text-align:left;vertical-align:top}} .step{{margin:.25rem 0;padding:.4rem;background:#f3f5f8}} details{{margin:.5rem 0}} input,select{{padding:.4rem;margin:.25rem}} .warning{{font-weight:700;color:#8a3b00}}
</style></head><body><h1>{title}</h1>
<p class="warning">Normalized research evidence only. No position sizing, live orders, or performance promise.</p>
<section id="summary"></section><section><h2>Pipeline explorer</h2>
<label>Instrument <input id="instrument-filter" autocomplete="off"></label>
<label>Evaluation time <input id="evaluation-filter" inputmode="numeric" autocomplete="off"></label>
<label>Disposition <select id="status-filter"><option value="">All</option><option>READY</option><option>NO_TRADE</option><option>UNAVAILABLE</option></select></label>
<label>Failed step <input id="failed-step-filter" autocomplete="off"></label>
<label>Reason <input id="reason-filter" autocomplete="off"></label>
<div id="traces"></div></section>
<script id="backtest-evidence" type="application/json">{embedded}</script><script>
'use strict';const evidence=JSON.parse(document.getElementById('backtest-evidence').textContent);
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
function renderSummary(){{const o=evidence.summary.overall;document.getElementById('summary').innerHTML=`<h2>Summary</h2><table><tr><th>Closed trades</th><th>Win rate</th><th>Net R</th><th>Maximum drawdown R</th></tr><tr><td>${{esc(o.trade_count)}}</td><td>${{esc(o.win_rate)}}</td><td>${{esc(o.cumulative_net_r)}}</td><td>${{esc(o.maximum_drawdown_r)}}</td></tr></table>`;}}
function value(id){{return document.getElementById(id).value.toLowerCase();}}
function renderTraces(){{const instrument=value('instrument-filter');const evaluation=value('evaluation-filter');const status=value('status-filter').toUpperCase();const failed=value('failed-step-filter');const reason=value('reason-filter');const rows=evidence.traces.filter(t=>(!instrument||t.instrument_id.toLowerCase().includes(instrument))&&(!evaluation||String(t.evaluation_time_ms).includes(evaluation))&&(!status||t.disposition===status)&&(!failed||String(t.first_rejection||'').toLowerCase().includes(failed))&&(!reason||t.steps.some(s=>s.reason.toLowerCase().includes(reason))));document.getElementById('traces').innerHTML=rows.map(t=>`<details><summary>${{esc(t.instrument_id)}} · ${{esc(t.evaluation_time_ms)}} · ${{esc(t.disposition)}} · first rejection: ${{esc(t.first_rejection||'none')}}</summary>${{t.steps.map(s=>`<div class="step"><strong>${{esc(s.kind)}} / ${{esc(s.step_id)}}</strong>: ${{esc(s.state)}} — ${{esc(s.reason)}}</div>`).join('')}}</details>`).join('')||'<p>No matching evaluations.</p>';}}
renderSummary();renderTraces();for(const id of ['instrument-filter','evaluation-filter','failed-step-filter','reason-filter']){{document.getElementById(id).addEventListener('input',renderTraces);}}document.getElementById('status-filter').addEventListener('change',renderTraces);
</script></body></html>"""
    return document.encode("utf-8")
