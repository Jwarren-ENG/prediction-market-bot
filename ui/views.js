import {marketActivity} from './activity.js';
import {age, arr, button, count, dash, day, emptyBox, esc, field, headline, human, icon, instant, money, num, price, sideName, sourceLink, scanButton, displayTimeZone, timeZoneChoices, stamp, text, tone} from './common.js';
import {recentEntries} from './research.js';

function table(headers, rows, label) {
  return `<div class="table-scroll" tabindex="0" role="region" aria-label="${esc(label)}"><table class="data-table"><thead><tr>${headers.map(s => `<th scope="col">${esc(s)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}
const metric = (label, value, sign = '') => `<div><dt>${esc(label)}</dt><dd class="${sign}">${esc(value)}</dd></div>`;
const panel = (title, body, extra = '') => `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2>${extra}</div>${body}</section>`;
export const modeLabel = state => state.mode === 'replay' ? 'Offline replay' : 'Forward paper';
function calibrationStatus(state) {
  if (state.mode !== 'forward') return '';
  const asOf = instant(state.portfolio?.mark_as_of || state.health?.heartbeat);
  if (!asOf) return '';
  const open = arr(state.markets).filter(m => {
    const close = instant(m.close_at);
    return close && close > asOf;
  });
  const comparable = open.filter(m => num(m.forecast?.samples) !== null && num(m.forecast?.minimum_samples) > 0);
  if (!comparable.length) return '';
  const coverage = m => m.forecast.samples / m.forecast.minimum_samples;
  const best = comparable.reduce((a,b) => coverage(a) >= coverage(b) ? a : b);
  const f = best.forecast;
  const ready = comparable.filter(m => coverage(m) >= 1).length;
  return panel('Evidence for paper entries',`<div class="panel-body"><dl class="budget-line">${field('Comparable station days',count(f.samples)+' / '+count(f.minimum_samples),'Best-covered open forecast · '+day(best.event_date))}
    ${field('Contracts with enough history',count(ready)+' / '+count(comparable.length),'Open contracts with a checked sample count.')}</dl>
    <p class="secondary-copy">${ready ? 'A contract with enough history still needs an edge after fees, available order size and room within the risk limits.' : 'The worker is collecting dated forecasts and outcomes. Multiple price checks and temperature ranges for the same day count as one weather outcome.'}</p></div>`);
}
function scorecard(state) {
  const s = state.scorecard;
  if (!s) return '';
  const score = v => num(v) == null ? dash : num(v).toFixed(3);
  return panel('Forecast accuracy',`<div class="panel-body"><p class="secondary-copy">${s.source === 'fixture' || state.mode === 'replay' ? 'Synthetic replay' : 'Forward observations'} · ${count(s.resolved_dates)} resolved ${s.resolved_dates === 1 ? 'date' : 'dates'} · ${count(s.markets)} scored ${s.markets === 1 ? 'market' : 'markets'}.</p>
    <dl class="budget-line">${field('Station model',score(s.model_brier))}${field('Market midpoint',score(s.market_brier))}${field('Climatology',score(s.climatology_brier))}</dl>
    <p class="secondary-copy">Lower Brier scores indicate more accurate probabilities. Each resolved date carries equal weight.</p></div>`);
}
export function portfolioView(state) {
  const p = state.portfolio || {};
  const positions = arr(p.positions);
  const orders = arr(p.orders);
  const events = arr(p.events);
  const filled = num(p.filled_orders);
  const settled = num(p.settled_orders);
  const tradingRecord = filled === null || settled === null ? 'Paper account' :
    filled === 0 ? 'No paper orders have filled yet' :
    `${count(filled)} filled paper ${filled === 1 ? 'order' : 'orders'} · ${count(settled)} settled. Partial fills count once per order.`;
  const positionTable = positions.length ? table(['Contract','Side','Held','Average ask','Marked value','Open P&L','Mark'], positions.map(r => `<tr>
    <th scope="row">${button(r.label || r.market_id || r.id, 'position', {value: r.market_id || r.id, kind: 'inline'})}<div class="row-meta">${esc(r.market_id || r.id)}</div></th>
    <td>${esc(sideName(r.side) || dash)}</td><td class="numeric">${count(r.qty)}</td><td class="numeric">${price(r.avg_ask_cents)}</td>
    <td class="numeric">${money(r.mark_value_cents)}</td><td class="numeric ${tone(r.open_pnl_cents)}">${money(r.open_pnl_cents, true)}</td>
    <td>${esc(human(r.mark_status) || dash)}<div class="row-meta">${num(r.quote_age_seconds) === null ? '' : esc(age(r.quote_age_seconds))}</div></td></tr>`), 'Open paper positions') :
    emptyBox('No open positions', 'An open position appears here when a paper order fills. You can find settled trades in Activity.', 'portfolio');
  const reserveTable = orders.length ? table(['Contract','Side','Outstanding','Reserved','Order state'], orders.map(r =>
    `<tr><th scope="row">${esc(r.market_id || r.id)}</th><td>${esc(sideName(r.side) || dash)}</td><td class="numeric">${count(r.outstanding_qty)}</td><td class="numeric">${money(r.reserved_cents)}</td><td>${esc(human(r.status) || dash)}${r.cancel_requested ? ' · Cancellation requested' : ''}</td></tr>`), 'Outstanding paper orders') : '<p class="compact-empty">No outstanding orders.</p>';
  const eRows = events.map(e => `<tr><th scope="row">${esc(e.event_id)}</th><td class="numeric">${money(e.charged_exposure_cents)}</td><td class="numeric">${money(e.worst_case_cents)}</td></tr>`);
  return `${headline('Portfolio', `${esc(modeLabel(state))} <span class="context-divider">/</span> ${esc(tradingRecord)}`)}
    <dl class="account-strip">${metric('Starting paper cash', money(p.initial_cash_cents))}${metric('Marked equity', money(p.equity_cents))}
      ${metric('Net gain or loss', money(p.net_pnl_cents,true),tone(p.net_pnl_cents))}${metric('Realized P&L', money(p.realized_cents,true),tone(p.realized_cents))}</dl>
    ${p.stale_marks ? `<div class="inline-alert">${icon('warn',18)}${esc(count(p.stale_marks))} stale ${p.stale_marks === 1 ? 'mark' : 'marks'}. Review marked values before using equity.</div>` : ''}
    <div class="page-panels">${calibrationStatus(state)}${panel('Open positions',positionTable, `<span class="small-note">${positions.length} ${positions.length === 1 ? 'position' : 'positions'}</span>`)}
      <div class="two-panels">${panel('Cash and marks', `<dl class="field-list panel-body">${field('Cash balance',money(p.cash_cents))}
        ${field('Available cash',money(p.available_cash_cents))}${field('Reserved cash',money(p.reserved_cents))}${field('Open cost',money(p.open_cost_cents))}
        ${field('Open P&L',money(p.open_pnl_cents,true))}${field('Fees paid',money(p.fees_cents), 'Included in net gain or loss.')}${field('Marked as of',stamp(p.mark_as_of))}</dl>`)}
        ${panel('Paper risk', `<dl class="field-list panel-body">${field('Maximum open loss',money(p.max_loss_cents))}${field('Daily marked loss',money(p.daily_loss_cents))}
          ${field('Drawdown from peak',money(p.drawdown_cents))}${field('Per position limit',money(p.limits?.position_cents))}${field('Per event limit',money(p.limits?.event_cents))}
          ${field('Portfolio risk limit',money(p.limits?.portfolio_cents))}${field('Daily loss stop',money(p.limits?.daily_loss_cents))}${field('Drawdown stop',money(p.limits?.drawdown_cents))}</dl>`)}</div>
      ${scorecard(state)}${panel('Outstanding orders',reserveTable)}
      ${eRows.length ? panel('Exposure by event',table(['Event','Charged exposure','Worst case loss'],eRows,'Exposure by event')) : ''}</div>`;
}
export function activityView(state, ui) {
  const entries = recentEntries(state.activity);
  const markets = new Map(arr(state.markets).map(m => [m.id,m]));
  const kinds = [...new Set(entries.map(r => r.kind).filter(Boolean))];
  const shown = entries.filter(r => ui.activityFilter === 'All events' || r.kind === ui.activityFilter);
  const select = `<div class="filter">${icon('filter',15)}<select id="activity-filter" aria-label="Filter activity by event type">${['All events',...kinds].map(k =>
    `<option value="${esc(k)}"${k === ui.activityFilter ? ' selected' : ''}>${k === 'All events' ? k : esc(human(k))}</option>`).join('')}</select></div>`;
  return `${headline('Activity', `${esc(modeLabel(state))} <span class="context-divider">/</span> Worker and ledger history`,state.mode === 'forward' ? scanButton(state,ui) : '')}
    ${marketActivity(state,ui)}
    ${panel('Event log',shown.length ? table(['Time','Event','Details'],shown.map(e =>
      `<tr data-key="${esc(e.id ?? e.at)}"><td class="time-cell numeric">${esc(stamp(e.at))}</td>
      <td>${esc(human(e.kind) || 'Worker event')}${e.market_id ? `<div class="row-meta">${esc(markets.get(e.market_id)?.label || e.market_id)}</div>` : ''}</td><td>${esc(e.message || dash)}</td></tr>`),'Worker event history') :
      emptyBox('No matching events', 'The worker records its decisions, fills and settlements here.', 'activity'),select)}`;
}
function modeControls(state,ui) {
  const replay = state.mode === 'replay';
  const intervalText = value => {
    const seconds = num(value);
    if (seconds === null || seconds <= 0) return 'Interval unavailable';
    return seconds % 60 === 0 ? `Every ${count(seconds / 60)} min` : `Every ${count(seconds)} s`;
  };
  const permitted = state.settings?.allow_market === true;
  const online = state.health?.online !== false;
  const automation = state.paper_automation;
  const risk = automation ? automation.risk_pause_reason : state.risk_pause_reason;
  const paused = automation ? automation.manual_pause || !!risk : state.paused;
  const entries = paused ? 'Paused' : !online ? 'Armed · worker offline' : !permitted ? 'Armed · awaiting market permission' : 'Armed for eligible contracts';
  const entryButton = risk ? 'Recheck risk and resume' : paused ? 'Resume entries' : 'Pause entries';
  const change = (name, value, body) => `<button type="button" class="mode-choice" data-action="mode" data-value="${value}" aria-pressed="${state.mode === value}"${ui.locked ? ' disabled' : ''}>
    <span class="radio-mark" aria-hidden="true"></span><span><b>${name}</b><small>${body}</small></span></button>`;
  return `${panel('Operating mode',`<div class="panel-body settings-body"><div class="mode-choices">
    ${change('Offline replay','replay','Work through the synthetic Central Park case.')}
    ${change('Forward paper','forward','Capture new eligible contracts and weather evidence.')}</div>
    <div class="settings-actions">${replay ? button('Run replay','replay',{symbol:'next',disabled:ui.locked}) : scanButton(state,ui)}</div></div>`)}
    <section class="panel"><div class="panel-head"><h2 id="automation-title" tabindex="-1">Paper automation</h2></div>
    <div class="panel-body settings-body"><p>The worker continues collecting data and evaluating eligible contracts while its host is awake, including when this window is closed.</p>
      <div class="automation-control"><div class="section-line"><h3>Public market collection</h3><span>${!online ? 'Worker offline' : permitted ? 'Enabled' : 'Off'}</span></div>
        <p>${state.quotes?.interval_seconds ? `${esc(intervalText(state.quotes.interval_seconds))}, the worker checks Kalshi for public YES bids and asks. ` : ''}Recorded quotes appear in Markets.</p>
        <label class="check-row"><input type="checkbox" id="market-permission"${permitted ? ' checked' : ''}${ui.locked ? ' disabled' : ''}>
        <span>I authorize market data capture under my applicable terms.</span></label></div>
      <div class="automation-control"><div class="section-line"><h3>Automatic paper entries</h3><span>${esc(entries)}</span></div>
        <p>${state.settings?.scan_interval_seconds ? `${esc(intervalText(state.settings.scan_interval_seconds))}, a full forward scan evaluates ` : 'Full forward scans evaluate '}the forecast, book depth, fees and risk limits. Price updates and weather collection continue during an entry pause.</p>
        ${risk ? `<p>${esc(risk)}</p>` : ''}
        ${button(entryButton,paused?'resume':'pause',{symbol:paused?'play':'pause',disabled:ui.locked})}</div>
    </div></section>`;
}
function providerSettings(state, ui) {
  const r = state.research || {};
  const providers = arr(r.providers);
  const p = providers.find(p => p.id === ui.provider) || providers[0];
  const limits = `<dl class="budget-line">${field('Daily call cap',count(r.max_calls_per_day))}
    ${r.monthly_allowance_cents != null ? field('Monthly estimate cap',money(r.monthly_allowance_cents)) : ''}
    ${r.reserved_this_month_cents != null ? field('Estimated use this month',money(r.reserved_this_month_cents)) : ''}</dl>`;
  return panel('Research analysts',`<div class="panel-body settings-body">
    <p>The local analyst reads the recorded evidence. Enabled external analysts receive permitted research packets when research runs.</p>
    ${limits}<p class="budget-note">PMB reserves 25¢ per external request and enforces the daily call cap. Your provider’s billing settings control actual charges.</p>
    ${providers.length ? `<div class="provider-picker"><label for="provider-picker">External analyst</label><select id="provider-picker">
      ${providers.map(entry => `<option value="${esc(entry.id)}"${entry.id === p.id ? ' selected' : ''}>${esc(entry.label || entry.id)}</option>`).join('')}</select>
      <span class="small-note">${p.ready ? 'Ready for research' : p.enabled ? 'Setup incomplete' : 'Disabled'}</span></div>
      <form class="provider-form" id="provider-form" data-key="form-${esc(p.id)}" autocomplete="off">
        <div class="form-fields"><label>Model ID<input name="model" autocomplete="off" spellcheck="false" maxlength="120" value="${esc(p.model)}"></label>
        <label>Daily call cap across analysts<input type="number" name="budget" min="0" max="100" step="1" required value="${num(r.max_calls_per_day) ?? ''}"></label>
        ${p.id === 'omniroute' ? `<label class="full">Worker-side endpoint<input name="endpoint" type="url" spellcheck="false" value="${esc(p.endpoint)}"></label>` : ''}
        <label class="full">API key${p.has_key ? ' · already configured' : p.id === 'omniroute' ? ' (optional)' : ''}<input name="key" type="password" maxlength="2048" autocomplete="new-password" ${p.has_key ? 'placeholder="Leave blank to keep the current key"' : ''}></label></div>
        <label class="check-row"><input type="checkbox" name="enabled"${p.enabled ? ' checked' : ''}><span>Enable ${esc(p.label || p.id)} for external research</span></label>
        <button class="btn secondary" type="submit"${ui.locked ? ' disabled' : ''}>Save analyst settings</button></form>` : '<p class="compact-empty">This worker has no external analyst configuration.</p>'}
    <label class="check-row auto-check"><input type="checkbox" id="research-auto"${r.auto ? ' checked' : ''}${ui.locked ? ' disabled' : ''}>
      <span>Allow automatic research within the worker’s budget</span></label></div>`);
}
function sourceSummary(value) {
  if (!value) return '';
  if (typeof value === 'string') return `<p class="source-summary">${esc(value)}</p>`;
  const records = Array.isArray(value) ? value.map((v,i) => [v?.source || v?.name || `Source ${i+1}`,v]) : Object.entries(value);
  return `<h3 class="source-heading">Source status</h3><dl class="field-list">${records.map(([k,v]) =>
    field(human(k), text(v) || text(v?.status) || text(v?.detail) || dash, text(v?.message) || text(v?.reason))).join('')}</dl>`;
}
function notificationsPanel(n) {
  if (!n) return '';
  return panel('Email notifications',`<dl class="field-list panel-body">${field('Delivery status',n.status || (n.enabled ? n.ready ? 'Ready' : 'Setup incomplete' : 'Off'))}
    ${field('Pending deliveries',count(n.pending))}${field('Last delivery',stamp(n.last_delivery?.at || n.last_delivery))}
    ${n.review_required ? field('Owner review',typeof n.review_required === 'number' ? count(n.review_required) : 'Required') : ''}</dl>`);
}
function forwardReports(health) {
  const reports = arr(health.source_evidence);
  if (!reports.length) return '';
  return panel('Recent public weather reports',`<ul class="public-reports">${reports.map(r =>
    `<li><b>${sourceLink(r.title || 'Weather source',r.url)}</b><p>${esc(r.detail)}</p>
      <small>Published ${esc(stamp(r.published_at))} · Available ${esc(stamp(r.available_at))}</small></li>`).join('')}</ul>`);
}
export function connectionForm(ui) {
  return `<form id="connection-form" class="connection-form" autocomplete="off"><label for="worker-url">Worker connection URL</label>
    <p id="connection-help">Paste the link from your worker. Use HTTPS for a hosted worker or a loopback address through an SSH tunnel.</p>
    <input id="worker-url" name="url" type="password" inputmode="url" required autocomplete="off" spellcheck="false" aria-describedby="connection-help connection-error" aria-invalid="${!!ui.connectionError}">
    <p id="connection-error" class="field-error">${esc(ui.connectionError)}</p><button type="submit" class="btn secondary">${icon('link',16)}Open worker</button></form>`;
}
export function workerView(state, ui) {
  const h = state.health || {};
  return `${headline('Worker', `${esc(modeLabel(state))} <span class="context-divider">/</span> Connection and settings`)}
    <div class="worker-grid"><div class="page-panels">
    ${panel('Worker health',`<div class="panel-body">${h.status ? `<div class="health-status"><span class="state-symbol ${ui.healthTone}" aria-hidden="true"></span>${esc(h.status)}</div>` : ''}
      <dl class="field-list">${field('Worker version',state.version || dash)}${field('Connected host',location.host)}${field('Last heartbeat',stamp(h.heartbeat))}
      ${field('Last forward scan',stamp(h.last_scan))}${field('External feeds',h.online === false ? 'Disabled at worker startup' : h.online === true ? 'Available to the worker' : dash)}
      ${num(state.settings?.scan_interval_seconds) != null ? field('Forward scan interval',age(state.settings.scan_interval_seconds)) : ''}
      ${field('Paper entries',state.paused ? 'Paused':'Enabled')}</dl>${sourceSummary(h.source_status)}</div>`)}
    ${panel('Display settings',`<div class="panel-body settings-body"><label class="zone-setting" for="display-zone">Time zone<select id="display-zone">
      ${timeZoneChoices.map(([id,label])=>`<option value="${esc(id)}"${id === displayTimeZone() ? ' selected' : ''}>${esc(label)}</option>`).join('')}</select></label></div>`)}
    ${forwardReports(h)}${notificationsPanel(state.notifications)}${panel('Connect to another worker',`<div class="panel-body">${connectionForm(ui)}</div>`)}</div>
    <div class="page-panels">${modeControls(state,ui)}${providerSettings(state,ui)}</div></div>`;
}
