"""Deterministic self-contained HTML projection of canonical backtest evidence."""

# ruff: noqa: E501 -- embedded HTML/JavaScript is intentionally kept byte-stable and compact.

from __future__ import annotations

import base64
import io
import json
import zlib
from collections.abc import Callable, Iterable
from html import escape
from typing import BinaryIO

_PAGE_SIZE = 25
type RowSource = Iterable[dict[str, object]] | Callable[[], Iterable[dict[str, object]]]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _gzip_base64(value: object) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=31)
    compressed = compressor.compress(_canonical_json(value)) + compressor.flush()
    return base64.b64encode(compressed)


def _chunked(rows: Iterable[object]) -> Iterable[list[object]]:
    chunk: list[object] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) == _PAGE_SIZE:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _rows(source: RowSource) -> Iterable[dict[str, object]]:
    return source() if callable(source) else source


def _write_encoded_chunks(handle: BinaryIO, rows: Iterable[object]) -> int:
    handle.write(b"[")
    separator = b""
    count = 0
    for chunk in _chunked(rows):
        handle.write(separator + b'"' + _gzip_base64(chunk) + b'"')
        separator = b","
        count += len(chunk)
    handle.write(b"]")
    return count


def _write_encoded_records(handle: BinaryIO, rows: Iterable[object]) -> int:
    handle.write(b"[")
    separator = b""
    count = 0
    for row in rows:
        handle.write(separator + b'"' + _gzip_base64(row) + b'"')
        separator = b","
        count += 1
    handle.write(b"]")
    return count


def write_report(
    handle: BinaryIO,
    *,
    manifest: dict[str, object],
    summary: dict[str, object],
    decisions: RowSource,
    traces: RowSource,
    trades: RowSource,
) -> None:
    """Stream local-only evidence without recalculating strategy outcomes."""

    title = f"Backtest {escape(str(manifest['backtest_id']))}"
    prefix = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; style-src 'nonce-backtest-report'; script-src 'nonce-backtest-report'; script-src-attr 'none'">
