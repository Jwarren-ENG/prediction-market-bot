import {button, connectionUrl, emptyBox, esc, headline, icon, reloadDisplayTimeZone, secureWorker, setDisplayTimeZone, stamp, wallClock} from './common.js';
import {detailTabs, researchView} from './research.js';
import {defaultQuoteRange, marketsView, paperSwitch, quoteRanges, quoteRows} from './markets.js';
import {activityView, connectionForm, modeLabel, portfolioView, workerView} from './views.js';
import {bitcoinRanges, bitcoinView, optionsView} from './bitcoin.js';

const links = ['Markets', 'Research', 'Portfolio', 'Activity', 'Worker'];
const titles = {Markets:'Markets', Research:'Research inbox', Portfolio:'Portfolio', Activity:'Activity', Worker:'Worker'};
const narrow = window.matchMedia('(max-width: 879px)');
const desks = {weather:'Central Park weather', bitcoin:'Bitcoin', options:'Options'};
function savedDesk() {
  try {
    const value = localStorage.getItem('pmb.tradingWorkspace');
    return Object.hasOwn(desks,value) ? value : 'weather';
  } catch { return 'weather'; }
}
const ui = {
  desk:savedDesk(), activityHours:24, btcRange:1440,
  view:'Research', selected:null, filter:'All decisions', tab:'Decision', mobileDetail:false,
  quoteSelected:null, quoteMobileDetail:false, quoteRange:defaultQuoteRange, paperSwitchPending:false,
  activityFilter:'All events', provider:null, connectionError:'', busy:false,
  connected:false, contactError:'', actionError:'', success:'', locked:true, healthTone:'',
};
let state = null;
let choseView = false;
let order = [];
let quoteOrder = [];
let requestVersion = 0;
let pendingRead = null;
let pendingToggle = null;
let token = '';
let successTimer;
const el = id => document.getElementById(id);
function tickClock() {
  const node = el('desk-clock');
  if (!node) return;
  const now = new Date();
  node.dateTime = now.toISOString();
  node.textContent = wallClock(now);
}
function readToken() {
  const url = new URL(location.href);
  const fragment = new URLSearchParams(url.hash.slice(1));
  if (fragment.has('token')) {
    const supplied = fragment.get('token');
    history.replaceState(null,'',url.pathname + url.search);
    if (secureWorker(url) && supplied) {
      token = supplied;
      try { sessionStorage.setItem('pmb.worker.token', token); } catch { }
      return;
    }
  }
  if (secureWorker(url)) {
    try { token = sessionStorage.getItem('pmb.worker.token') || ''; } catch { }
  }
}

