import {arr, button, clock, count, dash, day, emptyBox, esc, headline, icon, instant, num, price, scanButton, sourceLink, sourceUrl, stamp, zoneLabel} from './common.js';

export const quoteRanges = new Map([[15,'15 min'],[60,'1 hour'],[240,'4 hours'],[1440,'24 hours'],[2880,'48 hours'],[10080,'7 days']]);
export const defaultQuoteRange = 240;
const minute = 60_000;
const sourceLabel = {hour:'Kalshi hourly close', minute:'Kalshi minute close', poll:'Worker poll'};

const states = {
  live: ['Receiving public quotes', ''],
  waiting: ['Waiting for public quotes', 'The worker is waiting for its first response from Kalshi.'],
  disabled: ['Market capture is off', 'Authorize public Kalshi market capture in Worker to collect prices for this account.'],
  offline: ['Public feed offline', 'This worker was started in offline mode.'],
  empty: ['No open contracts', 'The last venue check found no open KXHIGHNY contracts.'],
  stale: ['Quotes delayed', 'The last successful venue response is outside the freshness window.'],
  error: ['Venue feed error', 'The latest request failed. The worker will check again.'],
  rate_limited: ['Venue rate limit', 'Kalshi has asked the worker to delay its next check.'],
  disconnected: ['Worker disconnected', 'The last received snapshot is shown below.'],
  unavailable: ['Quote feed unavailable', 'This worker has not supplied a public quote feed. Check the worker version in settings.'],
};

function quote(value) {
  const n = num(value);
  return n !== null && n > 0 && n < 100 ? n : null;
}
function sides(record) {
  const bid = quote(record?.bid_cents);
  const ask = quote(record?.ask_cents);
  return bid !== null && ask !== null && bid > ask ? {bid:null, ask:null} : {bid,ask};
}
const signedPrice = value => value === null ? dash : value === 0 ? '0¢' : `${value > 0 ? '+' : '−'}${price(Math.abs(value))}`;
const ms = value => instant(value)?.getTime() ?? null;
const isoClock = at => clock(new Date(at).toISOString());
const isoZone = at => zoneLabel(new Date(at).toISOString());
export function timeBounds(start, end) {
  const first = new Date(start).toISOString(), last = new Date(end).toISOString();
  const datesDiffer = day(first) !== day(last);
  const zonesDiffer = zoneLabel(first) !== zoneLabel(last);
  const left = `${datesDiffer ? `${day(first,true)} ` : ''}${clock(first)}${zonesDiffer ? ` ${zoneLabel(first)}` : ''}`;
  return `${left}–${datesDiffer ? `${day(last,true)} ` : ''}${clock(last)} ${zoneLabel(last)}`;
}

function observations(records, source) {
  const unique = new Map();
  for (const point of arr(records)) {
    const at = ms(point?.at);
    if (at !== null) unique.set(at, {at, source, ...sides(point)});
  }
  return [...unique.values()].sort((a,b) => a.at - b.at);
}
export function quoteStatus(state, connected) {
  if (!connected) return 'disconnected';
  if (state.health?.online === false || state.quotes?.status === 'offline') return 'offline';
  if (state.settings?.allow_market === false) return 'disabled';
  if (!state.quotes) return 'unavailable';
  return Object.hasOwn(states,state.quotes.status) ? state.quotes.status : 'unavailable';
}
export function quoteRows(state, connected) {
  const status = quoteStatus(state,connected);
  if (state.settings?.allow_market === false || status === 'disabled') return [];
  const cadence = (num(state.quotes?.interval_seconds) || 15) * 1000;
  const captured = ms(state.quotes?.last_success_at);
  return arr(state.quotes?.markets).filter(m => typeof m?.id === 'string' && m.id).map(m => {
    const history = observations(m.history,'poll');
    const latest = history.at(-1);
    const previous = history.at(-2);
    const at = ms(m.observed_at) ?? latest?.at ?? null;
    const recent = status === 'live' && at !== null && (captured === null || Math.abs(captured - at) <= 2 * cadence);
    const current = recent ? sides(m) : {bid:null,ask:null};
    const canCompare = latest && previous && latest.at === at && latest.at - previous.at <= 2 * cadence;
    const change = side => canCompare && current[side] !== null && current[side] === latest[side] && previous[side] !== null
      ? Number((current[side] - previous[side]).toFixed(4)) : null;
    return {...m,at,history,candles:observations(m.candles,'minute'),hourly:observations(m.hourly_candles,'hour'),
      ...current,bidChange:change('bid'),askChange:change('ask')};
  });
}

