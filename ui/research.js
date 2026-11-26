import {age, arr, badge, button, clock, count, dash, day, emptyBox, esc, field, headline, human, icon, instant, money, notesFor, num, percent, price, probability, sideName, sourceLink, scanButton, stamp, zoneLabel, text, tone} from './common.js';

import {paperSwitch} from './markets.js';

export const decisions = ['Paper buy', 'Watch', 'Pass', 'Held', 'Settled'];
export const detailTabs = ['Decision', 'Evidence', 'Order book', 'Settlement'];

export function recentEntries(activity) {
  return arr(activity).filter(Boolean).slice().sort((a, b) =>
    (instant(b.at)?.getTime() || 0) - (instant(a.at)?.getTime() || 0));
}
function activityPreview(state) {
  const entries = recentEntries(state.activity).slice(0, 3);
  return `<section class="recent panel" aria-labelledby="recent-title" data-key="recent">
    <div class="panel-head"><h2 id="recent-title">Recent activity</h2>
      ${button('View log', 'view', {value: 'Activity', kind: 'inline'})}</div>
    ${entries.length ? `<ol class="mini-log">${entries.map(e => `<li data-key="${esc(e.id ?? e.at)}">
      <span class="log-glyph">${icon('activity', 16)}</span><div><b>${esc(human(e.kind) || 'Worker event')}</b><p>${esc(e.message)}</p></div>
      <time title="${esc(stamp(e.at))}">${esc(clock(e.at).slice(0, 5))}</time></li>`).join('')}</ol>` :
    `<p class="compact-empty">The worker has recorded no activity for this mode.</p>`}
    </section>`;
}
function replayComparison(state) {
  const replay = state.health?.replay_result;
  const snapshots = arr(replay?.comparison);
  const market = state.markets.find(m => m.id === replay?.case_id);
  if (!market || !snapshots.length) return activityPreview(state);
  return `<section class="panel replay-panel" aria-labelledby="replay-title"><div class="panel-head"><h2 id="replay-title">Price change replay</h2>
    ${button('Open case','case',{value:market.id,kind:'inline'})}</div><div class="replay-intro"><span class="log-glyph">${icon('layers',16)}</span>
    <div><b>${esc(market.label || market.id)}</b><p>${esc(day(market.event_date))} · Model forecast ${percent(replay.forecast_probability)}</p></div></div>
    <div class="comparison-scroll" tabindex="0" role="region" aria-label="Replay quote comparison"><table class="replay-table data-table"><thead><tr><th scope="col">Book (${esc(zoneLabel(snapshots[0]?.at))})</th><th scope="col">Avg. ask</th><th scope="col">Net at size</th><th scope="col">Decision</th></tr></thead>
    <tbody>${snapshots.map(s => `<tr><td class="numeric">${esc(clock(s.at).slice(0,5))}</td><td class="numeric">${price(s.avg_ask_cents)}</td>
      <td class="numeric ${tone(s.net_cents)}">${money(s.net_cents,true)}</td><td>${badge(s.decision)}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function contractList(state, ui, rows) {
  const table = rows.map(m => {
    const selected = m.id === ui.selected;
    const side = sideName(m.side);
    const date = day(m.event_date, true);
    const decisionTime = m.decision_as_of || m.as_of;
    const decisionDay = day(decisionTime,true);
    const askAndSize = num(m.avg_ask_cents) === null ? '' : `${side ? `${side} ` : ''}${price(m.avg_ask_cents)}${num(m.qty) !== null ? ` × ${count(m.qty)}` : ''}`;
    return `<tr data-key="${esc(m.id)}" class="contract-row${selected ? ' selected' : ''}" data-market="${esc(m.id)}">
      <td><button class="contract-link" type="button" data-action="select" data-value="${esc(m.id)}" aria-pressed="${selected}">
        <span class="contract-name">${esc(m.label || m.title || m.id)}</span></button>
        <div class="row-meta">${esc(date)}${date !== dash && decisionTime ? ' · ' : ''}${decisionTime ? `<time title="Decision ${esc(stamp(decisionTime))}">${decisionDay !== date ? `as of ${esc(decisionDay)} ` : ''}${esc(clock(decisionTime).slice(0, 5))} ${esc(zoneLabel(decisionTime))}</time>` : ''}</div></td>
      <td class="num-col"><b class="numeric">${esc(money(m.net_cents, true))}</b><div class="row-meta numeric">${esc(askAndSize || 'Unpriced')}</div></td>
      <td class="decision-col">${badge(m.decision)}</td></tr>`;
  }).join('');
  return `<section class="panel contract-panel" aria-labelledby="contracts-title" data-key="contracts">
    <div class="panel-head"><h2 id="contracts-title">Contracts <span class="counter">${rows.length}</span></h2>
      <div class="filter">${icon('filter', 15)}<select aria-label="Filter contracts by decision" id="decision-filter">${['All decisions', ...decisions].map(d =>
        `<option${d === ui.filter ? ' selected' : ''}>${esc(d)}</option>`).join('')}</select></div></div>
    ${rows.length ? `<div class="contract-table-wrap" tabindex="0" role="region" aria-label="Ranked contracts"><table class="contracts"><thead><tr><th scope="col">Contract</th>
      <th scope="col" class="num-col">Net / ask × size</th><th scope="col" class="decision-col">Decision</th></tr></thead><tbody>${table}</tbody></table></div>` :
      emptyBox(ui.filter === 'All decisions' ? 'No contracts in this feed' : 'No matching contracts',
        ui.filter === 'All decisions' ? (state.mode === 'forward' ? 'Run a scan to check for eligible Central Park contracts. Feed permissions are in Worker.' : 'The replay has no contracts at this step.') : 'Choose another decision filter.')}
    </section>`;
}
function plotRow(label, raw, type, low, high) {
  const p = probability(raw);
  const lo = probability(low), hi = probability(high);
  const hasRange = lo != null && hi != null && lo <= hi;
  const band = hasRange ? `<line class="range" x1="${lo * 100}%" x2="${hi * 100}%" y1="9" y2="9"/>
    <line class="cap" x1="${lo * 100}%" x2="${lo * 100}%" y1="3" y2="15"/><line class="cap" x1="${hi * 100}%" x2="${hi * 100}%" y1="3" y2="15"/>` : '';
  return `<div class="plot-row ${type}"><span class="plot-name">${esc(label)}</span><svg class="plot-track" width="100%" height="18" aria-hidden="true">
    ${[0,25,50,75,100].map(x => `<line class="plot-grid" x1="${x}%" x2="${x}%" y1="0" y2="18"/>`).join('')}
    ${p == null ? '' : `<line class="plot-fill" x1="0" x2="${p * 100}%" y1="9" y2="9"/>${band}<circle class="plot-point" cx="${p * 100}%" cy="9" r="4"/>`}
    </svg><span class="plot-value numeric">${percent(p)}</span></div>`;
}
function forecastBlock(m) {
  const f = m.forecast || {};
  const low = probability(f.lower), high = probability(f.upper);
  const rangeText = low != null && high != null && low <= high ? `${percent(low)}–${percent(high)} calibrated range` : 'Calibration range unavailable';
  return `<section class="forecast-block" aria-labelledby="forecast-title"><div class="section-line">
    <h3 id="forecast-title">YES probability</h3><span class="range-label">${esc(rangeText)}</span></div>
    <div class="plot" role="group" aria-label="YES probability comparison">
    ${plotRow('Station model', f.probability, 'model', low, high)}
    ${plotRow('Market midpoint', m.market_baseline, 'market')}
    ${plotRow('Climatology', f.baseline, 'climate')}
    <div class="plot-axis" aria-hidden="true"><span></span><div><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span></div><span></span></div>
    </div></section>`;
}
function decisionView(m) {
  const f = m.forecast || {};
  const side = sideName(m.side);
  const summaryMetrics = [
    [m.settlement ? 'Conservative net at entry' : 'Conservative net', money(m.net_cents, true), tone(m.net_cents)],
    [`${side ? side + ' ' : ''}average ask`, price(m.avg_ask_cents), ''],
    ['Priced quantity', count(m.qty), ''],
  ];
  const size = num(m.available_qty) != null ? `${count(m.available_qty)} contracts` : dash;
  return `<div class="detail-content" data-key="decision">
    <div class="verdict"><div class="section-line"><h3>Worker decision</h3>${badge(m.decision)}</div>
      <p>${esc(m.reason || 'The worker has not recorded a decision reason.')}</p></div>
    <dl class="metrics-strip">${summaryMetrics.map(([k,v,c]) => `<div><dt>${esc(k)}</dt><dd class="${c}">${esc(v)}</dd></div>`).join('')}</dl>
    ${forecastBlock(m)}
    <details data-key="calculation"><summary>Price and model details</summary><dl class="field-list">
      ${field('Model expected net', money(m.model_net_cents, true))}
      ${field('Purchase premium', money(m.premium_cents))}
      ${field('Exchange fees at size', money(m.fee_cents))}
      ${field('Execution allowance at size', money(m.allowance_cents))}
      ${field('Total cost with allowance', money(m.cost_cents))}
      ${field('Worst acceptable ask', price(m.worst_acceptable_ask_cents))}
      ${field('Break-even probability', percent(m.break_even_probability))}
      ${field('Conservative side probability', percent(m.conservative_probability))}
      ${field('Available size', size)}
      ${field('Forecast model', f.model || dash)}
      ${field('Calibration observations', count(f.samples))}
      ${field('Forecast issued', stamp(f.issued_at))}
      ${field('Decision as of', stamp(m.decision_as_of || m.as_of))}
    </dl></details></div>`;
}
function evidenceView(m, state, ui) {
  const records = arr(m.evidence).filter(Boolean).slice().sort((a, b) => {
    const aDate = instant(a.available_at) || instant(a.published_at);
    const bDate = instant(b.available_at) || instant(b.published_at);
    return (aDate?.getTime() || 0) - (bDate?.getTime() || 0);
  });
  const notes = notesFor(state, m.id);
  const configured = arr(state.research?.configured);
  return `<div class="detail-content" data-key="evidence"><div class="section-line"><h3>Evidence timeline</h3>
    <span>${records.length} ${records.length === 1 ? 'record' : 'records'}</span></div>
    ${records.length ? `<ol class="timeline">${records.map((e, i) => {
      const asOf = instant(m.decision_as_of || m.as_of);
      const after = asOf && [e.published_at,e.available_at,e.retrieved_at].some(t => instant(t) && instant(t) > asOf);
      return `<li data-key="${esc(`${e.id || e.url || e.title}/${e.published_at}/${i}`)}"><i aria-hidden="true"></i>
        <div class="eyeline">${esc(stamp(e.available_at || e.published_at))}${after ? '<span class="warning-label">After decision</span>' : ''}</div>
        <h4>${sourceLink(e.title || e.kind || 'Source record', e.url)}</h4>
        ${e.detail ? `<p>${esc(e.detail)}</p>` : ''}<dl class="source-dates">
        ${field('Published', stamp(e.published_at))}${field('First available', stamp(e.available_at))}${field('Retrieved', stamp(e.retrieved_at))}</dl>
        ${e.url && !/^https?:/i.test(e.url) ? `<p class="source-id">${esc(e.url)}</p>` : ''}</li>`;
    }).join('')}</ol>` : emptyBox('No source records yet', 'Recorded forecasts and bulletins will appear here.', 'file')}
    <div class="section-line analyst-heading"><h3>Analyst notes</h3>${button(state.health?.research_running ? 'Researching…' : 'Run research', 'research', {value: m.id, symbol: 'file', disabled: ui.locked || state.health?.research_running})}</div>
    <p class="secondary-copy">${configured.length ? `Enabled analysts: ${esc(configured.join(', '))}. Research uses the worker’s daily budget.` : 'The worker can summarize the local evidence packet. External analysts can be configured in Worker.'}</p>
    ${notes.map((n,i) => `<article class="analyst-note" data-key="${esc(n.id || `${n.provider}/${n.at}/${i}`)}"><div class="section-line"><b>${esc(n.provider || 'Worker')}${n.status ? ` · ${esc(human(n.status))}` : ''}</b><time>${esc(stamp(n.at))}</time></div>
      <p>${esc(n.text || n.status || '')}</p>${arr(n.sources).length ? `<ul>${n.sources.map(s => `<li>${sourceLink(s.title || s.url || 'Source', s.url)}</li>`).join('')}</ul>` : ''}</article>`).join('')}
    </div>`;
}
function orderBookView(m) {
  const sides = [['yes','YES asks'],['no','NO asks']];
  return `<div class="detail-content" data-key="book"><div class="section-line"><h3>Executable asks</h3><span>Price per contract</span></div>
    <div class="book-grid">${sides.map(([side,label]) => {
      const levels = arr(m.levels?.[side]).filter(level => Array.isArray(level) && num(level[0]) != null && num(level[1]) != null);
      return `<section><h4>${label}</h4>${levels.length ? `<table class="data-table"><thead><tr><th scope="col">Ask</th><th scope="col" class="align-right">Size</th></tr></thead><tbody>${levels.map(([p,q]) =>
        `<tr><td class="numeric">${price(p)}</td><td class="align-right numeric">${count(q)}</td></tr>`).join('')}</tbody></table>` : '<p class="compact-empty">No executable levels.</p>'}</section>`;
    }).join('')}</div>
    <dl class="field-list">${field('Book source', m.book_source || dash)}${field('Quote observed', stamp(m.observed_at))}
      ${field('Quote age at account time', age(m.quote_age_seconds))}${field('Snapshot as of', stamp(m.as_of))}</dl></div>`;
}
function settlementView(m) {
  const s = m.settlement;
  return `<div class="detail-content" data-key="settlement"><h3>Venue determination</h3>
    ${s ? `<dl class="field-list settlement-fields">${field('Outcome',text(s.result).toUpperCase() || dash)}${field('Status',human(s.status) || dash)}
      ${field('Observed',stamp(s.observed_at))}${field('Authority record',s.source || dash)}${field('Paper payout',money(s.payout_cents))}${field('Realized P&L',money(s.realized_cents,true))}</dl>` :
      '<p class="settlement-wait">The worker is awaiting a venue determination.</p>'}
    <h3>Settlement terms</h3>
    <p class="rule-text">${esc(text(m.rules) || 'The worker has not verified the settlement terms.')}</p>
    <dl class="field-list">${field('Contract', m.id)}${field('Event', m.event_id || dash)}
    ${field('Station', m.station || dash)}${field('Local event date', day(m.event_date))}
    ${field('Closes', stamp(m.close_at))}${field('Rule version', m.rule_version || dash)}</dl></div>`;
}
function detailPanel(m, state, ui) {
  if (!m) return `<section class="panel detail-panel">${emptyBox('No contract selected', 'Select an available contract to inspect its decision, book and dated evidence.')}</section>`;
  const tab = ui.tab;
  const body = tab === 'Evidence' ? evidenceView(m,state,ui) : tab === 'Order book' ? orderBookView(m) : tab === 'Settlement' ? settlementView(m) : decisionView(m);
  return `<article class="panel detail-panel" data-key="detail" aria-labelledby="contract-title">
    <div class="detail-head">
      ${button('Back to contracts', 'list', {symbol: 'back', kind: 'inline', extraClass: 'back-list'})}
      <div class="detail-top"><div class="instrument-id"><span class="instrument-glyph">${icon('weather',18)}</span><span>${esc(m.id)}</span></div>
        ${tab === 'Decision' ? button('Review evidence','evidence',{symbol:'file',kind:'primary'}) : ''}</div>
      <h2 id="contract-title" tabindex="-1">${esc(m.title || m.label || m.id)}</h2>
      <div class="instrument-meta"><span>${icon('pin',14)}${esc(m.station || 'Station pending')}</span>
      <span>${esc(day(m.event_date))}</span>${m.label && !text(m.title).includes(m.label) ? `<span>${esc(m.label)}</span>` : ''}</div>
    </div><div class="detail-tabs" role="tablist" aria-label="Contract details">${detailTabs.map((t,i) =>
      `<button role="tab" id="tab-${i}" type="button" data-action="tab" data-value="${esc(t)}" aria-controls="detail-body" aria-selected="${t === tab}" tabindex="${t === tab ? 0 : -1}">${esc(t)}${t === 'Evidence' ? ` <span>${arr(m.evidence).length}</span>` : ''}</button>`).join('')}</div>
    <div id="detail-body" role="tabpanel" aria-labelledby="tab-${detailTabs.indexOf(tab)}" tabindex="0">${body}</div></article>`;
}
export function researchView(state, ui, rows, selected) {
  const replay = state.mode === 'replay';
  const scan = replay ? button('Run replay','replay',{symbol:'next',disabled:ui.locked}) : scanButton(state,ui);
  const pause = paperSwitch(state,ui);
  const recordedAt = replay ? (state.clock || state.health?.replay_result?.as_of) : state.health?.last_scan;
  return `${headline('Research inbox', 'Central Park daily high <span class="context-divider">/</span> Kalshi <span class="context-divider">/</span> <span class="series-code">KXHIGHNY</span>', pause + scan)}
    <div class="feed-band" data-key="feed-band"><div><span class="feed-dot ${replay ? 'replay' : 'forward'}" aria-hidden="true"></span>
      <strong>${replay ? 'Offline replay' : 'Forward paper'}</strong><span class="feed-sep"></span>${replay ? 'Synthetic market data' : state.health?.online === false ? 'Worker started offline' : state.settings?.allow_market ? 'Market feed permitted' : 'Market feed permission off'}</div>
      <div class="scan-time">${icon('clock', 15)}${recordedAt ? `${replay ? 'Replay clock' : 'Last scan'} <time title="${esc(stamp(recordedAt))}">${esc(day(recordedAt,true))} · ${esc(clock(recordedAt))} ${esc(zoneLabel(recordedAt))}</time>` : 'No scan recorded'}</div></div>
    <div class="inbox-grid${ui.mobileDetail ? ' show-detail' : ''}"><div class="inbox-left">${contractList(state,ui,rows)}${replay ? replayComparison(state) : activityPreview(state)}</div>
      ${detailPanel(selected,state,ui)}</div>`;
}