// Patch only changed nodes. Controls, row nodes and expanded details stay in place on heartbeat updates.
function key(node) {
  return node.nodeType === 1 ? node.getAttribute('data-key') || node.id || '' : '';
}
function sameType(a,b) {
  return a && a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b);
}
function patchNode(node,next) {
  if (node.nodeType !== 1) {
    if (node.nodeValue !== next.nodeValue) node.nodeValue = next.nodeValue;
    return;
  }
  for (const attr of [...node.attributes]) {
    if (node.localName === 'details' && attr.name === 'open') continue;
    if (!next.hasAttribute(attr.name)) node.removeAttribute(attr.name);
  }
  for (const {name,value} of next.attributes) {
    if (node.getAttribute(name) !== value) node.setAttribute(name,value);
  }
  if (['market-permission','research-auto'].includes(node.id)) {
    node.checked = pendingToggle?.id === node.id ? pendingToggle.value : next.checked;
  }
  patchChildren(node,next);
  if (node.localName === 'select' && node.value !== next.value) node.value = next.value;
}
function patchChildren(parent,next) {
  const keyed = new Map([...parent.childNodes].filter(n => key(n)).map(n => [key(n), n]));
  let cursor = parent.firstChild;
  for (const wanted of next.childNodes) {
    let node = sameType(cursor,wanted) ? cursor : keyed.get(key(wanted));
    if (!sameType(node,wanted)) {
      node = wanted.cloneNode(true);
      parent.insertBefore(node,cursor);
    } else {
      if (node !== cursor) parent.insertBefore(node,cursor);
      patchNode(node,wanted);
    }
    cursor = node.nextSibling;
  }
  while (cursor) {
    const after = cursor.nextSibling;
    cursor.remove();
    cursor = after;
  }
}
function paint(id, html) {
  const template = document.createElement('template');
  template.innerHTML = html;
  patchChildren(el(id),template.content);
}
function selectData(data) {
  if (!data || !['replay','forward'].includes(data.mode) || !Array.isArray(data.markets) || !data.portfolio || !data.health) {
    throw new Error('The worker returned an incomplete account snapshot.');
  }
  const changedMode = state && state.mode !== data.mode;
  if (!state && !choseView && data.mode === 'forward' && data.settings?.allow_market && data.quotes) ui.view = 'Markets';
  if (changedMode) {
    order = [];
    Object.assign(ui, {selected:null, filter:'All decisions', mobileDetail:false, tab:'Decision', activityFilter:'All events'});
  }
  const ids = data.markets.map(m => m.id);
  const listedQuotes = data.settings?.allow_market !== false && Array.isArray(data.quotes?.markets) ? data.quotes.markets : [];
  const quoteIds = listedQuotes.map(m => m?.id).filter(Boolean);
  const quoteIdsNow = new Set(quoteIds);
  const retainedQuotes = new Set(quoteOrder.filter(id => quoteIdsNow.has(id)));
  quoteOrder = [...retainedQuotes,...quoteIds.filter(id => !retainedQuotes.has(id))];
  if (ui.activityFilter !== 'All events' && !data.activity?.some(e => e.kind === ui.activityFilter)) ui.activityFilter = 'All events';
  const current = new Set(ids);
  const retained = new Set(order.filter(id => current.has(id)));
  order = [...retained, ...ids.filter(id => !retained.has(id))];
  if (!current.has(ui.selected)) {
    ui.selected = data.markets.find(m => ui.filter === 'All decisions' || m.decision === ui.filter)?.id ?? null;
    ui.tab = 'Decision';
    if (!ui.selected) ui.mobileDetail = false;
  }
  state = data;
  if (ui.scanReceipt && ui.announcedScan !== ui.scanReceipt) {
    const scan = [data.health.scan,...(data.health.recent_scans || [])].find(s => s?.id === ui.scanReceipt && s.finished_at);
    if (scan) {
      announce(scan.status === 'complete' ? 'Scan complete.' : scan.detail || 'The scan needs attention.');
      ui.announcedScan = scan.id;
    }
  }
}
function rows() {
  const byId = new Map(state.markets.map(m => [m.id,m]));
  return order.map(id => byId.get(id)).filter(m => m && (ui.filter === 'All decisions' || m.decision === ui.filter));
}
function announce(message) {
  el('announcement').textContent = message;
}
function notices() {
  const messages = [];
  if (ui.contactError) messages.push(`<div class="notice error" data-key="connection" role="alert">${icon('warn',18)}<span>${esc(ui.contactError)}${state ? ' Showing the last received snapshot.' : ''}</span>${button('Retry connection','retry',{kind:'inline'})}</div>`);
  if (ui.actionError) messages.push(`<div class="notice error" data-key="action" role="alert">${icon('warn',18)}<span>${esc(ui.actionError)}</span>${button('Dismiss','dismiss',{kind:'inline'})}</div>`);
  if (ui.desk === 'weather' && state?.paused) messages.push(`<div class="notice warn" data-key="pause">${icon('pause',16)}<span><strong>Paper entries paused.</strong> ${esc(state.risk_pause_reason || 'Existing paper positions remain in the portfolio.')}</span></div>`);
  if (ui.success) messages.push(`<div class="notice success" data-key="success" role="status">${icon('check',16)}<span>${esc(ui.success)}</span>${button('Dismiss','dismiss',{kind:'inline'})}</div>`);
  return messages.join('');
}
function render() {
  const previous = document.activeElement;
  ui.locked = ui.busy || !ui.connected;
  const late = state?.health?.heartbeat && Date.now() - new Date(state.health.heartbeat).getTime() > 180000;
  ui.healthTone = !ui.connected ? 'offline' : late ? 'late' : 'connected';
  const weather = ui.desk === 'weather';
  el('strategy-desk').value = ui.desk;
  el('navigation').hidden = !weather;
  paint('navigation',weather ? links.map(name => `<button type="button" data-action="view" data-value="${name}" data-key="nav-${name}"${ui.view === name ? ' aria-current="page"' : ''}>${icon(name.toLowerCase(),17)}<span>${name}</span></button>`).join('') : '');
  document.title = `${weather ? titles[ui.view] : desks[ui.desk]} · Prediction Market Bot`;
  paint('notices',notices());
  let html;
  if (ui.desk === 'options') html = optionsView(state,ui);
  else if (!state) {
    const label = ui.contactError ? 'Connect to a research worker' : 'Connecting to the research worker';
    const switchControl = weather && ['Markets','Research'].includes(ui.view) ? paperSwitch(null,ui) : '';
    html = `${headline(label,'Private paper workspace',switchControl)}<div class="connection-panel panel"><div class="panel-body">${ui.contactError ? connectionForm(ui) : emptyBox('Waiting for a snapshot','The worker owns the research feed and the paper account.','worker')}</div></div>`;
  } else if (ui.desk === 'bitcoin') html = bitcoinView(state,ui);
  else if (ui.view === 'Markets') {
    const rank = new Map(quoteOrder.map((id,i)=>[id,i]));
    const quotes = quoteRows(state,ui.connected).sort((a,b)=>rank.get(a.id)-rank.get(b.id));
    if (!quotes.some(m => m.id === ui.quoteSelected)) {
      ui.quoteSelected = (quotes.find(m => m.bid !== null && m.ask !== null) || quotes.find(m => m.bid !== null || m.ask !== null) || quotes[0])?.id ?? null;
      if (!ui.quoteSelected) ui.quoteMobileDetail = false;
    }
    html = marketsView(state,ui,quotes);
  } else if (ui.view === 'Research') {
    html = researchView(state,ui,rows(),state.markets.find(m => m.id === ui.selected));
  } else if (ui.view === 'Portfolio') html = portfolioView(state);
  else if (ui.view === 'Activity') html = activityView(state,ui);
  else html = workerView(state,ui);
  paint('workspace',html);
  const status = !state && !ui.contactError ? 'Connecting' : !ui.connected ? 'Worker disconnected' : late ? 'Heartbeat overdue' : 'Worker connected';
  paint('statusbar',`<div class="status-inner"><span class="connection-status"><i class="state-symbol ${ui.healthTone}" aria-hidden="true"></i>${status}</span>
    ${state ? `<span class="footer-mode">${weather ? esc(modeLabel(state)) + (state.paused ? ' · Entries paused' : '') : ui.desk === 'bitcoin' ? 'Bitcoin paper' : 'Options · Coming soon'}</span><span class="footer-heartbeat">Heartbeat <time>${esc(stamp(state.health.heartbeat))}</time></span>` : ''}</div>`);
  if (previous && previous !== document.body && document.activeElement === document.body) {
    const fallback = previous.isConnected ? previous : ui.view === 'Markets' ? document.querySelector('.quote-link[aria-pressed="true"]') || el('view-title') :
      ui.view === 'Research' && el('decision-filter') ? el('decision-filter') : el('view-title');
    fallback?.focus({preventScroll:true});
  }
}
async function api(path, options = {}) {
  const controller = options.controller || new AbortController();
  const timer = setTimeout(() => controller.abort(), options.method === 'POST' ? 90000 : 14000);
  const headers = {Accept:'application/json'};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body) headers['Content-Type'] = 'application/json';
  try {
    // WebKit sends a null Origin on same-origin POSTs with a no-referrer request policy.
    const response = await fetch(path,{method:options.method || 'GET',headers,body:options.body,signal:controller.signal,
      credentials:'same-origin',mode:'same-origin',redirect:'error',cache:'no-store',referrerPolicy:'strict-origin'});
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const message = typeof body?.error === 'string' ? body.error.slice(0,300) : `Worker returned HTTP ${response.status}.`;
      throw new Error(message);
    }
    if (body === null) throw new Error('The worker response could not be read.');
    return body;
  } finally { clearTimeout(timer); }
}
function failure(err) {
  if (err?.name === 'AbortError' || err?.name === 'TimeoutError') return 'The worker request timed out.';
  if (err instanceof TypeError) return 'The worker could not be reached.';
  return err?.message || 'The worker returned an error.';
}
async function refresh(force = false) {
  if (!secureWorker(new URL(location.href))) {
    ui.contactError = 'Open the connection link on HTTPS or a loopback worker address.';
    render();
    return false;
  }
  if ((pendingRead || ui.busy) && !force) return false;
  const version = ++requestVersion;
  pendingRead?.abort();
  const controller = new AbortController();
  pendingRead = controller;
  try {
    const query = ui.desk === 'bitcoin' ? '?workspace=bitcoin' : ui.desk === 'weather' && ui.view === 'Activity' ? `?activity_hours=${ui.activityHours}` : '';
    const next = await api('/api/state'+query,{controller});
    if (version !== requestVersion) return false;
    selectData(next);
    ui.contactError = '';
    ui.connected = true;
    return true;
  } catch (err) {
    if (version === requestVersion) { ui.connected = false; ui.contactError = failure(err); }
    return false;
  } finally {
    if (version === requestVersion) { pendingRead = null; render(); }
  }
}
const confirmations = {
  'btc-feed':'Bitcoin market data preference saved.', 'btc-auto':'Bitcoin paper trading preference saved.',
  scan:'Forward scan requested.', replay:'Offline replay processed.', pause:'Paper entries paused.', resume:'Manual pause cleared.',
  mode:'Operating mode updated.', permission:'Market data permission updated.', research:'Research requested. New notes appear in Evidence.',
  configure_research:'Analyst settings saved.', research_auto:'Automatic research preference updated.',
};
async function action(name, value) {
  if (ui.locked || !Object.hasOwn(confirmations,name)) return false;
  ++requestVersion;
  pendingRead?.abort();
  pendingRead = null;
  ui.busy = true;
  if (name === 'scan') {
    ui.scanSubmitting = true;
    ui.scanReceipt = null;
    ui.scanRequestedAt = Date.now();
  }
  if (name === 'permission' || name === 'research_auto') {
    pendingToggle = {id:name === 'permission' ? 'market-permission' : 'research-auto', value};
  }
  ui.success = '';
  ui.actionError = '';
  render();
  let accepted = false;
  try {
    const receipt = await api('/api/action',{method:'POST',body:JSON.stringify({action:name,...(value === undefined ? {} : {value})})});
    if (name === 'scan') { ui.scanReceipt = receipt.scan_id || null; ui.scanSubmitting = false; }
    accepted = true;
    if (await refresh(true)) {
      if (name !== 'scan') {
        const paused = value?.source === 'markets' ? !state.paper_automation?.enabled : state.paused;
        ui.success = name === 'resume' ? paused ? 'The worker rechecked risk. Paper entries remain paused.' : 'Paper entries resumed.' : confirmations[name];
      }
      clearTimeout(successTimer);
      successTimer = setTimeout(() => {ui.success = '';render();},9000);
    } else ui.actionError = 'The worker accepted the change. Refresh the account snapshot to verify it.';
  } catch (err) {
    ui.actionError = failure(err);
    if (err instanceof TypeError || err?.name === 'AbortError') ui.actionError += ' Check the current worker state before retrying.';
    await refresh(true);
  } finally { ui.busy = false;ui.scanSubmitting = false;pendingToggle = null;render(); }
  return accepted;
}
function navigate(view) {
  if (!links.includes(view)) return;
  choseView = true;
  if (view === 'Research' && state) order = state.markets.map(m => m.id);
  ui.view = view;
  ui.connectionError = '';
  ui.mobileDetail = false;
  ui.quoteMobileDetail = false;
  render();
  window.scrollTo(0,0);
  el('view-title')?.focus({preventScroll:true});
  if (view === 'Activity') void refresh(true);
}
function choose(id, open = true) {
  if (!state?.markets.some(m => m.id === id)) return;
  const changed = ui.selected !== id;
  ui.selected = id;
  if (changed) ui.tab = 'Decision';
  if (narrow.matches && open) ui.mobileDetail = true;
  render();
  if (narrow.matches && open) {
    el('contract-title')?.focus({preventScroll:true});
    document.querySelector('.detail-panel')?.scrollIntoView({block:'start'});
  } else if (changed) announce('Selected contract updated.');
}
function chooseQuote(id, open = true) {
  if (!state?.quotes?.markets?.some(m => m.id === id)) return;
  const changed = ui.quoteSelected !== id;
  ui.quoteSelected = id;
  if (narrow.matches && open) ui.quoteMobileDetail = true;
  render();
  if (narrow.matches && open) {
    el('quote-title')?.focus({preventScroll:true});
    document.querySelector('.market-detail')?.scrollIntoView({block:'start'});
  } else if (changed) announce('Selected public quote updated.');
}
function showTab(tab, focus = false) {
  if (!detailTabs.includes(tab)) return;
  ui.tab = tab;
  render();
  if (focus) {
    const active = el(`tab-${detailTabs.indexOf(tab)}`);
    active?.focus({preventScroll:true});
    active?.scrollIntoView({block:'nearest'});
  }
}
function editConnection(raw) {
  try {
    const url = connectionUrl(raw.trim());
    ui.connectionError = '';
    if (url.origin === location.origin) {
      if (url.hash) {
        history.replaceState(null,'',location.pathname + url.hash);
        readToken();
      }
      el('connection-form')?.reset();
      navigate('Research');
      void refresh(true);
    } else { window.location.assign(url.href); }
  } catch (err) {
    ui.connectionError = failure(err);
    render();
    el('worker-url')?.focus();
  }
}
document.addEventListener('click',event => {
  const clickable = event.target.closest('button[data-action]');
  if (!clickable) {
    const quoteRow = event.target.closest('tr[data-quote-market]');
    if (quoteRow && !window.getSelection()?.toString()) {
      quoteRow.querySelector('.quote-link')?.focus({preventScroll:true});
      chooseQuote(quoteRow.dataset.quoteMarket);
      return;
    }
    const row = event.target.closest('tr[data-market]');
    if (row && !window.getSelection()?.toString()) {
      row.querySelector('.contract-link')?.focus({preventScroll:true});
      choose(row.dataset.market);
    }
    return;
  }
  if (clickable.disabled) return;
  const {action:name,value} = clickable.dataset;
  if (name === 'view') navigate(value);
  else if (name === 'btc-range' && bitcoinRanges.has(Number(value))) { ui.btcRange = Number(value);render(); }
  else if (['btc-feed','btc-auto'].includes(name) && ['true','false'].includes(value)) void action(name,value === 'true');
  else if (name === 'activity-range' && [24,48,168].includes(Number(value))) {
    ui.activityHours = Number(value);render();void refresh(true);
  }
  else if (name === 'paper-switch') {
    const a = state?.paper_automation;
    if (!ui.connected || ui.locked || !a?.available || a.risk_pause_reason || typeof a.enabled !== 'boolean') return;
    const desired = a.enabled ? 'pause' : 'resume';
    if (value !== desired) return;
    ui.paperSwitchPending = true;
    void action(desired,{source:'markets'}).finally(() => { ui.paperSwitchPending = false; render(); });
  }
  else if (name === 'select-quote') chooseQuote(value);
  else if (name === 'quote-range' && quoteRanges.has(Number(value))) { ui.quoteRange = Number(value);render(); }
  else if (name === 'quote-list') {
    ui.quoteMobileDetail = false;render();
    document.querySelector('.quote-link[aria-pressed="true"]')?.focus();
  } else if (name === 'market-settings') {
    navigate('Worker');
    el('automation-title')?.focus();
  } else if (name === 'quote-research') {
    if (state?.mode !== 'forward' || !state.markets.some(m => m.id === value)) return;
    ui.filter = 'All decisions';
    navigate('Research');
    choose(value);
    el('contract-title')?.focus({preventScroll:true});
  }
  else if (name === 'retry') void refresh(true);
  else if (name === 'dismiss') { ui.success = '';ui.actionError = '';render(); }
  else if (name === 'select') choose(value);
  else if (name === 'position' || name === 'case') {
    ui.filter = 'All decisions';
    navigate('Research');
    choose(value);
    if (name === 'case') showTab('Settlement',true);
  } else if (name === 'list') {
    ui.mobileDetail = false; render();
    document.querySelector('.contract-link[aria-pressed="true"]')?.focus();
  } else if (name === 'evidence') showTab('Evidence',true);
  else if (name === 'tab') showTab(value);
  else if (name !== 'mode' || value !== state?.mode) void action(name,value);
});
document.addEventListener('change',event => {
  const field = event.target;
  switch (field.id) {
    case 'strategy-desk':
      if (!Object.hasOwn(desks,field.value)) break;
      ui.desk = field.value;
      try { localStorage.setItem('pmb.tradingWorkspace',ui.desk); } catch {}
      ui.actionError = '';ui.success = '';
      render();announce(`${desks[ui.desk]} workspace`);window.scrollTo(0,0);void refresh(true);break;
    case 'decision-filter':
      ui.filter = field.value;
      order = state.markets.map(m => m.id);
      if (!rows().some(m => m.id === ui.selected)) { ui.selected = rows()[0]?.id ?? null;ui.tab = 'Decision'; }
      render(); break;
    case 'activity-filter': ui.activityFilter = field.value;render();break;
    case 'display-zone':
      if (setDisplayTimeZone(field.value)) { tickClock(); render(); }
      break;
    case 'provider-picker': ui.provider = field.value;render();break;
    case 'market-permission': void action('permission',field.checked);break;
    case 'research-auto': void action('research_auto',field.checked);break;
  }
});
document.addEventListener('submit',async event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === 'connection-form') { editConnection(form.elements.url.value);return; }
  if (form.id !== 'provider-form' || ui.locked) return;
  const data = new FormData(form);
  const value = {provider:el('provider-picker').value,enabled:data.has('enabled'),model:String(data.get('model') || '').trim(),
    max_calls_per_day:Number(data.get('budget'))};
  if (data.get('endpoint')) value.endpoint = String(data.get('endpoint')).trim();
  if (data.get('key')) value.key = String(data.get('key')).trim();
  const applied = await action('configure_research',value);
  if (applied && form.isConnected) { form.reset();form.elements.key.value = ''; }
});
document.addEventListener('keydown',event => {
  const target = event.target;
  if (target.matches('[role="tab"]') && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
    event.preventDefault();
    const pos = detailTabs.indexOf(ui.tab);
    const idx = event.key === 'Home' ? 0 : event.key === 'End' ? detailTabs.length - 1 : (pos + (event.key === 'ArrowRight' ? 1 : -1) + detailTabs.length) % detailTabs.length;
    showTab(detailTabs[idx],true);
  }
  if (target.matches('.contract-link,.quote-link') && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
    event.preventDefault();
    const isQuote = target.classList.contains('quote-link');
    const controls = [...document.querySelectorAll(isQuote ? '.quote-link' : '.contract-link')];
    const pos = controls.indexOf(target);
    const idx = event.key === 'Home' ? 0 : event.key === 'End' ? controls.length - 1 : Math.min(controls.length-1,Math.max(0,pos + (event.key === 'ArrowDown' ? 1 : -1)));
    controls[idx]?.focus();
    if (!narrow.matches) (isQuote ? chooseQuote : choose)(controls[idx]?.dataset.value,false);
  }
});
document.addEventListener('visibilitychange',() => { if (!document.hidden) void refresh(); });
window.addEventListener('focus',() => { void refresh(); });
window.addEventListener('hashchange',() => {
  if (new URLSearchParams(location.hash.slice(1)).has('token')) {readToken();void refresh(true);}
});
window.addEventListener('storage',event => {
  if (event.key === 'pmb.displayTimeZone') {reloadDisplayTimeZone();tickClock();render();}
});
tickClock();
readToken();
render();
void refresh();
let lastRefresh = Date.now();
setInterval(() => {
  if (document.hidden) return;
  tickClock();
  const awaitingReceipt = ui.scanReceipt && ui.announcedScan !== ui.scanReceipt && Date.now() - ui.scanRequestedAt < 300000;
  const interval = ui.scanSubmitting || state?.health?.scanning || awaitingReceipt ? 1000 : 5000;
  if (Date.now() - lastRefresh >= interval) { lastRefresh = Date.now(); void refresh(); }
},1000);
