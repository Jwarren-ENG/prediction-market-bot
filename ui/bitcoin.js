import {arr, button, clock, count, dash, day, esc, field, headline, icon, instant, money, num, price, sourceLink, stamp, zoneLabel} from './common.js';
import {chartTimeAxis, timeBounds} from './markets.js';

export const bitcoinRanges = new Map([[1440,'24 hours'],[2880,'48 hours'],[10080,'7 days']]);
const hour = 3_600_000;
const usd = new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2});
const tickUsd = new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:0,maximumFractionDigits:2});
const units = new Intl.NumberFormat('en-US',{minimumFractionDigits:0,maximumFractionDigits:8});
const positive = value => { const n = num(value); return n !== null && n > 0 ? n : null; };
const dollars = value => positive(value) === null ? dash : usd.format(Number(value));
const btc = value => num(value) === null || Number(value) < 0 ? dash : `${units.format(Number(value))} BTC`;
const percentage = value => num(value) === null ? dash : `${Number(value) > 0 ? '+' : ''}${count(value)}%`;
const atMs = value => instant(value)?.getTime() ?? null;

export function bitcoinCountdown(value, now = Date.now()) {
  const expires = atMs(value);
  if (expires === null || !Number.isFinite(now)) return dash;
  const seconds = Math.ceil((expires-now)/1000);
  if (seconds <= 0) return 'Closed';
  return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
}

