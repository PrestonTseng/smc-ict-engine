"""Deterministic self-contained HTML projection of canonical backtest evidence."""

# ruff: noqa: E501 -- embedded HTML/JavaScript is intentionally kept byte-stable and compact.

from __future__ import annotations

import json
from html import escape

_PAGE_SIZE = 25


def render_report(
    *,
    manifest: dict[str, object],
    summary: dict[str, object],
    decisions: list[dict[str, object]],
    traces: list[dict[str, object]],
    trades: list[dict[str, object]],
) -> bytes:
    """Render local-only evidence without recalculating strategy outcomes."""

    embedded = (
        json.dumps(
            {
                "manifest": manifest,
                "summary": summary,
                "decisions": decisions,
                "traces": traces,
                "trades": trades,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
    )
    title = f"Backtest {escape(str(manifest['backtest_id']))}"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; style-src 'nonce-backtest-report'; script-src 'nonce-backtest-report'; script-src-attr 'none'">
<title>{title}</title><style nonce="backtest-report">
body{{font:14px system-ui,sans-serif;margin:2rem;color:#172033}} section{{margin:1.5rem 0}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccd3df;padding:.45rem;text-align:left;vertical-align:top}} .step{{margin:.25rem 0;padding:.4rem;background:#f3f5f8}} details{{margin:.5rem 0}} input,select,button{{padding:.4rem;margin:.25rem}} .warning{{font-weight:700;color:#8a3b00}} .pager{{display:flex;align-items:center;gap:.4rem;margin:.5rem 0}}
</style></head><body><h1>{title}</h1>
<p class="warning">Normalized research evidence only. No position sizing, live orders, or performance promise.</p>
<section id="overall-summary"></section><section id="instrument-summary"></section><section id="direction-summary"></section>
<section id="trade-outcomes"><h2>Trade outcomes</h2><div id="trade-counts"></div><div id="trade-table"></div><div class="pager"><button id="trade-prev" type="button">Previous</button><span id="trade-page"></span><button id="trade-next" type="button">Next</button></div></section>
<section id="decision-outcomes"><h2>Decision outcomes</h2><div id="decision-counts"></div><div id="decision-table"></div><div class="pager"><button id="decision-prev" type="button">Previous</button><span id="decision-page"></span><button id="decision-next" type="button">Next</button></div></section>
<section id="pipeline-explorer"><h2>Pipeline explorer</h2>
<label>Instrument <input id="instrument-filter" autocomplete="off"></label>
<label>Evaluation time <input id="evaluation-filter" inputmode="numeric" autocomplete="off"></label>
<label>Disposition <select id="status-filter"><option value="">All</option><option>READY</option><option>NO_TRADE</option><option>UNAVAILABLE</option></select></label>
<label>Failed step <input id="failed-step-filter" autocomplete="off"></label>
<label>Reason <input id="reason-filter" autocomplete="off"></label>
<div id="traces"></div><div class="pager"><button id="trace-first" type="button">First</button><button id="trace-prev" type="button">Previous</button><span id="trace-page"></span><label>Page <input id="trace-page-input" type="number" min="1" value="1"></label><button id="trace-go" type="button">Go</button><button id="trace-next" type="button">Next</button><button id="trace-last" type="button">Last</button></div></section>
<script id="backtest-evidence" type="application/json" nonce="backtest-report">{embedded}</script><script nonce="backtest-report">
'use strict';const evidence=JSON.parse(document.getElementById('backtest-evidence').textContent);
const PAGE_SIZE={_PAGE_SIZE};const pages={{trade:0,decision:0,trace:0}};
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const display=v=>Array.isArray(v)?v.map(item=>Array.isArray(item)?item.join(': '):item).join(', '):v;
const table=(headers,rows)=>`<table><thead><tr>${{headers.map(value=>`<th>${{esc(value)}}</th>`).join('')}}</tr></thead><tbody>${{rows.map(row=>`<tr>${{row.map(value=>`<td>${{esc(display(value))}}</td>`).join('')}}</tr>`).join('')}}</tbody></table>`;
function renderMetrics(id,title,groups){{if(!groups.length){{document.getElementById(id).innerHTML=`<h2>${{esc(title)}}</h2><p>No trades.</p>`;return;}}const fields=Object.keys(groups[0][1]);document.getElementById(id).innerHTML=`<h2>${{esc(title)}}</h2>${{table(['Group',...fields],groups.map(([name,metrics])=>[name,...fields.map(field=>metrics[field])]))}}`;}}
function pageCount(rows){{return Math.max(1,Math.ceil(rows.length/PAGE_SIZE));}}
function boundedPage(kind,rows){{pages[kind]=Math.max(0,Math.min(pages[kind],pageCount(rows)-1));const start=pages[kind]*PAGE_SIZE;return rows.slice(start,start+PAGE_SIZE);}}
function updatePager(kind,rows){{const count=pageCount(rows);document.getElementById(`${{kind}}-page`).textContent=`Page ${{pages[kind]+1}} of ${{count}} · ${{rows.length}} records`;document.getElementById(`${{kind}}-prev`).disabled=pages[kind]===0;document.getElementById(`${{kind}}-next`).disabled=pages[kind]===count-1;if(kind==='trace'){{const input=document.getElementById('trace-page-input');input.max=String(count);input.value=String(pages.trace+1);}}}}
function renderSummary(){{renderMetrics('overall-summary','Overall summary',[['Overall',evidence.summary.overall]]);renderMetrics('instrument-summary','By instrument',evidence.summary.by_instrument);renderMetrics('direction-summary','By direction',evidence.summary.by_direction);}}
function renderTrades(){{const rows=evidence.trades;const visible=boundedPage('trade',rows);document.getElementById('trade-counts').innerHTML=`<h3>Status</h3>${{table(['Status','Count'],evidence.summary.overall.status_counts)}}<h3>Exit reason</h3>${{table(['Reason','Count'],evidence.summary.overall.exit_reason_counts)}}`;document.getElementById('trade-table').innerHTML=visible.length?table(['Instrument','Direction','Status','Signal time','Exit reason','Net R'],visible.map(t=>[t.instrument_id,t.direction,t.status,t.signal_time_ms,t.exit_reason,t.net_r])):'<p>No trades.</p>';updatePager('trade',rows);}}
function renderDecisions(){{const rows=evidence.decisions;const visible=boundedPage('decision',rows);document.getElementById('decision-counts').innerHTML=`<h3>Disposition</h3>${{table(['Disposition','Count'],evidence.summary.decision_status_counts)}}<h3>Unavailable reason</h3>${{table(['Reason','Count'],evidence.summary.unavailable_reason_counts)}}`;document.getElementById('decision-table').innerHTML=visible.length?table(['Instrument','Evaluation time','Disposition','Direction','First failed signal'],visible.map(row=>[row.instrument_id,row.evaluation_time_ms,row.decision.status,row.decision.direction,row.decision.first_failed_signal])):'<p>No decisions.</p>';updatePager('decision',rows);}}
function value(id){{return document.getElementById(id).value.toLowerCase();}}
function matchingTraces(){{const instrument=value('instrument-filter');const evaluation=value('evaluation-filter');const status=value('status-filter').toUpperCase();const failed=value('failed-step-filter');const reason=value('reason-filter');return evidence.traces.map((trace,index)=>({{...trace,disposition:evidence.decisions[index].decision.status}})).filter(t=>(!instrument||t.instrument_id.toLowerCase().includes(instrument))&&(!evaluation||String(t.evaluation_time_ms).includes(evaluation))&&(!status||t.disposition===status)&&(!failed||String(t.first_rejection||'').toLowerCase().includes(failed))&&(!reason||t.steps.some(s=>String(s.reason||'').toLowerCase().includes(reason))));}}
function renderTraces(){{const rows=matchingTraces();const visible=boundedPage('trace',rows);document.getElementById('traces').innerHTML=visible.map(t=>`<details><summary>${{esc(t.instrument_id)}} · ${{esc(t.evaluation_time_ms)}} · ${{esc(t.disposition)}} · first rejection: ${{esc(t.first_rejection||'none')}}</summary>${{t.steps.map(s=>`<div class="step"><strong>${{esc(s.kind)}} / ${{esc(s.step_id)}}</strong>: ${{esc(s.state)}} — ${{esc(s.reason)}}</div>`).join('')}}</details>`).join('')||'<p>No matching evaluations.</p>';updatePager('trace',rows);}}
function move(kind,delta,render){{pages[kind]+=delta;render();}}function lastTrace(){{pages.trace=pageCount(matchingTraces())-1;renderTraces();}}function jumpTrace(){{pages.trace=Number(document.getElementById('trace-page-input').value)-1;renderTraces();}}
renderSummary();renderTrades();renderDecisions();renderTraces();for(const id of ['instrument-filter','evaluation-filter','failed-step-filter','reason-filter']){{document.getElementById(id).addEventListener('input',()=>{{pages.trace=0;renderTraces();}});}}document.getElementById('status-filter').addEventListener('change',()=>{{pages.trace=0;renderTraces();}});
document.getElementById('trade-prev').addEventListener('click',()=>move('trade',-1,renderTrades));document.getElementById('trade-next').addEventListener('click',()=>move('trade',1,renderTrades));document.getElementById('decision-prev').addEventListener('click',()=>move('decision',-1,renderDecisions));document.getElementById('decision-next').addEventListener('click',()=>move('decision',1,renderDecisions));document.getElementById('trace-first').addEventListener('click',()=>{{pages.trace=0;renderTraces();}});document.getElementById('trace-prev').addEventListener('click',()=>move('trace',-1,renderTraces));document.getElementById('trace-go').addEventListener('click',jumpTrace);document.getElementById('trace-page-input').addEventListener('change',jumpTrace);document.getElementById('trace-next').addEventListener('click',()=>move('trace',1,renderTraces));document.getElementById('trace-last').addEventListener('click',lastTrace);
</script></body></html>"""
    return document.encode("utf-8")