function rangeOf(points, side) {
  const values = points.map(p => p[side]).filter(v => v !== null);
  return values.length ? {low:Math.min(...values), high:Math.max(...values), count:values.length} : null;
}
function rangeText(range) {
  if (!range) return dash;
  return range.low === range.high ? price(range.low) : `${price(range.low).slice(0,-1)}–${price(range.high)}`;
}
function hasQuote(p) { return p.bid !== null || p.ask !== null; }

// Prefer finer observations inside continuous coverage. A coarser close can
// still document an outage or the opposite side of a one-sided book.
function uncoveredCloses(closes, finer, maximumGap) {
  const retained = [];
  let next = 0;
  for (const candle of closes) {
    while (next < finer.length && finer[next].at < candle.at) next++;
    const after = finer[next], before = finer[next-1];
    if (after?.at === candle.at) {
      const remaining = {...candle,bid:after.bid === null ? candle.bid : null,ask:after.ask === null ? candle.ask : null};
      if (hasQuote(remaining)) retained.push(remaining);
      continue;
    }
    const covered = before && after && after.at - before.at <= maximumGap &&
      ['bid','ask'].every(side => candle[side] === null || (before[side] !== null && after[side] !== null));
    if (!covered) retained.push(candle);
  }
  return retained;
}
export function quoteWindow(m, feed, minutes = defaultQuoteRange) {
  const gaps = {poll:(num(feed.interval_seconds) || 15) * 2000,minute:(num(feed.history_period_seconds) || 60) * 1000,
    hour:(num(feed.hourly_history_period_seconds) || 3600) * 1000};
  const captures = [feed.last_checked_at,feed.last_success_at,feed.history_last_checked_at,feed.history_last_success_at,
    feed.hourly_history_last_checked_at,feed.hourly_history_last_success_at].map(ms).filter(t => t !== null);
  const latest = captures.length ? captures : [m.at,m.history.at(-1)?.at,m.candles.at(-1)?.at,m.hourly.at(-1)?.at].filter(Number.isFinite);
  const end = latest.length ? Math.max(...latest) : null;
  const start = end === null ? null : end - minutes * minute;
  const listed = ms(m.open_at);
  const listedAt = listed !== null && end !== null && listed <= end ? listed : null;
  const available = p => end !== null && p.at <= end && (listedAt === null || p.at >= listedAt);
  const polls = m.history.filter(available), minutesRaw = m.candles.filter(available);
  const minuteCloses = uncoveredCloses(minutesRaw,polls,gaps.poll);
  const long = minutes > defaultQuoteRange;
  const hourlyCloses = long ? uncoveredCloses(uncoveredCloses(m.hourly.filter(available),minutesRaw,gaps.minute),polls,gaps.poll) : [];
  const points = [...hourlyCloses,...minuteCloses,...polls].filter(p => p.at >= start).sort((a,b) => a.at-b.at);
  const observed = points.filter(hasQuote);
  return {minutes,start,end,listedAt,long,points,observed,gaps,bid:rangeOf(points,'bid'),ask:rangeOf(points,'ask')};
}