export function bitcoinWindow(market, minutes) {
  const selected = bitcoinRanges.has(minutes) ? minutes : 1440;
  const closes = new Map();
  for (const p of arr(market.hourly)) {
    const at = atMs(p?.at);
    if (at !== null && at % hour === 0) closes.set(at,{at,close:positive(p?.close_usd)});
  }
  const receipts = [market.last_checked_at,market.quote?.observed_at].map(atMs).filter(t => t !== null);
  const end = receipts.length ? Math.max(...receipts) : closes.size ? Math.max(...closes.keys()) : null;
  const start = end === null ? null : end - selected*60_000;
  const points = [...closes.values()].filter(p => p.at >= start && p.at <= end).sort((a,b)=>a.at-b.at);
  const observed = points.filter(p => p.close !== null);
  return {minutes:selected,start,end,long:true,points,observed};
}
function priceScale(values) {
  const low = Math.min(...values), high = Math.max(...values);
  const pad = Math.max(.01,(high-low)*.07,low === high ? low*.0005 : 0);
  const raw = (high-low+2*pad)/4;
  const base = 10**Math.floor(Math.log10(raw));
  const step = Math.max(.01,([1,2,2.5,5,10].find(n => n*base >= raw) || 10)*base);
  const min = Math.max(0,Math.floor((low-pad)/step)*step);
  const max = Math.ceil((high+pad)/step)*step;
  const ticks = Array.from({length:Math.round((max-min)/step)+1},(_,i)=>min+i*step);
  return {min,max,ticks};
}
function observedLines(points, key, style, x, y, maxGap) {
  const runs = [];
  let run = [];
  for (const p of points) {
    if (p[key] === null || (run.length && p.at-run.at(-1).at > maxGap)) {
      if (run.length) runs.push(run);
      run = [];
    }
    if (p[key] !== null) run.push(p);
  }
  if (run.length) runs.push(run);
  return runs.map(segment => {
    if (segment.length === 1) return `<path class="quote-point ${style}" d="M${x(segment[0].at)},${y(segment[0][key]).toFixed(2)}h.01"/>`;
    return `<path class="quote-line ${style}" d="${segment.map((p,i)=>`${i ? 'L' : 'M'}${x(p.at)},${y(p[key]).toFixed(2)}`).join(' ')}"/>`;
  }).join('');
}
function closeChart(window) {
  if (window.end === null) return '<p class="btc-empty">Waiting for the first Bitstamp capture.</p>';
  const axis = chartTimeAxis(window);
  const range = timeBounds(window.start,window.end);
  if (!window.observed.length) return `<div class="quote-plot bitcoin-plot" role="img" aria-label="${esc(`No hourly closes from ${range}.`)}"><p class="quote-chart-empty">No completed hourly closes in this window.</p>${axis}</div>`;
  const values = window.observed.map(p=>p.close), scale = priceScale(values);
  const x = at => (3+994*(at-window.start)/(window.end-window.start)).toFixed(2);
  const y = v => 4+192*(scale.max-v)/(scale.max-scale.min);
  const lines = observedLines(window.points,'close','bid',x,y,hour);
  return `<div class="quote-plot bitcoin-plot" role="img" aria-label="${esc(`Bitstamp hourly closes from ${range}. Recorded low ${usd.format(Math.min(...values))}, high ${usd.format(Math.max(...values))}.`)}">
    <svg class="quote-y-axis" aria-hidden="true">${scale.ticks.map(v=>`<text x="98%" y="${(y(v)/2).toFixed(2)}%" dy=".32em" text-anchor="end">${tickUsd.format(v)}</text>`).join('')}</svg>
    <div class="chart-drawing-box"><svg class="quote-drawing" aria-hidden="true" viewBox="0 0 1000 200" preserveAspectRatio="none">
      ${scale.ticks.map(v=>`<line class="quote-gridline" x1="0" x2="1000" y1="${y(v).toFixed(2)}" y2="${y(v).toFixed(2)}"/>`).join('')}${lines}</svg></div>${axis}</div>`;
}
function bitcoinSwitch(label, action, value, disabled, pending) {
  const known = typeof value === 'boolean';
  return `<button type="button" class="paper-switch ${action === 'btc-feed' ? 'btc-feed-control ' : ''}${known ? value ? 'is-on' : 'is-off' : 'unknown'}" data-action="${action}" data-value="${!value}"
    ${known ? `role="switch" aria-checked="${value}" aria-label="${label}"` : ''}${disabled || !known ? ' disabled' : ''}${pending ? ' aria-busy="true"' : ''}>
    <i class="paper-lamp" aria-hidden="true"></i><span>${label}</span><b${known ? ' aria-hidden="true"' : ''}>${known ? value ? 'Yes' : 'No' : 'Unknown'}</b></button>`;
}
function sectionTitle(text,other='') {
  return `<div class="panel-head"><h2>${text}</h2>${other}</div>`;
}
function bitcoinHistory(market, ui) {
  const w = bitcoinWindow(market,ui.btcRange), quote = market.quote;
  const fresh = ui.connected && market.online && market.enabled && quote?.fresh === true;
  const bid = positive(quote?.bid_usd), ask = positive(quote?.ask_usd);
  const valid = bid === null || ask === null || bid <= ask;
  const quoteState = !quote ? 'Awaiting first quote' : fresh ? 'Latest posted quote' : 'Last observed quote';
  const choices = [...bitcoinRanges].map(([v,label])=>`<button type="button" data-action="btc-range" data-value="${v}" aria-pressed="${v===w.minutes}">${label}</button>`).join('');
  const values = w.observed.map(p=>p.close);
  const rangeText = values.length ? `${dollars(Math.min(...values))} – ${dollars(Math.max(...values))}` : dash;
  const rows = [...w.observed].reverse();
  const when = quote?.source_at ? `Venue quote ${esc(stamp(quote.source_at))}` : quote?.observed_at ? `Received ${esc(stamp(quote.observed_at))}` : '';
  const incomplete = w.observed.length && (w.observed[0].at-w.start > hour || w.end-w.observed.at(-1).at > hour);
  return `<section class="panel btc-market" aria-labelledby="btc-market-title">
    <div class="panel-head"><h2 id="btc-market-title">BTC / USD</h2><a class="small-note" href="https://www.bitstamp.net/api/" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">Bitstamp spot ${icon('external',13)}</a></div>
    <div class="panel-body btc-chart-body"><div class="section-line"><h3>${quoteState}</h3>${when ? `<span>${when}</span>` : ''}</div>
      <dl class="btc-top-quote"><div><dt>Best bid</dt><dd>${dollars(valid ? bid : null)}</dd></div><div><dt>Best ask</dt><dd>${dollars(valid ? ask : null)}</dd></div></dl>
      <section class="market-chart quote-history" aria-labelledby="btc-history-title"><div class="chart-heading"><h3 id="btc-history-title">Price history</h3><div class="quote-window btc-windows" role="group" aria-label="Bitcoin history window">${choices}</div></div>
        ${w.end === null ? '' : `<p class="history-window-bounds"><strong>${bitcoinRanges.get(w.minutes)}</strong> <span>·</span> ${esc(timeBounds(w.start,w.end))}</p>`}
        <div class="chart-readings"><dl class="history-ranges"><div><dt><i class="chart-series-bid" aria-hidden="true"></i>Hourly close range</dt><dd>${rangeText}</dd></div></dl><span class="chart-unit-label">USD per BTC</span></div>
        ${closeChart(w)}
        <p class="history-note">${w.observed.length} completed hourly ${w.observed.length === 1 ? 'close' : 'closes'}${w.observed.length ? ` · Last close ${esc(stamp(w.observed.at(-1).at))}` : ''}${incomplete ? `<br>Available closes span ${esc(timeBounds(w.observed[0].at,w.observed.at(-1).at))}.` : ''}</p>
        ${rows.length ? `<details class="quote-observations" data-key="btc-closes"><summary>Hourly closes (${rows.length})</summary><div class="table-scroll" tabindex="0" role="region" aria-label="Bitstamp hourly closing prices"><table class="data-table"><thead><tr><th scope="col">Period ended</th><th scope="col">BTC / USD close</th></tr></thead><tbody>${rows.map(p=>`<tr data-key="${p.at}"><td>${esc(day(p.at,true))} · ${esc(clock(p.at))} ${esc(zoneLabel(p.at))}</td><td class="numeric">${dollars(p.close)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
      </section></div></section>`;
}
function bitcoinAccount(market) {
  const p = market.portfolio || {};
  const latest = p.marked_at || arr(market.history).at(-1)?.at;
  const passive = market.benchmark;
  const benchmark = passive ? `<details class="btc-benchmark" data-key="btc-passive"><summary>Passive BTC benchmark</summary>
    <dl class="field-list">${field('Marked equity',money(passive.equity_cents))}${field('Bitcoin position',btc(passive.btc))}${field('Gain / loss',money(passive.net_gain_cents,true))}</dl>
    ${passive.marked_at ? `<p class="btc-note">Last benchmark mark ${esc(stamp(passive.marked_at))}.</p>` : ''}</details>` : '';
  return `<section class="panel">${sectionTitle('Bitcoin paper account')}<div class="panel-body"><dl class="btc-account-lead"><div><dt>${p.mark_fresh === false ? 'Last marked equity' : 'Marked equity'}</dt><dd>${money(p.equity_cents)}</dd></div>
    <div><dt>Net gain / loss</dt><dd>${money(p.net_gain_cents,true)}</dd></div></dl>
    <dl class="field-list">${field('Starting cash',money(p.initial_cash_cents))}${field('Available cash',money(p.cash_cents))}${field('Bitcoin position',btc(p.btc))}${field('Realized gain / loss',money(p.realized_cents,true))}${field('Paper fills',count(p.filled_orders))}</dl>
    ${latest ? `<p class="btc-note">Last equity mark ${esc(stamp(latest))}.</p>` : ''}${market.started_at ? `<p class="btc-note">Trial started ${esc(stamp(market.started_at))}.</p>` : '<p class="btc-note">The trial begins with the first eligible decision while paper trading is on.</p>'}${benchmark}</div></section>`;
}
function bitcoinSignal(market) {
  const s = market.signal || {};
  const target = {long:'Hold bitcoin',cash:'Hold cash'}[s.target] || 'Awaiting completed daily closes';
  return `<section class="panel">${sectionTitle('Daily signal')}<div class="panel-body"><h3 class="btc-target">${esc(target)}</h3>
    <dl class="field-list">${field('Last daily close',dollars(s.close_usd),s.day ? `${day(s.day)} · UTC market day` : '')}${field('Seven days earlier',dollars(s.reference_close_usd),s.reference_day ? `${day(s.reference_day)} · UTC market day` : '')}</dl>
    ${s.reason ? `<p class="btc-note">${esc(s.reason)}</p>` : ''}<p class="btc-note">A higher close signals a long position. Paper entries use up to $500, with a 0.60% fee and a 0.05% price allowance on each side.</p></div></section>`;
}
export function comparisonView(state,ui) {
  const c = state.comparison || {}, accounts = c.accounts || {};
  const rows = [['weather','Central Park strategy'],['bitcoin','Bitcoin spot strategy'],['hold','Passive $500 BTC hold']];
  const start = atMs(c.start_at), end = atMs(c.as_of);
  const ready = start !== null && end !== null && end >= start && rows.every(([id]) =>
    num(accounts[id]?.start_cents) !== null && num(accounts[id]?.end_cents) !== null);
  const data = ready ? `<dl class="btc-paired-dates"><div><dt>Common start</dt><dd>${esc(stamp(start))}</dd></div><div><dt>Latest shared mark</dt><dd>${esc(stamp(end))}</dd></div></dl>
    <div class="table-scroll" tabindex="0" role="region" aria-label="Common-start paper account comparison"><table class="data-table btc-compare"><thead><tr><th scope="col">Paper account</th><th scope="col">Starting equity</th><th scope="col">Latest equity</th><th scope="col">Gain / loss</th><th scope="col">Return</th><th scope="col">Max drawdown</th></tr></thead><tbody>${rows.map(([id,label])=>{
      const a=accounts[id];
      return `<tr><th scope="row">${label}</th><td>${money(a.start_cents)}</td><td>${money(a.end_cents)}</td><td>${money(a.net_cents,true)}</td><td>${percentage(a.return_pct)}</td><td>${money(a.drawdown_cents)}</td></tr>`;
    }).join('')}</tbody></table></div>${start === end ? '<p class="btc-note">One shared mark is available. The next one will show the first change over time.</p>' : ''}` :
    '<p class="btc-empty">Waiting for the first shared equity marks from the Bitcoin and Central Park paper accounts.</p>';
  return `${headline('Compare','Forward paper accounts')}<section class="panel btc-comparison">${sectionTitle('Strategy comparison')}<div class="panel-body">
    ${ui?.connected === false ? '<p class="btc-note">Worker disconnected. Showing the last received comparison.</p>' : ''}${data}
    <p class="btc-note">Returns and drawdowns use marks recorded for all three accounts at the same times. The passive benchmark buys up to $500 of BTC and holds the remaining cash.</p></div></section>`;
}
function bitcoinActivity(market) {
  const entries=arr(market.activity).filter(e=>typeof e?.message === 'string').sort((a,b)=>(atMs(b.at)||0)-(atMs(a.at)||0)).slice(0,8);
  return `<section class="panel">${sectionTitle('Recent Bitcoin decisions')}<div class="panel-body">${entries.length ? `<ol class="btc-activity">${entries.map(e=>`<li data-key="${esc(e.id ?? e.at)}"><time>${esc(stamp(e.at))}</time><span>${esc(e.message)}</span></li>`).join('')}</ol>` : '<p class="btc-empty">The worker will record Bitcoin paper decisions here.</p>'}</div></section>`;
}
const contractStatuses = {
  live:'Receiving public quotes', partial:'Partial market data', waiting:'Waiting for the first Kalshi quote',
  empty:'Waiting for the next contract', stale:'Quotes delayed', error:'Kalshi request failed',
  rate_limited:'Kalshi rate limit', offline:'Worker offline', disabled:'Kalshi market capture is off',
  disconnected:'Worker disconnected', unavailable:'Waiting for the Bitcoin contract feed',
};
const contractFields = ['yes_bid_cents','yes_ask_cents','no_bid_cents','no_ask_cents'];
function contractPrices(row) {
  const prices = {};
  for (const key of contractFields) {
    const v = num(row?.[key]);
    prices[key] = v !== null && v > 0 && v < 100 ? v : null;
  }
  for (const side of ['yes','no']) {
    const bid = side+'_bid_cents', ask = side+'_ask_cents';
    if (prices[bid] !== null && prices[ask] !== null && prices[bid] > prices[ask]) {
      prices[bid] = prices[ask] = null;
    }
  }
  return prices;
}
function contractHistory(market, now) {
  const points = new Map();
  const start = atMs(market.open_at), close = atMs(market.close_at);
  for (const row of arr(market.history)) {
    const at = atMs(row?.at);
    if (at === null || (start !== null && at < start) || (close !== null && at >= close) || at > now) continue;
    const gap = row.kind === 'gap';
    points.set(at,{at,gap,...contractPrices(gap ? null : row)});
  }
  return [...points.values()].sort((a,b)=>a.at-b.at);
}
function shortTime(at) {
  return atMs(at) === null ? dash : `${clock(at)} ${zoneLabel(at)}`;
}
function oddsChart(market, points, interval) {
  const start = atMs(market.open_at), end = atMs(market.close_at);
  if (start === null || end === null || end <= start) return '<p class="btc-empty">Waiting for the venue’s contract times.</p>';
  const span = timeBounds(start,end), axis = chartTimeAxis({minutes:(end-start)/60000,start,end,long:false});
  const book = points.filter(p=>!p.gap);
  const x = t => (3+994*(t-start)/(end-start)).toFixed(2);
  const y = v => 4+192*(1-v/100);
  const ticks = [100,75,50,25,0];
  const lines = observedLines(points,'yes_ask_cents','bid',x,y,interval*2000) +
    observedLines(points,'no_ask_cents','ask',x,y,interval*2000);
  const plot = lines ? `<div class="quote-plot" role="img" aria-label="${esc(`Observed YES and NO asking prices in cents, ${span}. Exact values are in the quote receipts.`)}">
    <svg class="quote-y-axis" aria-hidden="true">${ticks.map(n=>`<text x="96%" y="${y(n)/2}%" dy=".32em" text-anchor="end">${n}¢</text>`).join('')}</svg>
    <div class="chart-drawing-box"><svg class="quote-drawing" aria-hidden="true" viewBox="0 0 1000 200" preserveAspectRatio="none">
    ${ticks.map(n=>`<line class="quote-gridline" x1="0" x2="1000" y1="${y(n)}" y2="${y(n)}"/>`).join('')}${lines}</svg></div>${axis}</div>` :
    `<div class="quote-plot"><p class="quote-chart-empty">No asking prices have been captured for this window.</p>${axis}</div>`;
  const receipts = points.length ? `<details class="quote-observations" data-key="btc-odds-${esc(market.id)}"><summary>Quote receipts (${count(points.length)})</summary>
    <div class="table-scroll" role="region" tabindex="0" aria-label="Kalshi Bitcoin book receipts"><table class="data-table btc-book-receipts">
    <thead><tr><th scope="col">Received · ${esc(zoneLabel(start))}</th><th scope="col">YES bid</th><th scope="col">YES ask</th><th scope="col">NO bid</th><th scope="col">NO ask</th></tr></thead>
    <tbody>${[...points].reverse().map(p=>`<tr data-key="${p.at}"><th scope="row"><time datetime="${new Date(p.at).toISOString()}">${esc(shortTime(p.at))}</time></th>${p.gap ? '<td colspan="4">Quote unavailable</td>' : contractFields.map(key=>`<td>${price(p[key])}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>` : '';
  return `<section class="panel market-chart btc-contract-chart" aria-labelledby="btc-odds-title">
    <div class="panel-head"><h2 id="btc-odds-title">Asking price history</h2><span class="small-note">Cents per $1 contract</span></div>
    <div class="panel-body"><div class="btc-odds-key"><span><i class="chart-series-bid" aria-hidden="true"></i>YES ask</span><span><i class="chart-series-ask" aria-hidden="true"></i>NO ask</span><span>${esc(zoneLabel(start))}</span></div>${plot}
    <p class="btc-note">${count(book.length)} book ${book.length === 1 ? 'receipt' : 'receipts'} this window${book.length ? ` · Latest ${esc(shortTime(book.at(-1).at))}` : ''}.</p>${receipts}</div></section>`;
}
function oddsSide(side, prices, delta, market) {
  const ask = prices[side+'_ask_cents'], bid = prices[side+'_bid_cents'];
  const movement = delta === null ? '' : delta === 0 ? 'Unchanged since prior poll' :
    `${delta > 0 ? '+' : '−'}${price(Math.abs(delta))} since prior poll`;
  const size = ask !== null ? positive(market[side+'_ask_size']) : null;
  return `<div class="btc-odds-side"><span>${side.toUpperCase()} ask</span><strong>${price(ask)}</strong>
    <span class="btc-odds-bid">Bid <b>${price(bid)}</b>${size !== null ? ` · ${count(size)} ${size === 1 ? 'contract' : 'contracts'} at ask` : ''}</span>
    ${movement ? `<small>${esc(movement)}</small>` : ''}</div>`;
}
function contractTerms(market) {
  const rules = [market.rules_primary,market.rules_secondary].filter(t=>typeof t === 'string' && t.trim());
  return `<section class="panel btc-contract-terms">${sectionTitle('Contract terms')}<div class="panel-body">
    <dl class="field-list">${field('Opening target',dollars(market.target_usd))}${field('Opens',stamp(market.open_at))}${field('Closes',stamp(market.close_at))}
    ${field('Settlement index','CF Benchmarks BRTI')}</dl>
    <p class="btc-note">YES pays $1 if the closing 60-second index average is at least the opening average. Otherwise NO pays $1.</p>
    ${rules.length ? `<details class="btc-contract-rules" data-key="btc-rules-${esc(market.id)}"><summary>Read venue rules</summary>${rules.map(t=>`<p>${esc(t)}</p>`).join('')}</details>` : ''}
    <div class="btc-source-links">${market.rules_url ? sourceLink('Kalshi rules',market.rules_url) : ''}${market.index_url ? sourceLink('Settlement index',market.index_url) : ''}</div></div></section>`;
}
function previousWindows(recent,now) {
  const rows = arr(recent).filter(m=>m && atMs(m.close_at) !== null && atMs(m.close_at) <= now)
    .sort((a,b)=>atMs(b.close_at)-atMs(a.close_at)).slice(0,4);
  if (!rows.length) return '';
  return `<section class="panel btc-recent-windows">${sectionTitle('Previous windows')}<div class="panel-body"><p class="btc-note">Last observed quotes before each expiry.</p>
    <div class="table-scroll" tabindex="0" role="region" aria-label="Previously observed Bitcoin contracts"><table class="data-table btc-book-receipts"><thead><tr><th scope="col">Contract expired</th><th scope="col">Opening target</th><th scope="col">Last YES ask</th><th scope="col">Last NO ask</th><th scope="col">Last book receipt</th></tr></thead><tbody>${rows.map(m=>{
      const last = contractHistory(m,now).filter(p=>!p.gap).at(-1);
      return `<tr data-key="${esc(m.id)}"><th scope="row">${esc(stamp(m.close_at))}</th><td>${dollars(m.target_usd)}</td><td>${price(last?.yes_ask_cents)}</td><td>${price(last?.no_ask_cents)}</td><td>${esc(stamp(last?.at))}</td></tr>`;
    }).join('')}</tbody></table></div></div></section>`;
}
function contractMonitor(state,ui) {
  const feed = state.bitcoin_contracts || {}, now = Number.isFinite(ui.now) ? ui.now : Date.now();
  let status = !ui.connected ? 'disconnected' : feed.status || (state.health?.online === false ? 'offline' : 'unavailable');
  if (ui.connected && feed.enabled === false && status !== 'offline') status = 'disabled';
  if (!Object.hasOwn(contractStatuses,status)) status = 'unavailable';
  const permitted = feed.enabled !== false && status !== 'disabled';
  const market = permitted ? feed.current : null;
  const interval = positive(feed.interval_seconds) || 10;
  const start = atMs(market?.open_at), close = atMs(market?.close_at), received = atMs(market?.observed_at);
  const active = start !== null && close !== null && start <= now && now < close;
  const fresh = active && received !== null && received >= start && received <= now+1000 && now-received <= (positive(feed.stale_after_seconds) || 2*interval)*1000;
  if ((status === 'live' || status === 'partial') && market && !active) status = 'empty';
  else if ((status === 'live' || (status === 'partial' && received !== null)) && !fresh) status = 'stale';
  const posted = permitted && fresh && ['live','partial'].includes(status);
  const checked = feed.last_checked_at ? `Last checked ${esc(stamp(feed.last_checked_at))}` : 'Awaiting first check';
  const band = `<div class="feed-band btc-contract-status"><div><i class="state-symbol ${posted ? 'connected' : ['stale','error','rate_limited'].includes(status) ? 'late' : ''}" aria-hidden="true"></i><strong>${esc(contractStatuses[status])}</strong></div>
    <div>${icon('clock',15)}<span>${checked}${permitted && feed.interval_seconds ? ` · ${count(interval)}s polling` : ''}</span></div></div>`;
  const recent = permitted ? previousWindows(feed.recent,now) : '';
  const closed = close !== null && close <= now;
  if (!market || closed || (start !== null && now < start)) {
    const waiting = status === 'disabled' ? 'Public Kalshi quotes are off' : status === 'offline' ? 'Contract feed offline' :
      status === 'disconnected' ? 'Live feed disconnected' : status === 'empty' ? 'Waiting for the next 15-minute contract' : 'Waiting for a current contract';
    const message = status === 'disabled' ? 'Enable public Kalshi market data in Worker settings to follow the 15-minute Bitcoin market.' :
      status === 'offline' ? 'The online worker collects public Bitcoin prediction market quotes.' :
      status === 'disconnected' ? 'Reconnect to the worker to receive the current Bitcoin contract.' :
      closed ? 'The previous contract has closed. The next published market will appear after a venue check.' :
      feed.message || 'The worker will show the current Kalshi contract when it receives a public market response.';
    return `${band}<div class="btc-workspace"><section class="panel btc-contract-empty"><h2>${esc(waiting)}</h2><p>${esc(message)}</p>
      ${status === 'disabled' ? button('Market data settings','kalshi-settings',{kind:'secondary',symbol:'control'}) : ''}</section>${recent}</div>`;
  }
  const points = contractHistory(market,now), latest = points.at(-1), prior = points.at(-2);
  const current = contractPrices(posted ? market : null);
  const change = side => {
    const key = side+'_ask_cents';
    if (!latest || !prior || latest.at !== received || latest.at-prior.at > interval*2000 ||
        current[key] === null || latest[key] !== current[key] || prior[key] === null) return null;
    return Number((latest[key]-prior[key]).toFixed(4));
  };
  const title = positive(market.target_usd) !== null && market.comparison === 'greater_or_equal' ?
    `Bitcoin at or above ${dollars(market.target_usd)}?` : market.title || 'Current Bitcoin contract';
  const remaining = close === null ? dash : `<time datetime="${new Date(close).toISOString()}" data-btc-countdown="${esc(market.close_at)}" aria-live="off">${bitcoinCountdown(close,now)}</time>`;
  const reason = status === 'stale' && status !== feed.status ? 'The last book receipt is outside the freshness window.' : feed.message;
  const detail = ['partial','stale','rate_limited','error'].includes(status) && reason ? `<p class="btc-contract-warning">${esc(reason)}</p>` : '';
  return `${band}<div class="btc-workspace"><section class="panel btc-current" aria-labelledby="btc-current-title">
    <div class="panel-head"><div><h2 id="btc-current-title">${esc(title)}</h2><p>${esc(market.id)}${start !== null && close !== null ? ` · ${esc(timeBounds(start,close))}` : ''}</p></div>
    ${market.market_url ? `<div class="small-note">${sourceLink('Kalshi market',market.market_url)}</div>` : ''}</div>
    <div class="btc-odds-strip">${oddsSide('yes',current,change('yes'),market)}${oddsSide('no',current,change('no'),market)}
    <div class="btc-time-left"><span>Time to expiry</span><strong>${remaining}</strong><small>Closes ${esc(shortTime(close))}</small></div></div>
    <p class="btc-receipt-line">${received === null ? 'Awaiting this contract’s first book receipt.' : `Book received ${esc(stamp(received))}.`}${market.http_date ? ` Venue response ${esc(shortTime(market.http_date))}.` : ''}</p>${detail}</section>
    <div class="btc-grid">${oddsChart(market,points,interval)}${contractTerms(market)}</div>${recent}</div>`;
}
function bitcoinSpot(state,ui) {
  const m=state.bitcoin || {}, linked=ui.connected === true;
  const known = linked && typeof m.enabled === 'boolean';
  const controls = bitcoinSwitch('BTC collection','btc-feed',known ? m.enabled : null,ui.locked || (!m.enabled && m.online === false),ui.busy) +
    bitcoinSwitch('Paper auto trading','btc-auto',known ? m.auto_enabled : null,ui.locked || (!m.auto_enabled && (!m.enabled || !m.online)),ui.busy);
  const status=!linked ? 'Worker disconnected' : !known ? 'Waiting for the Bitcoin worker' : m.online === false ? 'Worker offline' : !m.enabled ? 'BTC collection is off. Enable collection to start the paper trial.' : m.status || 'Awaiting data';
  const body = `<div class="feed-band"><div><i class="state-symbol ${linked && m.enabled && m.quote?.fresh ? 'connected' : ''}" aria-hidden="true"></i><strong>${esc(status)}</strong></div>
    <div class="scan-time">${icon('clock',15)}${m.last_checked_at ? `Last checked ${esc(stamp(m.last_checked_at))}` : 'Awaiting the first Bitstamp check'}</div></div>
    <div class="btc-workspace"><div class="btc-grid">${bitcoinHistory(m,ui)}<div class="btc-sections">${bitcoinAccount(m)}${bitcoinSignal(m)}</div></div>${bitcoinActivity(m)}</div>`;
  return {body,controls};
}
export function bitcoinView(state,ui) {
  const panels = [['contracts','15-minute markets'],['spot','Spot strategy']];
  const active = ui.btcPanel === 'spot' ? 'spot' : 'contracts';
  const spot = active === 'spot' ? bitcoinSpot(state,ui) : null;
  const tabs = `<div class="btc-panel-tabs" role="tablist" aria-label="Bitcoin views">${panels.map(([key,label])=>
    `<button type="button" role="tab" id="btc-tab-${key}" data-action="btc-panel" data-value="${key}" aria-selected="${active===key}" aria-controls="btc-view-${key}" tabindex="${active===key ? 0 : -1}">${label}</button>`).join('')}</div>`;
  const body = spot ? spot.body : contractMonitor(state,ui);
  return `${headline('Bitcoin',spot ? 'BTC / USD <span class="context-divider">/</span> Spot paper trading' : 'Kalshi <span class="context-divider">/</span> 15-minute prediction markets',spot?.controls)}${tabs}
    ${panels.map(([key])=>`<div id="btc-view-${key}" role="tabpanel" aria-labelledby="btc-tab-${key}"${active===key ? ' tabindex="0"' : ' hidden'}>${active===key ? body : ''}</div>`).join('')}`;
}
export function optionsView() {
  return `${headline('Options','Paper trading workspace')}<section class="panel options-coming"><h2>Coming soon</h2><p>The options workspace is being built.</p></section>`;
}
