import {arr, button, count, dash, displayTimeZone, esc, field, headline, human, instant, money, num, price, sourceLink, stamp, timeZoneChoices} from './common.js';
import {bitcoinAccount, bitcoinSignal} from './bitcoin.js';
import {autoSwitch, brokerFor, contractAuto, feedFor, riskForm, venues} from './bitcoin-trading.js';
import {trialPerformance} from './bitcoin-performance.js';

const products=Object.keys(venues);
const panel=(title,body,control='')=>`<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2>${control}</div><div class="panel-body">${body}</div></section>`;
const note=t=>`<p class="btc-note">${esc(t)}</p>`;
const tab=(view)=>button('View '+view.toLowerCase(),'btc-view',{value:view,kind:'inline'});
const strategy = (b,m) => typeof m?.signal==='string' ? m.signal : m?.signal?.reason || b?.signal?.reason || '';
function table(headers,rows,label) {
  return `<div class="table-scroll" role="region" tabindex="0" aria-label="${esc(label)}"><table class="data-table btc-account-table"><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}
const cells=(first,values)=>`<tr><th scope="row">${esc(first)}</th>${values.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`;
function sourceStatus(feed) {
  const name={live:'Receiving live quotes',partial:'Partial quote data',disabled:'Public data off',offline:'Worker offline',unavailable:'Feed unavailable',restricted:'Venue access restricted',empty:'No current listing',waiting:'Awaiting public data',stale:'Quotes delayed',rate_limited:'Venue rate limit',error:'Venue request failed'};
  return name[feed?.status] || feed?.status || 'Waiting for worker';
}
function sourceControl(state,ui,venue,explicitLabel) {
  const spot=venue==='spot',feed=spot?state.bitcoin:feedFor(state,venue),a=spot?'btc-feed':venue==='kalshi'?'btc-kalshi-feed':'btc-poly-feed';
  return autoSwitch(explicitLabel,a,ui.connected && state.health?.online!==false?feed?.enabled:null,ui.locked || state.health?.online===false);
}
function spotAuto(state,ui) {
  const m=state.bitcoin||{};
  return autoSwitch('Paper auto trading','btc-auto',ui.connected?m.auto_enabled:null,ui.locked || (!m.auto_enabled && (!m.enabled || !m.online)));
}
function recordedResearch(state,venue) {
  const b=brokerFor(state,venue)||{},f=feedFor(state,venue),m=f.current,record=arr(b.markets).find(r=>r.market_id===m?.id);
  const checked=arr(m?.history).filter(p=>p?.kind!=='gap' && [p?.yes_bid_cents,p?.yes_ask_cents,p?.no_bid_cents,p?.no_ask_cents].some(v=>num(v)!==null)).length;
  const verdict=strategy(b,record) || record?.reason || f.message || 'Waiting for enough public book observations.';
  const performance=b.performance||{},perf=performance.auto_only;
  const recordText=perf?.closed_windows>0 ? `${count(perf.profitable_windows)} profitable of ${count(perf.closed_windows)} completed automatic-only windows (${count(perf.win_rate_pct)}%).` :
    'No completed automatic-only windows with a measured result.';
  return panel(venues[venue].label,`<p class="btc-research-reading">${esc(verdict)}</p><dl class="field-list">
    ${field('Current contract',m?.id||dash)}${field('Book observations',m?count(checked):dash)}
    ${field('Latest receipt',stamp(m?.observed_at))}${field('Order eligibility',record ? record.tradable?'Eligible':record.reason||'Waiting':'Waiting for broker')}
    ${field('Fees paid today',money(b.daily?.fees_cents))}${field('Realized today',money(b.daily?.realized_cents,true))}</dl>
    ${typeof b.rule==='string'?note(b.rule):''}<div class="btc-study-results"><h3>Recorded automation results</h3><p>${esc(recordText)}</p>
    ${perf?.closed_windows>0?`<p>Net after fees ${money(perf.net_cents,true)}. ${count(perf.loss_windows)} losing · ${count(perf.flat_windows)} flat.</p>`:''}</div>
    <dl class="field-list">${field('All completed traded windows',count(performance.closed_windows))}${field('Their net after fees',money(performance.net_cents,true))}${field('Worst recorded drawdown',money(performance.max_drawdown_cents))}</dl>
    ${m?.rules_url?`<div class="btc-source-links">${sourceLink('Venue contract rules',m.rules_url)}</div>`:''}`);
}
function portfolio(state) {
  const balances=products.map(v=>{
    const p=brokerFor(state,v)?.portfolio||{};
    return cells(venues[v].label,[money(p.cash_cents),money(p.equity_cents),money(p.open_cost_cents),money(p.realized_cents,true),money(p.net_gain_cents,true),count(p.filled_orders)]);
  });
  const positions=products.flatMap(v=>arr(brokerFor(state,v)?.positions).filter(p=>Number(p.qty)>0).map(p=>
    cells(venues[v].label,[p.title||p.market_id,String(p.side).toUpperCase(),count(p.qty),money(p.cost_cents),money(p.mark_value_cents),human(p.mark_status)||dash,stamp(p.close_at)])));
  return `<div class="btc-workspace">${panel('Contract accounts',table(['Paper account','Cash','Marked equity','Open cost','Realized','Net gain / loss','Fills'],balances,'Independent Bitcoin contract accounts')+note('Each venue and interval uses its own ledger. A missing mark leaves equity and net gain unavailable.'))}
    ${panel('Open contract positions',positions.length ? table(['Account','Contract','Outcome','Held','Cost','Marked value','Mark status','Close'],positions,'Open Bitcoin contract positions') :
      note('There are no open Bitcoin contract positions.'))}${trialPerformance(state)}${bitcoinAccount(state.bitcoin||{})}</div>`;
}
function activities(state) {
  const entries=[];
  for(const v of products) {
    const b=brokerFor(state,v)||{},events=arr(b.activity),fills=arr(b.fills);
    const ids=new Set();
    for(const item of [...events,...fills]) {
      const e={...item.data,...item};
      const id=e.client_id || e.id, when=instant(e.at);
      if (!when || (id!=null && ids.has(String(id)))) continue;
      if (id!=null) ids.add(String(id));
      const qty=e.qty==null?'':`${count(e.qty)} ${String(e.side||'').toUpperCase()}`;
      const detail=e.message || e.reason || `${human(e.action)||'Paper order'} ${qty}${num(e.price_cents)!==null?' at '+price(e.price_cents):''}.`;
      const origin=e.origin || (['auto','manual'].includes(e.source)?e.source:null);
      entries.push({product:v,when:when.getTime(),kind:human(e.status||e.kind||e.type)||'Paper receipt',origin:human(origin)||dash,detail,fee:money(e.fee_cents),id:id??''});
    }
  }
  for (const e of arr(state.bitcoin?.activity)) {
    const when=instant(e.at);
    if (when) entries.push({product:'spot',when:when.getTime(),kind:human(e.kind)||'Spot receipt',origin:e.account==='hold'?'Benchmark':human(e.origin)||'Strategy',detail:e.message||'',fee:money(e.fee_cents),id:e.client_id||e.id||''});
  }
  return entries.sort((a,b)=>b.when-a.when);
}
function activity(state,ui) {
  const choices=[['all','All Bitcoin products'],...products.map(v=>[v,venues[v].label]),['spot','Bitstamp spot']];
  const active=choices.some(([v])=>v===ui.btcActivityFilter)?ui.btcActivityFilter:'all';
  const select=`<label class="btc-filter">Product<select id="btc-activity-filter">${choices.map(([v,l])=>`<option value="${v}"${v===active?' selected':''}>${esc(l)}</option>`).join('')}</select></label>`;
  const rows=activities(state).filter(e=>active==='all'||e.product===active).map(e=>
    cells(stamp(e.when),[e.product==='spot'?'Bitstamp spot':venues[e.product].label,e.kind,e.origin,e.detail,e.fee,e.id||dash]));
  return `<div class="btc-workspace">${panel('Recent Bitcoin receipts',rows.length?table(['Time','Paper account','Receipt','Origin','Details','Fee','Reference'],rows,'Bitcoin paper activity'):note('No receipts have been recorded for the selected product.'),select)}</div>`;
}
function worker(state,ui) {
  const spot=state.bitcoin||{},h=state.health||{};
  const sources=products.map(v=>{
    const f=feedFor(state,v);return cells(venues[v].label,[sourceStatus(f),stamp(f.last_checked_at),stamp(f.current?.observed_at||f.last_success_at),f.message||dash]);
  });
  sources.push(cells('Bitstamp spot',[spot.status||dash,stamp(spot.last_checked_at),stamp(spot.quote?.observed_at),spot.enabled?'BTC/USD order book and completed closes':'Public collection off']));
  const sourceControls=`<div class="btc-source-controls">${sourceControl(state,ui,'kalshi','Kalshi public data')}${sourceControl(state,ui,'polymarket_us','Polymarket US public data')}${sourceControl(state,ui,'spot','Bitstamp public data')}</div>`;
  const sourceNote='Kalshi access also serves Central Park weather. Its saved automation setting applies when data returns. Polymarket US collection serves both its five-minute and hourly books.';
  const risk=products.map(v=>{
    const b=brokerFor(state,v);
    return panel(venues[v].label,`${b?.gate_reason?note(b.gate_reason):''}${b?.started_at?note('Trial started '+stamp(b.started_at)+'.'):''}${riskForm(state,ui,v)}`,contractAuto(state,ui,v));
  }).join('');
  const zone=`<label class="zone-setting" for="display-zone">Time zone<select id="display-zone">${timeZoneChoices.map(([id,label])=>`<option value="${esc(id)}"${id===displayTimeZone()?' selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
  return `<div class="btc-workspace">${panel('Public data sources',sourceControls+note(sourceNote)+table(['Product','Source status','Last checked','Last receipt','Details'],sources,'Bitcoin data source health'))}
    <div class="btc-worker-grid">${risk}${panel('Bitstamp spot',`<dl class="field-list">${field('Maximum entry cost',money(spot.max_entry_cents??50000))}${field('Modeled fee','0.60% per side')}${field('Price allowance','0.05% per side')}${field('Collection status',spot.status||dash)}</dl>${note('A successful manual spot order pauses the daily strategy until you turn it on again.')}`,spotAuto(state,ui))}
    ${panel('Worker health',`<dl class="field-list">${field('Version',state.version||dash)}${field('Worker',h.online===false?'Offline':h.online===true?'Online':dash)}${field('Heartbeat',stamp(h.heartbeat))}</dl>`)}
    ${panel('Display settings',zone)}</div></div>`;
}
export function bitcoinAccountView(state,ui) {
  const view=ui.btcView;
  let body;
  if (view==='Research') body=`<div class="btc-worker-grid">${products.map(v=>recordedResearch(state,v)).join('')}${bitcoinSignal(state.bitcoin||{})}</div><div class="btc-source-links">${sourceLink('Bot performance research','https://github.com/Jwarren-ENG/prediction-market-bot/blob/main/docs/BITCOIN_BOT_EVIDENCE.md')}</div>`;
  else if(view==='Portfolio') body=portfolio(state);
  else if(view==='Activity') body=activity(state,ui);
  else body=worker(state,ui);
  return `${headline('Bitcoin '+view.toLowerCase(),{Research:'Contract signals and source evidence',Portfolio:'Independent paper accounts and positions',Activity:'Paper fills, decisions and settlement receipts',Worker:'Bitcoin source permissions, automation and risk limits'}[view]||'Paper settings',view==='Worker'?'':tab('Markets'))}${body}`;
}