function axis(values) {
  const low = Math.min(...values), high = Math.max(...values);
  const margin = Math.max(.5,(high-low)*.1);
  const bottom = Math.max(0,low-margin), top = Math.min(100,high+margin);
  const desired = (top-bottom)/4;
  const step = [.25,.5,1,2,5,10,20,25].find(n => n >= desired) || 25;
  const start = Math.max(0,Math.floor(bottom/step)*step);
  const end = Math.min(100,Math.ceil(top/step)*step);
  const ticks = [];
  for (let n=start; n<=end; n+=step) ticks.push(n);
  return {start,end,ticks};
}
function segments(window, valid) {
  const paths = [];
  let run = [];
  for (const p of window.points) {
    const previous = run.at(-1);
    if (!valid(p) || (previous && (p.source !== previous.source || p.at - previous.at > window.gaps[p.source]))) {
      if (run.length) paths.push(run);
      run = [];
    }
    if (valid(p)) run.push(p);
  }
  if (run.length) paths.push(run);
  return paths;
}
export function chartTimeAxis(window) {
  const week = window.minutes === 10080;
  const divisions = week ? 7 : window.long ? 4 : 2;
  const dates = window.long || day(window.start) !== day(window.end);
  const labels = Array.from({length:divisions+1},(_,i) => {
    const at = window.start + (window.end-window.start)*i/divisions;
    const edge = i === 0 ? 'first' : i === divisions ? 'last' : 'inside';
    const date = dates ? `<span>${esc(day(at,true))}</span>` : '';
    const time = week ? '' : `<span>${esc(clock(at).slice(0,5))}</span>`;
    return `<time class="${edge}${i === Math.floor(divisions/2) ? ' middle' : ''}" style="--tick:${100*i/divisions}%" datetime="${new Date(at).toISOString()}" title="${esc(stamp(at))}">${date}${time}</time>`;
  }).join('');
  return `<div class="quote-x-axis chart-time-axis${dates && !week ? ' date-and-time' : ''}" aria-hidden="true">${labels}</div>`;
}
function historyPlot(window) {
  if (window.end === null) return emptyBox('Waiting for the first timestamp','The worker will set the chart window after its first venue check.','markets');
  const {start:first,end:last} = window;
  const times = chartTimeAxis(window);
  const span = timeBounds(first,last);
  const values = window.observed.flatMap(p => [p.bid,p.ask]).filter(v => v !== null);
  if (!values.length) return `<div class="quote-plot" role="img" aria-label="${esc(`No recorded prices from ${span}.`)}"><p class="quote-chart-empty">No recorded prices in this window.</p>${times}</div>`;
  const {start,end,ticks} = axis(values);
  const X = 1000, Y = 200;
  const x = t => 3 + (X-6) * (t-first)/(last-first);
  const y = v => 4 + (Y-8)*(end-v)/(end-start);
  const draw = (side,run) => {
    if (run.length === 1) {
      const p = run[0];
      // A round-capped, short line stays circular under an uneven SVG aspect ratio.
      return `<path class="quote-point ${side}" data-source="${p.source}" d="M${x(p.at).toFixed(2)},${y(p[side]).toFixed(2)}h.01"/>`;
    }
    const d = run.map((p,i) => `${i ? 'L' : 'M'}${x(p.at).toFixed(2)},${y(p[side]).toFixed(2)}`).join(' ');
    return `<path class="quote-line ${side}" data-source="${run[0].source}" d="${d}"/>`;
  };
  const openX = window.listedAt > first ? x(window.listedAt) : 0;
  return `<div class="quote-plot" role="img" aria-label="${esc(`${span}. Observed YES bid range ${rangeText(window.bid)}, ask range ${rangeText(window.ask)}. Gaps show missing observations.`)}">
    <svg class="quote-y-axis" aria-hidden="true">${ticks.map(t => `<text x="95%" y="${(y(t)/Y*100).toFixed(2)}%" dy=".32em" text-anchor="end">${price(t)}</text>`).join('')}</svg>
    <div class="chart-drawing-box"><svg viewBox="0 0 ${X} ${Y}" preserveAspectRatio="none" class="quote-drawing" aria-hidden="true">
      ${openX ? `<rect class="chart-before-listing" width="${openX.toFixed(2)}" height="${Y}"/><line class="chart-listing-rule" x1="${openX.toFixed(2)}" x2="${openX.toFixed(2)}" y1="0" y2="${Y}"/>` : ''}
      ${ticks.map(t=>`<line class="quote-gridline" x1="0" x2="${X}" y1="${y(t).toFixed(2)}" y2="${y(t).toFixed(2)}"/>`).join('')}
      ${['bid','ask'].map(side => segments(window,p => p[side] !== null).map(run => draw(side,run)).join('')).join('')}</svg>
      ${openX > X*.4 ? '<span class="chart-listing-label" aria-hidden="true">Before listing</span>' : ''}</div>${times}</div>`;
}
const historyStates = {
  loading:name => `Loading Kalshi ${name} closes.`, waiting:name => `Waiting for the first Kalshi ${name} history request.`,
  empty:name => `Kalshi returned no ${name} closes.`, partial:name => `Some Kalshi ${name} history is unavailable.`,
  error:name => `Kalshi ${name} history could not be refreshed.`, rate_limited:name => `Kalshi has delayed the next ${name} history request.`,
  offline:name => `The ${name} history feed is offline.`, disabled:name => `The ${name} history feed is off.`,
};
function historyNotice(feed, hourly) {
  const prefix = hourly ? 'hourly_history' : 'history', status = feed[`${prefix}_status`];
  if (status === 'ready') return '';
  const name = hourly ? 'hourly' : 'minute';
  return feed[`${prefix}_message`] || (Object.hasOwn(historyStates,status) ? historyStates[status](name) : `Historical ${name} closes are unavailable from this worker.`);
}
function historyCoverage(window) {
  const first = window.observed[0], last = window.observed.at(-1);
  const listedDuring = window.listedAt !== null && window.listedAt > window.start;
  const notes = listedDuring ? [`Listed ${stamp(window.listedAt)}.`] : [];
  if (first && first.at === last.at) notes.push(`One observation at ${isoClock(first.at)} ${isoZone(first.at)}.`);
  else if (first) {
    const earliest = listedDuring ? window.listedAt : window.start;
    if (first.at - earliest > window.gaps[first.source] || window.end - last.at > window.gaps[last.source]) {
      notes.push(`Available observations span ${timeBounds(first.at,last.at)}.`);
    }
  }
  return notes.join(' ');
}
function historySection(feed, window) {
  const recent = window.points.slice(-12).reverse();
  const firstRecent = recent.at(-1)?.at;
  const olderHours = window.long ? window.observed.filter(p => p.source === 'hour' && p.at < firstRecent).reverse() : [];
  const records = [...recent,...olderHours];
  const controls = `<div class="quote-window" role="group" aria-label="History window">${[...quoteRanges].map(([minutes,label]) =>
    `<button type="button" data-action="quote-range" data-value="${minutes}" aria-pressed="${window.minutes === minutes}">${label}</button>`).join('')}</div>`;
  const counts = {hour:0,minute:0,poll:0};
  for (const p of window.observed) counts[p.source]++;
  const sources = (window.long ? ['hour','minute','poll'] : ['minute','poll']).map(source => {
    const n = counts[source], label = source === 'poll' ? 'worker poll' : sourceLabel[source];
    return `<span>${count(n)} ${label}${n === 1 ? '' : 's'}</span>`;
  }).join('<i aria-hidden="true">·</i>');
  const notes = [historyCoverage(window),historyNotice(feed,false),window.long && historyNotice(feed,true)].filter(Boolean);
  const zone = window.end === null ? zoneLabel() : isoZone(window.end);
  const rangeStat = (label,r) => `<div><dt><i class="chart-series-${label}" aria-hidden="true"></i>YES ${label} range</dt><dd>${esc(rangeText(r))}${r?.count === 1 ? '<small>One quote</small>' : ''}</dd></div>`;
  return `<section class="quote-history market-chart" aria-labelledby="price-history-title"><div class="chart-heading"><h3 id="price-history-title">Price history</h3>${controls}</div>
    ${window.end === null ? '' : `<p class="history-window-bounds"><strong>${quoteRanges.get(window.minutes)}</strong> <span>·</span> ${esc(timeBounds(window.start,window.end))}</p>`}
    <div class="chart-readings"><dl class="history-ranges">${rangeStat('bid',window.bid)}${rangeStat('ask',window.ask)}</dl><span class="chart-unit-label">Cents per contract · ${esc(zone)}</span></div>
    ${historyPlot(window)}
    <div class="history-sources" aria-label="Sources in this window">${sources}</div>
    ${notes.length ? `<p class="history-note">${esc(notes.join(' '))}</p>` : ''}
    ${records.length ? `<details class="quote-observations" data-key="quote-observations"><summary>${olderHours.length ? 'Recent observations and hourly closes' : 'Recent observations'} (${records.length})</summary><div class="table-scroll" tabindex="0" role="region" aria-label="Recorded bid and ask observations"><table class="data-table"><thead><tr><th scope="col">Time</th><th scope="col">Source</th><th scope="col">YES bid</th><th scope="col">YES ask</th></tr></thead><tbody>${records.map(p =>
      `<tr data-key="${p.at}-${p.source}"><td class="numeric">${esc(window.long ? `${day(p.at,true)} · ` : '')}${esc(isoClock(p.at))} ${esc(isoZone(p.at))}</td><td>${sourceLabel[p.source]}</td><td class="numeric">${price(p.bid)}</td><td class="numeric">${price(p.ask)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    </section>`;
}
function marketList(rows, selected, windows, minutes) {
  const dates = new Map();
  for (const m of rows) {
    const date = m.event_date || '';
    if (!dates.has(date)) dates.set(date,[]);
    dates.get(date).push(m);
  }
  // Venue tickers sort the two open tails after the bounded temperature bands.
  const firstDegree = label => {
    const match = /^(-?\d+(?:\.\d+)?)(?:\s*°|\s+to\s+)/i.exec(String(label || '').replace('−','-'));
    return match ? Number(match[1]) : Infinity;
  };
  for (const readings of dates.values()) readings.sort((a,b) => firstDegree(a.label) - firstDegree(b.label));
  return `<section class="panel market-list" aria-labelledby="quote-list-title"><div class="panel-head"><h2 id="quote-list-title">Contracts <span class="counter">${rows.length}</span></h2>
    <span class="small-note">Central Park daily high</span></div>
    <div class="market-list-scroll" role="region" aria-label="Public contract quotes" tabindex="0"><table class="quote-table"><thead><tr>
      <th scope="col">Contract</th><th scope="col">YES bid</th><th scope="col">YES ask</th><th scope="col" aria-label="Observed YES bid range over ${quoteRanges.get(minutes)}">${minutes === 10080 ? '7d' : minutes < 60 ? `${minutes}m` : `${minutes/60}h`} bid range</th></tr></thead>
    ${[...dates.keys()].sort().map(date => `<tbody data-key="${esc(date)}"><tr class="quote-group"><th colspan="4" scope="rowgroup">${date ? esc(day(date)) : 'Event date unavailable'}</th></tr>
      ${dates.get(date).map(m=>`<tr data-key="${esc(m.id)}" data-quote-market="${esc(m.id)}" class="quote-row${m.id === selected ? ' selected' : ''}">
        <th scope="row"><button type="button" class="quote-link" data-action="select-quote" data-value="${esc(m.id)}" aria-pressed="${m.id === selected}">${esc(m.label || m.id)}</button></th>
        <td class="numeric">${price(m.bid)}</td><td class="numeric">${price(m.ask)}</td><td class="numeric">${rangeText(windows.get(m.id).bid)}</td></tr>`).join('')}</tbody>`).join('')}
    </table></div></section>`;
}
function quoteDetail(m, state, status, window) {
  if (!m) return '';
  const canResearch = state.mode === 'forward' && state.markets.some(r=>r.id === m.id);
  const sidesAvailable = Number(m.bid !== null) + Number(m.ask !== null);
  const bookState = status !== 'live' ? 'Current prices unavailable' : sidesAvailable === 2 ? 'Two-sided book' : sidesAvailable === 1 ? 'One-sided book' : 'No posted prices';
  const tradingCutoff = ms(m.last_trading_at);
  const workerTime = ms(state.health?.heartbeat) ?? ms(state.quotes?.last_checked_at);
  const tradingClosed = tradingCutoff !== null && workerTime !== null && tradingCutoff <= workerTime;
  const priceField = (name,value,change) => `<div><dt>Best YES ${name}</dt><dd>${price(value)}</dd>
    <dd class="quote-change">${change === null ? (value === null ? `No current ${name}` : 'No comparable prior poll') : change === 0 ? 'Unchanged since prior poll' : `${signedPrice(change)} from prior poll`}</dd></div>`;
  return `<article class="panel market-detail" data-key="quote-detail" aria-labelledby="quote-title">
    <div class="market-detail-head">${button('Back to market list','quote-list',{symbol:'back',kind:'inline',extraClass:'back-market-list'})}
      <div class="detail-top"><div class="instrument-id"><span class="instrument-glyph">${icon('markets',18)}</span><span>${esc(m.id)}</span></div>
        ${canResearch ? button('Open research','quote-research',{value:m.id,symbol:'file',kind:'primary'}) : ''}</div>
      <h2 id="quote-title" tabindex="-1">${esc(m.label || m.id)}</h2><p class="quote-contract-meta">Central Park daily high <span>·</span> ${esc(day(m.event_date))}${tradingClosed ? ' <span class="warning-label">Trading window closed</span>' : ''}</p></div>
    <div class="quote-body"><div class="section-line"><h3>Latest posted quote</h3><span>${esc(bookState)}</span></div>
      <dl class="quote-metrics">${priceField('bid',m.bid,m.bidChange)}${priceField('ask',m.ask,m.askChange)}
        <div><dt>Bid-ask spread</dt><dd>${m.bid === null || m.ask === null ? dash : price(Number((m.ask-m.bid).toFixed(4)))}</dd><dd class="quote-change">Per YES contract</dd></div></dl>
      <p class="quote-as-of">${icon('clock',14)} Quote received ${esc(stamp(m.at === null ? null : new Date(m.at).toISOString()))}</p>
      ${historySection(state.quotes,window)}
      ${m.last_trading_at || m.close_at ? `<dl class="quote-cutoffs">${m.last_trading_at ? `<div><dt>Last trading time</dt><dd>${esc(stamp(m.last_trading_at))}</dd></div>` : ''}${m.close_at ? `<div><dt>Venue listing close</dt><dd>${esc(stamp(m.close_at))}</dd></div>` : ''}</dl>` : ''}
    </div></article>`;
}
export function paperSwitch(state, ui) {
  const a = state?.paper_automation;
  const known = ui.connected && typeof a?.enabled === 'boolean';
  const on = known && a.enabled;
  const risk = known && !!a.risk_pause_reason;
  const disabled = !known || !a.available || risk || ui.locked;
  const hint = !ui.connected ? 'Connect to the worker to check its forward paper account.' : !known ? 'Waiting for the forward paper account.' :
    risk ? a.risk_pause_reason : !a.available ? a.reason || 'Forward paper trading is unavailable.' : ui.paperSwitchPending ? 'Waiting for the worker to confirm.' :
    state.mode === 'replay' ? 'Controls the forward paper account.' : '';
  return `<div class="paper-switch-wrap" data-key="paper-switch-wrap">
    <button type="button" class="paper-switch ${known ? on ? 'is-on' : 'is-off' : 'unknown'}" data-action="paper-switch" data-value="${on ? 'pause' : 'resume'}"
      ${known ? `role="switch" aria-checked="${on}" aria-label="Paper auto trading"` : ''}${hint ? ' aria-describedby="paper-switch-hint"' : ''}${disabled ? ' disabled' : ''}>
      <i class="paper-lamp" aria-hidden="true"></i><span>Paper auto trading</span><b${known ? ' aria-hidden="true"' : ''}>${known ? on ? 'Yes' : 'No' : 'Unknown'}</b></button>
    ${hint ? `<div class="paper-switch-hint" id="paper-switch-hint">${esc(hint)}${risk ? ` ${button('Review risk in Worker','market-settings',{kind:'inline'})}` : ''}</div>` : ''}</div>`;
}
export function marketsView(state, ui, rows) {
  const status = quoteStatus(state,ui.connected);
  const feed = state.quotes || {};
  const [label,defaultMessage] = states[status];
  const minutes = quoteRanges.has(ui.quoteRange) ? ui.quoteRange : defaultQuoteRange;
  const windows = new Map(rows.map(m => [m.id,quoteWindow(m,feed,minutes)]));
  const header = headline('Markets','Kalshi <span class="context-divider">/</span> <span class="series-code">KXHIGHNY</span> <span class="context-divider">/</span> Public YES quotes',
    paperSwitch(state,ui) + scanButton(state,ui));
  const marked = ['stale','rate_limited','error','offline','disconnected'].includes(status) ? 'late' : status === 'live' ? 'connected' : '';
  const band = `<div class="feed-band quote-feed-band"><div><i class="state-symbol ${marked}" aria-hidden="true"></i><strong>${esc(label)}</strong>
      ${num(feed.interval_seconds) ? `<span class="feed-sep"></span>${count(feed.interval_seconds)} s polling interval` : ''}</div>
      <div class="scan-time">${icon('clock',15)}${feed.last_success_at ? `Last venue response received <time datetime="${esc(feed.last_success_at)}">${esc(stamp(feed.last_success_at))}</time>` : 'Awaiting a venue response'}</div></div>`;
  const message = (status === 'disabled' || status === 'disconnected' ? '' : feed.message) || defaultMessage;
  const messages = `${state.mode === 'replay' && rows.length ? '<p class="quote-status-note">These prices come from the public venue. Research and Portfolio currently show the replay account.</p>' : ''}
    ${status !== 'live' && rows.length ? `<p class="quote-status-note">${esc(message)}</p>` : ''}`;
  const active = rows.find(m=>m.id === ui.quoteSelected);
  const content = rows.length ? `<div class="markets-grid${ui.quoteMobileDetail ? ' show-quote-detail' : ''}">${marketList(rows,ui.quoteSelected,windows,minutes)}${quoteDetail(active,state,status,windows.get(ui.quoteSelected))}</div>` :
    `<section class="panel quote-empty">${emptyBox(label,message || 'Waiting for a public venue observation.','markets')}</section>`;
  const source = state.settings?.allow_market && sourceUrl(feed.source_url) ? sourceLink('Kalshi public feed',feed.source_url) : '';
  const historySource = state.settings?.allow_market && sourceUrl(feed.history_source_url) ? sourceLink('Kalshi minute history',feed.history_source_url) : '';
  const hourlySource = minutes > defaultQuoteRange && state.settings?.allow_market && sourceUrl(feed.hourly_history_source_url) ? sourceLink('Kalshi hourly history',feed.hourly_history_source_url) : '';
  const checked = feed.last_checked_at ? `Last checked ${esc(stamp(feed.last_checked_at))}` : '';
  const links = [button('Worker settings','market-settings',{kind:'inline',symbol:'control'}),source,historySource,hourlySource,checked].filter(Boolean);
  return `${header}${band}${messages}${content}<div class="quote-last-check">${links.join('<span aria-hidden="true">·</span>')}</div>`;
}
