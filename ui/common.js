import {instant, stamp} from './time.js';
export {instant, day, clock, stamp, zoneLabel, wallClock, setDisplayTimeZone, reloadDisplayTimeZone, displayTimeZone, timeZoneChoices} from './time.js';
const entities = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};

export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => entities[c]);
export const arr = value => Array.isArray(value) ? value : [];
export const text = value => ['number', 'string', 'boolean'].includes(typeof value) ? String(value) : '';
export const dash = '—';
export const num = value => {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const moneyFormat = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
const figureFormat = new Intl.NumberFormat('en-US', {maximumFractionDigits: 2});
const askFormat = new Intl.NumberFormat('en-US', {maximumFractionDigits: 3});
export const count = value => num(value) == null ? dash : figureFormat.format(num(value));
export function money(cents, signed = false) {
  const n = num(cents);
  if (n == null) return dash;
  return `${n < 0 ? '−' : signed && n > 0 ? '+' : ''}${moneyFormat.format(Math.abs(n) / 100)}`;
}
export const price = value => num(value) == null ? dash : `${askFormat.format(num(value))}¢`;
export const probability = value => {
  const n = num(value);
  return n !== null && n >= 0 && n <= 1 ? n : null;
};
export function percent(value) {
  const p = probability(value);
  return p == null ? dash : `${Number((p * 100).toFixed(1))}%`;
}
export const sideName = value => ['YES', 'NO'].includes(text(value).toUpperCase()) ? text(value).toUpperCase() : '';
export function age(value) {
  const seconds = num(value);
  if (seconds == null) return dash;
  if (seconds < 0) return `${count(Math.abs(seconds))} s ahead of decision`;
  if (seconds < 60) return `${count(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${Math.floor(seconds % 60)} s`;
  return `${count(seconds / 3600)} h`;
}
export function human(value) {
  const words = text(value).replace(/[_-]+/g, ' ').trim();
  if (!words) return '';
  return words[0].toUpperCase() + words.slice(1);
}
export function sourceUrl(value) {
  try {
    const u = new URL(value);
    return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? u.href : '';
  } catch { return ''; }
}
export function sourceLink(label, url) {
  const href = sourceUrl(url);
  return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">${esc(label)}${icon('external', 14)}<span class="sr-only"> (opens a new window)</span></a>` : esc(label);
}
export function secureWorker(url) {
  return url.protocol === 'https:' || (url.protocol === 'http:' && (
    url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(url.hostname)
  ));
}
export function connectionUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('Enter the complete URL from your worker.'); }
  if (!secureWorker(url)) throw new Error('Use HTTPS or a loopback address for this connection.');
  if (url.username || url.password || url.search || !['/', '/index.html'].includes(url.pathname)) {
    throw new Error('Use the worker’s root URL. The access token belongs in its link fragment.');
  }
  if (url.hash) {
    const parts = [...new URLSearchParams(url.hash.slice(1))];
    if (parts.length !== 1 || parts[0][0] !== 'token' || !parts[0][1]) {
      throw new Error('Paste the complete connection link with its token fragment.');
    }
  }
  return url;
}
const drawings = {
  markets: '<path d="M3 3v17h18M6 13l4-4 4 2 6-7m-4 0h4v4"/>',
  research: '<rect x="3" y="4" width="18" height="16"/><path d="M3 10h18m-13 4h4"/>',
  portfolio: '<path d="M3 8h18v12H3zM8 8V5h8v3M3 12h18m-11 0v2h4v-2"/>',
  activity: '<path d="M2 13h5l3-8 4 14 3-8h5"/>',
  worker: '<rect x="3" y="4" width="18" height="7"/><path d="M7 7.5h.1m2.9 0h.1"/><rect x="3" y="14" width="18" height="7"/><path d="M7 17.5h.1m2.9 0h.1"/>',
  pause: '<path d="M8 5v14m8-14v14" stroke-width="3"/>',
  play: '<path d="m8 5 10 7-10 7z"/>',
  next: '<path d="m6 5 9 7-9 7zM18 5v14"/>',
  refresh: '<path d="M20 8a8 8 0 0 0-14-2L3 9m0-5v5h5m-4 7a8 8 0 0 0 14 2l3-3m0 5v-5h-5"/>',
  filter: '<path d="M3 6h18M6 12h12m-9 6h6"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 6.5V12l4 2"/>',
  right: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  back: '<path d="M20 12H5m6-6-6 6 6 6"/>',
  external: '<path d="M13 4h7v7m0-7L9 15m-1-9H4v14h14v-4"/>',
  weather: '<path d="M5 17a4 4 0 0 1 1-7.8 5 5 0 0 1 9.3.2A4 4 0 1 1 17 17H5ZM16 4V2m4 4 1.5-1.5M21 10h2"/>',
  pin: '<path d="M18 9c0 4-6 10-6 10S6 13 6 9a6 6 0 1 1 12 0Z"/><circle cx="12" cy="9" r="2"/>',
  layers: '<path d="m3 8 9-5 9 5-9 5-9-5Zm0 5 9 5 9-5M3 17l9 5 9-5"/>',
  file: '<path d="M6 3h8l4 4v14H6zM14 3v5h4m-9 5h6m-6 4h6"/>',
  link: '<path d="m9 15 6-6m-5-1 2-2a4 4 0 0 1 6 6l-2 2m-2 2-2 2a4 4 0 0 1-6-6l2-2"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  warn: '<path d="m12 3 10 17H2zM12 9v5m0 2v1"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  control: '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  terminal: '<path d="m5 7 5 5-5 5m8 0h6"/>',
  arrow: '<path d="m9 5 7 7-7 7"/>',
};
export function icon(name, size = 18) {
  return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${drawings[name] || ''}</svg>`;
}
const tones = {'Paper buy': 'buy', Pass: 'pass', Watch: 'watch', Held: 'held', Settled: 'settled'};
export function badge(decision) {
  const label = text(decision) || 'Unclassified';
  return `<span class="decision-badge ${tones[label] || 'pass'}"><i aria-hidden="true"></i>${esc(label)}</span>`;
}
export function field(label, value, small = '') {
  return `<div><dt>${esc(label)}</dt><dd>${esc(value ?? dash)}</dd>${small ? `<dd class="field-note">${esc(small)}</dd>` : ''}</div>`;
}
export function button(label, action, options = {}) {
  const {value, symbol, kind = 'secondary', disabled, key, title, extraClass = ''} = options;
  return `<button type="button" class="btn ${kind} ${extraClass}" data-action="${esc(action)}"${title ? ` title="${esc(title)}"` : ''}${value === undefined ? '' : ` data-value="${esc(value)}"`}${key ? ` data-key="${esc(key)}"` : ''}${disabled ? ' disabled' : ''}>${symbol ? icon(symbol, 16) : ''}${esc(label)}</button>`;
}
export function scanButton(state, ui) {
  const latest = state.health?.scan;
  const running = latest?.status === 'running' || state.health?.scanning === true;
  const requested = ui.scanReceipt && arr(state.health?.recent_scans).find(item => item.id === ui.scanReceipt && item.finished_at);
  const scan = latest?.id ? latest : requested || latest;
  const terminal = ['complete','partial','failed'].includes(scan?.status);
  const finished = instant(scan?.finished_at)?.getTime();
  const secondsAgo = (Date.now() - finished) / 1000;
  const showResult = terminal && (ui.scanReceipt === scan.id || (secondsAgo >= 0 && secondsAgo < 12));
  let label = 'Run scan', symbol = 'refresh';
  if (running || ui.scanSubmitting) label = running ? 'Scanning…' : 'Starting scan…';
  else if (showResult) {
    label = {complete:'Scan complete',partial:'Scan complete with gaps',failed:'Scan failed'}[scan.status];
    symbol = scan.status === 'complete' ? 'check' : 'warn';
  }
  const title = running ? 'Collecting current weather and market data' : showResult ?
    `${scan.detail || label} · ${stamp(scan.finished_at)}. Click to run another scan.` : 'Run a new forward paper scan';
  return button(label,'scan',{symbol,title,key:'scan-action',extraClass:running || ui.scanSubmitting ? 'scan-in-progress':'',
    disabled:ui.locked || running || ui.scanSubmitting || state.health?.online === false});
}
export function emptyBox(heading, description, symbol = 'layers') {
  return `<div class="empty">${icon(symbol, 24)}<p class="empty-title">${esc(heading)}</p><p>${esc(description)}</p></div>`;
}
export function headline(title, context, controls = '') {
  return `<div class="page-head" data-key="page-head"><div><h1 id="view-title" tabindex="-1">${esc(title)}</h1><p>${context}</p></div><div class="actions">${controls}</div></div>`;
}
export function tone(cents) {
  const n = num(cents);
  return n > 0 ? 'positive' : n < 0 ? 'negative' : '';
}
export function notesFor(state, id) {
  return arr(state.research?.notes).filter(note => note?.market_id === id);
}