<title>{title}</title><style nonce="backtest-report">
body{{font:14px system-ui,sans-serif;margin:2rem;color:#172033}} section{{margin:1.5rem 0}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccd3df;padding:.45rem;text-align:left;vertical-align:top}} .step{{margin:.25rem 0;padding:.4rem;background:#f3f5f8}} details{{margin:.5rem 0}} input,select,button{{padding:.4rem;margin:.25rem}} .warning{{font-weight:700;color:#8a3b00}} .pager{{display:flex;align-items:center;gap:.4rem;margin:.5rem 0}}
</style></head><body><h1>{title}</h1>
<p class="warning">Normalized research evidence only. No position sizing, live orders, or performance promise.</p>
<p id="compatibility" class="warning" hidden>This browser cannot decompress the embedded backtest evidence. No evidence has been omitted; use a browser with DecompressionStream gzip support.</p>
<section id="overall-summary"></section><section id="instrument-summary"></section><section id="direction-summary"></section>
<section id="trade-outcomes"><h2>Trade outcomes</h2><div id="trade-counts"></div><div id="trade-table"></div><div class="pager"><button id="trade-prev" type="button">Previous</button><span id="trade-page"></span><button id="trade-next" type="button">Next</button></div></section>
<section id="decision-outcomes"><h2>Decision outcomes</h2><div id="decision-counts"></div><div id="decision-table"></div><div class="pager"><button id="decision-prev" type="button">Previous</button><span id="decision-page"></span><button id="decision-next" type="button">Next</button></div></section>
<section id="pipeline-explorer"><h2>Pipeline explorer</h2>
<label>Instrument <input id="instrument-filter" autocomplete="off"></label>
<label>Direction <select id="direction-filter"><option value="">All</option><option>LONG</option><option>SHORT</option></select></label>
<label>Evaluation time <input id="evaluation-filter" inputmode="numeric" autocomplete="off"></label>
<label>Disposition <select id="status-filter"><option value="">All</option><option>READY</option><option>NO_TRADE</option><option>UNAVAILABLE</option></select></label>
<label>First rejection <input id="failed-step-filter" autocomplete="off"></label>
<label>Reason <input id="reason-filter" autocomplete="off"></label>
<div id="traces"></div><div class="pager"><button id="trace-first" type="button">First</button><button id="trace-prev" type="button">Previous</button><span id="trace-page"></span><label>Page <input id="trace-page-input" type="number" min="1" value="1"></label><button id="trace-go" type="button">Go</button><button id="trace-next" type="button">Next</button><button id="trace-last" type="button">Last</button></div></section>
<script id="backtest-evidence" type="application/json" nonce="backtest-report">"""
    handle.write(prefix.encode("utf-8"))
    handle.write(b'{"compression":"gzip","schema_version":3,"page_size":25')
    handle.write(b',"manifest":"' + _gzip_base64(manifest) + b'"')
    handle.write(b',"summary":"' + _gzip_base64(summary) + b'"')

    def evaluation_rows() -> Iterable[dict[str, object]]:
        for decision, trace in zip(_rows(decisions), _rows(traces), strict=True):
            yield {"decision": decision, "trace": trace}

    def evaluation_index_rows() -> Iterable[list[object]]:
        for decision, trace in zip(_rows(decisions), _rows(traces), strict=True):
            decision_value = decision.get("decision")
            decision_body = decision_value if isinstance(decision_value, dict) else {}
            steps_value = trace.get("steps")
            steps = steps_value if isinstance(steps_value, list) else []
            reasons = "\n".join(
                str(step.get("reason", "")) for step in steps if isinstance(step, dict)
            )
            yield [
                str(decision.get("instrument_id", trace.get("instrument_id", ""))),
                str(decision_body.get("direction") or ""),
                str(decision_body.get("status") or ""),
                str(trace.get("first_rejection") or ""),
                reasons,
                str(decision.get("evaluation_time_ms", trace.get("evaluation_time_ms", ""))),
            ]

    handle.write(b',"evaluation_records":')
    evaluation_count = _write_encoded_records(handle, evaluation_rows())
    handle.write(b',"evaluation_count":' + str(evaluation_count).encode("ascii"))
    handle.write(b',"evaluation_index_chunks":')
    index_count = _write_encoded_chunks(handle, evaluation_index_rows())
    if index_count != evaluation_count:
        raise ValueError("evaluation index does not match evaluation chunks")
    handle.write(b',"trade_chunks":')
    trade_count = _write_encoded_chunks(handle, _rows(trades))
    handle.write(b',"trade_count":' + str(trade_count).encode("ascii"))
    handle.write(b"}")

    suffix = f"""</script><script nonce="backtest-report">
'use strict';const store=JSON.parse(document.getElementById('backtest-evidence').textContent);const PAGE_SIZE={_PAGE_SIZE};const pages={{trade:0,decision:0,trace:0}},evaluationCache=new Map();let summary,traceMatchCount=store.evaluation_count;
const esc=v=>String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
const display=v=>Array.isArray(v)?v.map(item=>Array.isArray(item)?item.join(': '):item).join(', '):v;
const table=(headers,rows)=>`<table><thead><tr>${{headers.map(value=>`<th>${{esc(value)}}</th>`).join('')}}</tr></thead><tbody>${{rows.map(row=>`<tr>${{row.map(value=>`<td>${{esc(display(value))}}</td>`).join('')}}</tr>`).join('')}}</tbody></table>`;
async function gunzip(encoded){{const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));return JSON.parse(await new Response(stream).text());}}
function renderMetrics(id,title,groups){{if(!groups.length){{document.getElementById(id).innerHTML=`<h2>${{esc(title)}}</h2><p>No trades.</p>`;return;}}const fields=Object.keys(groups[0][1]);document.getElementById(id).innerHTML=`<h2>${{esc(title)}}</h2>${{table(['Group',...fields],groups.map(([name,metrics])=>[name,...fields.map(field=>metrics[field])]))}}`;}}
function pageCount(count){{return Math.max(1,Math.ceil(count/PAGE_SIZE));}}function bound(kind,count){{pages[kind]=Math.max(0,Math.min(pages[kind],pageCount(count)-1));}}
function updatePager(kind,count){{bound(kind,count);const pagesCount=pageCount(count);document.getElementById(`${{kind}}-page`).textContent=`Page ${{pages[kind]+1}} of ${{pagesCount}} · ${{count}} records`;document.getElementById(`${{kind}}-prev`).disabled=pages[kind]===0;document.getElementById(`${{kind}}-next`).disabled=pages[kind]===pagesCount-1;if(kind==='trace'){{const input=document.getElementById('trace-page-input');input.max=String(pagesCount);input.value=String(pages.trace+1);}}}}
async function chunkRows(chunks,indexes){{const decoded=new Map();for(const index of indexes){{const chunk=Math.floor(index/PAGE_SIZE);if(!decoded.has(chunk))decoded.set(chunk,await gunzip(chunks[chunk]));}}return indexes.map(index=>decoded.get(Math.floor(index/PAGE_SIZE))[index%PAGE_SIZE]);}}
async function evaluationRows(indexes){{const selected=new Set(indexes);for(const index of indexes){{if(!evaluationCache.has(index))evaluationCache.set(index,gunzip(store.evaluation_records[index]));}}const rows=await Promise.all(indexes.map(index=>evaluationCache.get(index)));for(const index of evaluationCache.keys()){{if(!selected.has(index))evaluationCache.delete(index);}}return rows;}}
function pageRange(kind,count){{bound(kind,count);const start=pages[kind]*PAGE_SIZE,indexes=[];for(let index=start;index<Math.min(start+PAGE_SIZE,count);index++)indexes.push(index);return indexes;}}
function renderSummary(){{renderMetrics('overall-summary','Overall summary',[['Overall',summary.overall]]);renderMetrics('instrument-summary','By instrument',summary.by_instrument);renderMetrics('direction-summary','By direction',summary.by_direction);}}
async function renderTrades(){{const count=store.trade_count;const indexes=pageRange('trade',count);const visible=await chunkRows(store.trade_chunks,indexes);document.getElementById('trade-counts').innerHTML=`<h3>Status</h3>${{table(['Status','Count'],summary.overall.status_counts)}}<h3>Exit reason</h3>${{table(['Reason','Count'],summary.overall.exit_reason_counts)}}`;document.getElementById('trade-table').innerHTML=visible.length?table(['Instrument','Direction','Status','Signal time','Exit reason','Net R'],visible.map(t=>[t.instrument_id,t.direction,t.status,t.signal_time_ms,t.exit_reason,t.net_r])):'<p>No trades.</p>';updatePager('trade',count);}}
async function renderDecisions(){{const indexes=pageRange('decision',store.evaluation_count);const visible=await evaluationRows(indexes);document.getElementById('decision-counts').innerHTML=`<h3>Disposition</h3>${{table(['Disposition','Count'],summary.decision_status_counts)}}<h3>Unavailable reason</h3>${{table(['Reason','Count'],summary.unavailable_reason_counts)}}`;document.getElementById('decision-table').innerHTML=visible.length?table(['Instrument','Evaluation time','Disposition','Direction','First failed signal'],visible.map(item=>{{const row=item.decision;return [row.instrument_id,row.evaluation_time_ms,row.decision.status,row.decision.direction,row.decision.first_failed_signal];}})):'<p>No decisions.</p>';updatePager('decision',store.evaluation_count);}}
function value(id){{return document.getElementById(id).value.toLowerCase();}}function hasFilters(){{return ['instrument-filter','direction-filter','evaluation-filter','status-filter','failed-step-filter','reason-filter'].some(id=>value(id));}}function matcher(){{const instrument=value('instrument-filter'),direction=value('direction-filter'),evaluation=value('evaluation-filter'),status=value('status-filter'),failed=value('failed-step-filter'),reason=value('reason-filter');return row=>(!instrument||row[0].toLowerCase().includes(instrument))&&(!direction||row[1].toLowerCase()===direction)&&(!status||row[2].toLowerCase()===status)&&(!failed||row[3].toLowerCase().includes(failed))&&(!reason||row[4].toLowerCase().includes(reason))&&(!evaluation||row[5].includes(evaluation));}}
async function matchingPageAndCount(){{if(!hasFilters())return {{count:store.evaluation_count,indexes:pageRange('trace',store.evaluation_count)}};const include=matcher(),requestedPage=Math.max(0,pages.trace),start=Math.max(0,pages.trace)*PAGE_SIZE,selected=[],last=[];let count=0,index=0;for(const encoded of store.evaluation_index_chunks){{const chunk=await gunzip(encoded);for(const row of chunk){{if(include(row)){{if(count>=start&&count<start+PAGE_SIZE)selected.push(index);last.push(index);if(last.length>PAGE_SIZE)last.shift();count++;}}index++;}}}}bound('trace',count);return {{count,indexes:pages.trace===requestedPage?selected:last}};}}
async function renderTraces(){{const match=await matchingPageAndCount();traceMatchCount=match.count;const visible=await evaluationRows(match.indexes);document.getElementById('traces').innerHTML=visible.map(item=>{{const t=item.trace,d=item.decision.decision;return `<details><summary>${{esc(t.instrument_id)}} · ${{esc(t.evaluation_time_ms)}} · ${{esc(d.status)}} · ${{esc(d.direction)}} · first rejection: ${{esc(t.first_rejection||'none')}}</summary>${{t.steps.map(s=>`<div class="step"><strong>${{esc(s.kind)}} / ${{esc(s.step_id)}}</strong>: ${{esc(s.state)}} — ${{esc(s.reason)}}</div>`).join('')}}</details>`;}}).join('')||'<p>No matching evaluations.</p>';updatePager('trace',match.count);}}
function move(kind,delta,render){{pages[kind]+=delta;void render();}}function lastTrace(){{pages.trace=pageCount(traceMatchCount)-1;void renderTraces();}}function jumpTrace(){{pages.trace=Number(document.getElementById('trace-page-input').value)-1;void renderTraces();}}
async function init(){{if(typeof DecompressionStream==='undefined'){{document.getElementById('compatibility').hidden=false;return;}}try{{summary=await gunzip(store.summary);renderSummary();await Promise.all([renderTrades(),renderDecisions(),renderTraces()]);document.documentElement.dataset.reportReady='true';}}catch(error){{document.getElementById('compatibility').hidden=false;document.getElementById('compatibility').textContent='Embedded backtest evidence could not be decompressed; no partial evidence is shown.';console.error(error);}}}}
for(const id of ['instrument-filter','evaluation-filter','failed-step-filter','reason-filter']){{document.getElementById(id).addEventListener('input',()=>{{pages.trace=0;void renderTraces();}});}}for(const id of ['status-filter','direction-filter']){{document.getElementById(id).addEventListener('change',()=>{{pages.trace=0;void renderTraces();}});}}
document.getElementById('trade-prev').addEventListener('click',()=>move('trade',-1,renderTrades));document.getElementById('trade-next').addEventListener('click',()=>move('trade',1,renderTrades));document.getElementById('decision-prev').addEventListener('click',()=>move('decision',-1,renderDecisions));document.getElementById('decision-next').addEventListener('click',()=>move('decision',1,renderDecisions));document.getElementById('trace-first').addEventListener('click',()=>{{pages.trace=0;void renderTraces();}});document.getElementById('trace-prev').addEventListener('click',()=>move('trace',-1,renderTraces));document.getElementById('trace-go').addEventListener('click',jumpTrace);document.getElementById('trace-page-input').addEventListener('change',jumpTrace);document.getElementById('trace-next').addEventListener('click',()=>move('trace',1,renderTraces));document.getElementById('trace-last').addEventListener('click',lastTrace);void init();
</script></body></html>"""
    handle.write(suffix.encode("utf-8"))


def render_report(
    *,
    manifest: dict[str, object],
    summary: dict[str, object],
    decisions: RowSource,
    traces: RowSource,
    trades: RowSource,
) -> bytes:
    """Render a report into memory; publishers should use :func:`write_report`."""

    handle = io.BytesIO()
    write_report(
        handle,
        manifest=manifest,
        summary=summary,
        decisions=decisions,
        traces=traces,
        trades=trades,
    )
    return handle.getvalue()
