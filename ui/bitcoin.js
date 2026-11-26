import {arr, clock, count, dash, day, esc, field, headline, icon, instant, money, num, stamp, zoneLabel} from './common.js';
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
function closeChart(window) {
  if (window.end === null) return '<p class="btc-empty">Waiting for the first Bitstamp capture.</p>';
  const axis = chartTimeAxis(window);
  const range = timeBounds(window.start,window.end);
  if (!window.observed.length) return `<div class="quote-plot bitcoin-plot" role="img" aria-label="${esc(`No hourly closes from ${range}.`)}"><p class="quote-chart-empty">No completed hourly closes in this window.</p>${axis}</div>`;
  const values = window.observed.map(p=>p.close), scale = priceScale(values);
  const x = at => (3+994*(at-window.start)/(window.end-window.start)).toFixed(2);
  const y = v => 4+192*(scale.max-v)/(scale.max-scale.min);
  const runs = [];
  let run = [];
  for (const point of window.points) {
    if (point.close === null || (run.length && point.at-run.at(-1).at !== hour)) {
      if (run.length) runs.push(run);
      run = [];
    }
    if (point.close !== null) run.push(point);
  }
  if (run.length) runs.push(run);
  const lines = runs.map(segment => {
    if (segment.length === 1) return `<path class="quote-point bid" d="M${x(segment[0].at)},${y(segment[0].close).toFixed(2)}h.01"/>`;
    return `<path class="quote-line bid" d="${segment.map((p,i)=>`${i ? 'L' : 'M'}${x(p.at)},${y(p.close).toFixed(2)}`).join(' ')}"/>`;
  }).join('');
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
  return `<section class="panel">${sectionTitle('Bitcoin paper account')}<div class="panel-body"><dl class="btc-account-lead"><div><dt>${p.mark_fresh === false ? 'Last marked equity' : 'Marked equity'}</dt><dd>${money(p.equity_cents)}</dd></div>
    <div><dt>Net gain / loss</dt><dd>${money(p.net_gain_cents,true)}</dd></div></dl>
    <dl class="field-list">${field('Starting cash',money(p.initial_cash_cents))}${field('Available cash',money(p.cash_cents))}${field('Bitcoin position',btc(p.btc))}${field('Realized gain / loss',money(p.realized_cents,true))}${field('Paper fills',count(p.filled_orders))}</dl>
    ${latest ? `<p class="btc-note">Last equity mark ${esc(stamp(latest))}.</p>` : ''}${market.started_at ? `<p class="btc-note">Trial started ${esc(stamp(market.started_at))}.</p>` : '<p class="btc-note">The trial begins with the first eligible decision while paper trading is on.</p>'}</div></section>`;
}
function bitcoinSignal(market) {
  const s = market.signal || {};
  const target = {long:'Hold bitcoin',cash:'Hold cash'}[s.target] || 'Awaiting completed daily closes';
  return `<section class="panel">${sectionTitle('Daily signal')}<div class="panel-body"><h3 class="btc-target">${esc(target)}</h3>
    <dl class="field-list">${field('Last daily close',dollars(s.close_usd),s.day ? `${day(s.day)} · UTC market day` : '')}${field('Seven days earlier',dollars(s.reference_close_usd),s.reference_day ? `${day(s.reference_day)} · UTC market day` : '')}</dl>
    ${s.reason ? `<p class="btc-note">${esc(s.reason)}</p>` : ''}<p class="btc-note">A higher close signals a long position. Paper entries use up to $500, with a 0.60% fee and a 0.05% price allowance on each side.</p></div></section>`;
}
function comparisonPanel(comparison, benchmark) {
  const c = comparison || {}, accounts = c.accounts || {};
  const rows = [['bitcoin','Bitcoin strategy'],['weather','Central Park strategy'],['hold','Passive $500 BTC hold']];
  const ready = c.start_at && c.as_of && rows.every(([id])=>accounts[id] && num(accounts[id].start_cents) !== null && num(accounts[id].end_cents) !== null);
  const data = ready ? `<p class="btc-note">Common window ${esc(stamp(c.start_at))} to ${esc(stamp(c.as_of))}.</p>
    <div class="table-scroll" tabindex="0" role="region" aria-label="Common-start paper account comparison"><table class="data-table btc-compare"><thead><tr><th scope="col">Paper account</th><th scope="col">Starting equity</th><th scope="col">Latest equity</th><th scope="col">Gain / loss</th><th scope="col">Return</th><th scope="col">Max drawdown</th></tr></thead><tbody>${rows.map(([id,label])=>{
      const a=accounts[id];
      return `<tr><th scope="row">${label}</th><td>${money(a.start_cents)}</td><td>${money(a.end_cents)}</td><td>${money(a.net_cents,true)}</td><td>${percentage(a.return_pct)}</td><td>${money(a.drawdown_cents)}</td></tr>`;
    }).join('')}</tbody></table></div>` : '<p class="btc-empty">Waiting for the first shared equity marks from the Bitcoin and Central Park paper accounts.</p>';
  const passive = typeof benchmark === 'object' && benchmark ? `<dl class="btc-hold-mark"><div><dt>Passive BTC equity</dt><dd>${money(benchmark.equity_cents)}</dd></div>
    <div><dt>Passive BTC position</dt><dd>${btc(benchmark.btc)}</dd></div></dl>` : '';
  return `<section class="panel">${sectionTitle('Strategy comparison')}<div class="panel-body">${data}<p class="btc-note">Returns use each account’s equity at the same starting time. The passive benchmark buys up to $500 of BTC and holds the remaining cash.</p>${passive}</div></section>`;
}
function bitcoinActivity(market) {
  const entries=arr(market.activity).filter(e=>typeof e?.message === 'string').sort((a,b)=>(atMs(b.at)||0)-(atMs(a.at)||0)).slice(0,8);
  return `<section class="panel">${sectionTitle('Recent Bitcoin decisions')}<div class="panel-body">${entries.length ? `<ol class="btc-activity">${entries.map(e=>`<li data-key="${esc(e.id ?? e.at)}"><time>${esc(stamp(e.at))}</time><span>${esc(e.message)}</span></li>`).join('')}</ol>` : '<p class="btc-empty">The worker will record Bitcoin paper decisions here.</p>'}</div></section>`;
}
export function bitcoinView(state,ui) {
  const m=state.bitcoin || {}, linked=ui.connected === true;
  const known = linked && typeof m.enabled === 'boolean';
  const controls = bitcoinSwitch('BTC collection','btc-feed',known ? m.enabled : null,ui.locked || (!m.enabled && m.online === false),ui.busy) +
    bitcoinSwitch('Paper auto trading','btc-auto',known ? m.auto_enabled : null,ui.locked || (!m.auto_enabled && (!m.enabled || !m.online)),ui.busy);
  const status=!linked ? 'Worker disconnected' : !known ? 'Waiting for the Bitcoin worker' : m.online === false ? 'Worker offline' : !m.enabled ? 'BTC collection is off. Enable collection to start the paper trial.' : m.status || 'Awaiting data';
  return `${headline('Bitcoin','BTC / USD <span class="context-divider">/</span> Spot paper trading',controls)}
    <div class="feed-band"><div><i class="state-symbol ${linked && m.enabled && m.quote?.fresh ? 'connected' : ''}" aria-hidden="true"></i><strong>${esc(status)}</strong></div>
    <div class="scan-time">${icon('clock',15)}${m.last_checked_at ? `Last checked ${esc(stamp(m.last_checked_at))}` : 'Awaiting the first Bitstamp check'}</div></div>
    <div class="btc-workspace"><div class="btc-grid">${bitcoinHistory(m,ui)}<div class="btc-sections">${bitcoinAccount(m)}${bitcoinSignal(m)}</div></div>${comparisonPanel(state.comparison,m.benchmark)}${bitcoinActivity(m)}</div>`;
}
export function optionsView() {
  return `${headline('Options','Paper trading workspace')}<section class="panel options-coming"><h2>Coming soon</h2><p>The options workspace is being built.</p></section>`;
}
