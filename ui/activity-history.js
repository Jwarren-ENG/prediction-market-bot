import {arr, clock, count, day, esc, human, instant, stamp, zoneLabel} from './common.js';

const ranges = new Map([[24,'24h'],[48,'48h'],[168,'7d']]);
const total = values => values.reduce((sum,value) => sum + value,0);

export function historyPanel(history, hours) {
  const controls = [...ranges].map(([value,label]) =>
    `<button type="button" data-action="activity-range" data-value="${value}" aria-pressed="${value === hours}">${label}</button>`).join('');
  const head = `<div class="panel-head"><h2 id="history-title">Recorded activity</h2><div class="activity-ranges" role="group" aria-label="Activity time range">${controls}</div></div>`;
  const section = body => `<section class="panel receipt-history" aria-labelledby="history-title">${head}${body}</section>`;
  if (history?.hours !== hours) return section('<p class="activity-caption">Loading recorded activity…</p>');
  const decisions = arr(history.decisions?.counts), scans = arr(history.scans?.counts);
  const start = instant(history.start_at)?.getTime(), end = instant(history.end_at)?.getTime();
  if (!decisions.length || !Number.isFinite(start) || !Number.isFinite(end)) return section('<p class="activity-caption">Waiting for recorded activity.</p>');
  const maximum = Math.max(1,...decisions,...scans);
  const width = 1000/decisions.length;
  const bucket = history.bin_seconds*1000;
  const stats = [`<span><i class="key-decisions"></i>Decision changes <strong>${count(total(decisions))}</strong></span>`];
  if (scans.length) stats.push(`<span><i class="key-scans"></i>Finished scans <strong>${count(total(scans))}</strong></span>`);
  const rows = [], bars = [];
  for (let i=0; i<decisions.length; i++) {
    const at = start + i*bucket, decision = decisions[i], scan = scans[i];
    const time = stamp(at), title = `${time} · ${decision} decision changes${scan == null ? '' : ` · ${scan} finished scans`}`;
    const rect = (amount,offset,kind) => amount ? `<rect class="${kind}" x="${(i+offset)*width}" y="${112-106*amount/maximum}" width="${width*.31}" height="${106*amount/maximum}"/>` : '';
    bars.push(`<g><title>${esc(title)}</title>${rect(decision,.12,'decision-bar')}${rect(scan,.47,'scan-bar')}</g>`);
    rows.push(`<tr><th scope="row">${esc(time)}</th><td>${count(decision)}</td>${scan == null ? '' : `<td>${count(scan)}</td>`}</tr>`);
  }
  const ticks = [start,(start+end)/2,end].map(at =>
    `<time title="${esc(stamp(at))}">${esc(day(at,true))} · ${esc(clock(at).slice(0,5))}</time>`).join('');
  const first = [history.decisions?.first_at,history.scans?.first_at].map(instant).filter(Boolean).sort((a,b)=>a-b)[0];
  const coverage = first && first.getTime()>start ? ` The earliest retained receipt is from ${stamp(first)}.` : '';
  return section(`<div class="receipt-chart"><div class="receipt-legend">${stats.join('')}</div>
    <div class="receipt-scale"><span>${count(maximum)} events</span><span>0</span></div>
    <svg viewBox="0 0 1000 115" preserveAspectRatio="none" role="img" aria-label="Recorded events in ${bucket/3600000}-hour intervals. Exact counts follow in the data table."><path d="M0 6H1000M0 59H1000M0 112H1000" class="receipt-grid"/>${bars.join('')}</svg>
    <div class="receipt-ticks">${ticks}</div></div>
    <p class="activity-caption">${esc(zoneLabel(start))} · Counts per ${count(bucket/3600000)}h interval. Empty intervals have no recorded receipts.${esc(coverage)}</p>
    <details class="receipt-values" data-key="activity-table-${hours}"><summary>View interval counts</summary><div class="table-scroll" tabindex="0" role="region" aria-label="Recorded activity interval counts"><table class="data-table"><thead><tr><th>Interval start</th><th>Decision changes</th>${scans.length ? '<th>Finished scans</th>' : ''}</tr></thead><tbody>${rows.reverse().join('')}</tbody></table></div></details>`);
}

export function groupReceipts(entries) {
  const groups = [], byVerdict = new Map();
  for (const event of entries) {
    if (event.kind !== 'decision' || !event.market_id || !/^(Watch|Pass)\./.test(event.message || '')) {
      groups.push([event]);
      continue;
    }
    const key = JSON.stringify([event.at,event.message]);
    if (!byVerdict.has(key)) {
      const group = [];
      byVerdict.set(key,group);
      groups.push(group);
    }
    byVerdict.get(key).push(event);
  }
  return groups;
}

export function receiptRows(entries, markets) {
  const market = event => markets.get(event.market_id)?.label || event.market_id;
  return groupReceipts(entries).map(group => {
    const e = group[0], name = market(e);
    const details = group.length === 1 ? esc(e.message || '—') :
      `<details data-key="receipts-${esc(e.id)}"><summary><strong>${count(group.length)} decisions</strong> · ${esc(e.message)}</summary><ul>${group.map(row =>
        `<li><strong>${esc(market(row))}</strong> <span class="row-meta">${esc(row.market_id)} · ${esc(row.message)}</span></li>`).join('')}</ul></details>`;
    return `<tr data-key="${esc(e.id ?? e.at)}"><td class="time-cell numeric">${esc(stamp(e.at))}</td><td>${esc(human(e.kind) || 'Worker event')}${group.length === 1 && name ? `<div class="row-meta">${esc(name)}</div>` : ''}</td><td>${details}</td></tr>`;
  });
}
