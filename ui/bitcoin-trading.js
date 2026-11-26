import {arr, button, count, dash, esc, instant, money, num, price, stamp} from './common.js';

export const venues = {
  kalshi:{name:'Kalshi',label:'Kalshi 15-minute',feed:'bitcoin_contracts',minutes:15},
  polymarket_us:{name:'Polymarket US',label:'Polymarket US 5-minute',feed:'polymarket_contracts',minutes:5},
  polymarket_us_hourly:{name:'Polymarket US',label:'Polymarket US hourly',feed:'polymarket_hourly_contracts',minutes:60},
};
const contractLimits = [
  ['max_order_qty','Contracts per order',100,false],['max_order_cents','Cost per order ($)',10000,true],
  ['max_window_cents','Cost per window ($)',25000,true],['max_open_cents','Total open cost ($)',50000,true],
  ['daily_loss_cents','Daily loss stop ($)',25000,true],
];
const pendingKey = 'pmb.bitcoin.pendingPaperOrder.v1';
const positive = value => num(value) !== null && Number(value)>0;
const dollars = value => positive(value) ? money(Number(value)*100) : dash;
const unit = value => num(value) === null ? dash : `${Number(value).toLocaleString('en-US',{maximumFractionDigits:8})} BTC`;
const nowMs = ui => Number.isFinite(ui.now) ? ui.now : Date.now();
const quotedAt = value => instant(value)?.getTime();
const recent = (at, now, seconds) => Number.isFinite(at) && now-at >= -1000 && now-at <= seconds*1000;
const fieldNumber = (n,maxDigits) => new RegExp('^\\d+(?:\\.\\d{1,'+maxDigits+'})?$').test(String(n)) && positive(n);
const toCents = value => fieldNumber(value,2) ? Math.round(Number(value)*100) : null;
const safeQuantity = value => /^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value)) && Number(value)>0;
export const brokerFor = (state,venue) => state.bitcoin_brokers?.[venue];
export const feedFor = (state,venue) => state[venues[venue]?.feed] || {};
export const ticketKey = (venue,market) => venue === 'spot' ? 'spot' : `${venue}/${market || ''}`;
export function createOrderId() {
  return crypto.randomUUID();
}
export function loadPendingOrders() {
  try {
    const parsed=JSON.parse(sessionStorage.getItem(pendingKey)),orders=new Map();
    for (const o of Array.isArray(parsed)?parsed:[parsed]) {
      const venue=o?.action==='btc-spot-order'?'spot':o?.value?.venue;
      if ((venue==='spot'||Object.hasOwn(venues,venue)) && typeof o.key==='string' && typeof o.value?.client_id==='string' &&
          ['btc-spot-order','btc-contract-order'].includes(o.action)) orders.set(venue,o);
    }
    return orders;
  } catch { return new Map(); }
}
export function savePendingOrders(orders) {
  try { if (orders.size) sessionStorage.setItem(pendingKey,JSON.stringify([...orders.values()])); else sessionStorage.removeItem(pendingKey); } catch {}
}
export function ticketDraft(ui, key) {
  ui.btcDrafts ??= new Map();
  if (!ui.btcDrafts.has(key)) {
    if (ui.btcDrafts.size >= 24) ui.btcDrafts.delete(ui.btcDrafts.keys().next().value);
    ui.btcDrafts.set(key,{action:'buy',side:'yes',qty:'1',budget:'',amount:'',limit:null,full:false,done:false});
  }
  return ui.btcDrafts.get(key);
}
export function updateTicket(ui,key,name,value) {
  if (!['action','side','qty','budget','amount','limit','full'].includes(name)) return;
  const draft=ticketDraft(ui,key);
  if (name==='action' && !['buy','sell'].includes(value) || name==='side' && !['yes','no'].includes(value)) return;
  if (name==='action' || name==='side') draft.limit=null;
  draft[name]=value;draft.done=false;
  ui.btcMessages?.delete(key);
}
function bestContract(market,side,action) {
  const n=num(market?.[`${side}_${action==='buy'?'ask':'bid'}_cents`]);
  return n!==null && n>0 && n<100 ? n : null;
}
export function defaultTicketLimit(state,venue,draft) {
  if (venue==='spot') {
    const m=state.bitcoin || {}, q=m.quote, buying=draft.action==='buy';
    if (!q?.fresh || !positive(q[buying?'ask_usd':'bid_usd'])) return null;
    const value=Number(q[buying?'ask_usd':'bid_usd'])*(1+(buying?1:-1)*(num(m.adverse_execution_bps)??5)/10000);
    return ((buying ? Math.ceil : Math.floor)(value*100)/100).toFixed(2);
  }
  const n=bestContract(feedFor(state,venue).current,draft.side,draft.action);
  return n===null ? null : String(n);
}
function orderGate(state,ui,venue,marketId,draft) {
  if (!ui.connected) return 'Reconnect to the worker to submit a paper order.';
  const now=nowMs(ui);
  if (venue==='spot') {
    const m=state.bitcoin || {},q=m.quote;
    if (!m.online || !m.enabled) return 'Enable Bitstamp collection on an online worker to trade spot BTC.';
    if (!q?.fresh || !recent(quotedAt(q.observed_at),now,30) || !recent(quotedAt(q.source_at),now,30)) return 'Waiting for a fresh Bitstamp bid and ask.';
    if (draft.action==='sell' && !positive(m.portfolio?.btc)) return 'This spot account holds no BTC to sell.';
    return '';
  }
  const feed=feedFor(state,venue), market=feed.current, broker=brokerFor(state,venue);
  if (!feed.enabled) return `Enable ${venues[venue].name} public data to submit a paper order.`;
  if (!broker?.portfolio) return 'Waiting for the contract paper account.';
  if (!market || market.id!==marketId) return 'Waiting for this venue’s current contract.';
  if (!(quotedAt(market.open_at)<=now && now<quotedAt(market.close_at))) return 'The contract is outside its trading window.';
  if (!['live','partial'].includes(feed.status) || !recent(quotedAt(market.observed_at),now,num(feed.stale_after_seconds)||20)) return 'Waiting for a fresh, executable order book.';
  const record=arr(broker.markets).find(r=>r.market_id===marketId);
  if (!record?.tradable) return record?.reason || 'Waiting for the broker to verify this contract and its fees.';
  if (bestContract(market,draft.side,draft.action)===null) return `No posted ${draft.side.toUpperCase()} ${draft.action==='buy'?'ask':'bid'} for this order.`;
  if (draft.action==='sell' && !arr(broker.positions).some(p=>p.market_id===marketId && p.side===draft.side && positive(p.qty))) return `This account holds no ${draft.side.toUpperCase()} contracts in this window.`;
  return '';
}
export function makeOrder(state,ui,venue,marketId,draft,id) {
  if (venue!=='spot' && !Object.hasOwn(venues,venue)) throw new Error('Choose a listed Bitcoin venue.');
  const reason=orderGate(state,ui,venue,marketId,draft);
  if (reason) throw new Error(reason);
  if (!['buy','sell'].includes(draft.action)) throw new Error('Choose buy or sell.');
  const buying=draft.action==='buy';
  if (venue==='spot') {
    if (!fieldNumber(draft.limit,8)) throw new Error('Enter a positive BTC price with up to eight decimal places.');
    const value={client_id:id,side:draft.action,limit_usd:String(draft.limit)};
    if (buying) {
      const m=state.bitcoin, budget=toCents(draft.budget);
      if (!Number.isSafeInteger(budget) || budget<=0) throw new Error('Enter a dollar budget with up to two decimal places.');
      const ceiling=Math.min(m.portfolio.cash_cents,Math.max(0,(m.max_entry_cents??50000)-(m.portfolio.open_cost_cents??0)));
      if (budget>ceiling) throw new Error(`The available spot entry budget is ${money(ceiling)}.`);
      value.budget_cents=budget;
    } else {
      if (!draft.full && !fieldNumber(draft.amount,8)) throw new Error('Enter a positive BTC quantity with up to eight decimal places, or select the entire position.');
      value.quantity_btc=draft.full?'all':String(draft.amount);
    }
    return {key:'spot',action:'btc-spot-order',value};
  }
  if (!Object.hasOwn(venues,venue) || !['yes','no'].includes(draft.side)) throw new Error('Choose a listed venue and outcome.');
  if (!safeQuantity(draft.qty)) throw new Error('Enter a positive whole number of contracts.');
  if (!fieldNumber(draft.limit,2) || Number(draft.limit)>=100) throw new Error('Enter a price between 0.01¢ and 99.99¢, with up to two decimal places.');
  return {key:ticketKey(venue,marketId),action:'btc-contract-order',value:{client_id:id,venue,market_id:marketId,action:draft.action,side:draft.side,qty:Number(draft.qty),limit_cents:Number(draft.limit)}};
}
export function ticketFeedback(receipt,spot) {
  if (!receipt) return 'The worker returned no readable order receipt.';
  if (receipt.status==='rejected') return `Paper order rejected. ${receipt.reason || 'The order did not fill.'}`;
  const buy=spot ? receipt.side==='buy' : receipt.action==='buy';
  const fill=spot ? `${unit(receipt.btc)} at ${dollars(receipt.price_usd)}` : `${count(receipt.qty)} ${String(receipt.side).toUpperCase()} at ${price(receipt.price_cents)}`;
  return `${buy?'Bought':'Sold'} ${fill}. ${buy?'Cash paid':'Cash received'} ${money(receipt.cash_amount_cents)}, including ${money(receipt.fee_cents)} in fees. ${stamp(receipt.at)}.`;
}
const option = (key,title,current) => `<option value="${esc(key)}"${current===key?' selected':''}>${esc(title)}</option>`;
function ticketInput(name,label,value,attrs='') {
  return `<label>${esc(label)}<input name="${name}" data-ticket-field="${name}" inputmode="${name==='qty'?'numeric':'decimal'}" autocomplete="off" maxlength="24" value="${esc(value??'')}" ${attrs}></label>`;
}
export function autoSwitch(label,action,enabled,disabled,venue='') {
  const known=typeof enabled==='boolean';
  const name=venue ? `${label} for ${venues[venue].label}` : label;
  return `<button type="button" class="paper-switch ${known ? enabled ? 'is-on':'is-off' : 'unknown'}" data-action="${action}" data-value="${!enabled}"${venue ? ` data-venue="${venue}"`:''} ${known?`role="switch" aria-checked="${enabled}" aria-label="${esc(name)}"`:''}${disabled||!known?' disabled':''}><i class="paper-lamp" aria-hidden="true"></i><span>${esc(label)}</span><b>${known ? enabled ? 'Yes':'No':'Unknown'}</b></button>`;
}
export function contractAuto(state,ui,venue) {
  const broker=brokerFor(state,venue),feed=feedFor(state,venue);
  return autoSwitch('Paper auto trading','btc-contract-auto',ui.connected ? broker?.auto_enabled:null,ui.locked || !ui.connected || (!broker?.auto_enabled && (!feed.enabled || state.health?.online===false)),venue);
}
export function orderTicket(state,ui,venue) {
  const spot=venue==='spot',broker=spot ? state.bitcoin : brokerFor(state,venue),feed=spot?null:feedFor(state,venue);
  const m=feed?.enabled===true && feed.status!=='disabled'?feed.current:null;
  if (!spot && !broker?.portfolio && !m) return '';
  const key=ticketKey(venue,m?.id), d=ticketDraft(ui,key), gate=orderGate(state,ui,venue,m?.id,d), buying=d.action==='buy';
  const current=defaultTicketLimit(state,venue,d);
  if (d.limit===null && current!==null && !gate) d.limit=current;
  const p=broker?.portfolio || {},holding=arr(broker?.positions).find(pos=>pos.market_id===m?.id && pos.side===d.side);
  const purpose=spot ? 'Spot paper order' : 'Paper order';
  const account=spot ? 'Bitstamp spot' : venues[venue].label;
  const pending=(ui.btcPendingOrders??loadPendingOrders()).get(venue), msg=ui.btcMessages?.get(key);
  const retry=pending?.key===key;
  const qtyLabel=spot ? buying?'Buy budget ($)':'Amount to sell (BTC)' : 'Contracts';
  const qty=spot ? buying ? ticketInput('budget',qtyLabel,d.budget,'required placeholder="25.00"') :
    `${ticketInput('amount',qtyLabel,d.amount,d.full?'disabled':'required')}<label class="btc-full-order"><input type="checkbox" data-ticket-field="full" name="full"${d.full?' checked':''}>Sell entire position</label>` :
    ticketInput('qty',qtyLabel,d.qty,'required');
  const selectedQuote=spot ? state.bitcoin?.quote?.[buying?'ask_usd':'bid_usd'] : bestContract(m,d.side,d.action);
  const quotePrice=spot ? dollars(selectedQuote) : price(selectedQuote);
  const quoteTime=spot ? state.bitcoin?.quote?.observed_at : m?.observed_at;
  const submitted=(!spot && m?.id) || spot;
  const limitLabel=(buying?'Maximum buy price':'Minimum sell price')+(spot?' ($/BTC)':' (¢)');
  const behind=gate && !retry ? `<p class="btc-order-reason">${esc(gate)}</p>` : '';
  return `<section class="panel btc-order" aria-labelledby="paper-order-${venue}"><div class="panel-head"><h2 id="paper-order-${venue}" tabindex="-1">${purpose}</h2><span class="small-note">${esc(account)}</span></div><div class="panel-body">
    <dl class="btc-order-account"><div><dt>Available cash</dt><dd>${money(p.cash_cents)}</dd></div><div><dt>${spot?'BTC held':d.side.toUpperCase()+' held'}</dt><dd>${spot?unit(p.btc):broker?.portfolio ? count(holding?.qty??0) : dash}</dd></div></dl>
    <form data-btc-ticket="${esc(key)}" data-venue="${venue}" data-market="${esc(m?.id || '')}" data-key="form-${esc(key)}">
    <fieldset${retry || ui.busy ? ' disabled':''}><legend class="sr-only">${purpose} parameters</legend><div class="btc-ticket-fields"><label>Direction<select name="action" data-ticket-field="action">${option('buy','Buy',d.action)}${option('sell','Sell',d.action)}</select></label>
    ${spot?'':`<label>Outcome<select name="side" data-ticket-field="side">${option('yes','YES',d.side)}${option('no','NO',d.side)}</select></label>`}
    ${qty}${ticketInput('limit',limitLabel,d.limit,'required')}</div>
    ${current!==null && !gate ? `<p class="btc-ticket-quote">Posted ${buying?'ask':'bid'} <strong>${quotePrice}</strong> · ${esc(stamp(quoteTime))} ${button(spot?'Use modeled price':'Use posted price','btc-use-price',{kind:'inline'})}</p>`:''}</fieldset>
    ${spot?`<p class="btc-note">Available entry allowance ${money(Math.min(p.cash_cents??0,Math.max(0,(broker?.max_entry_cents??50000)-(p.open_cost_cents??0))))} of ${money(broker?.max_entry_cents??50000)}. A manual fill pauses spot automation. Prices include a 0.05% allowance and the worker charges a 0.60% fee.</p>`:
      `<p class="btc-note">${m?.id ? esc(m.id)+' · ' : ''}The worker checks available book depth, your price limit and account limits before filling.</p>`}
    ${behind}${msg?`<p class="btc-ticket-result${msg.error?' error':''}" role="${msg.error?'alert':'status'}">${esc(msg.text)}</p>`:''}
    ${retry?`${button('Retry same paper order','btc-retry-order',{value:venue,kind:'primary',disabled:ui.locked})}<p class="btc-note">The prior request is awaiting a confirmed receipt.</p>`:
      d.done?button('New paper order','btc-new-order',{kind:'secondary',disabled:ui.locked}):
      `<button type="submit" class="btn primary"${ui.locked||gate||!submitted||pending?' disabled':''}>Paper ${buying?'buy':'sell'} ${spot?'BTC':d.side.toUpperCase()}</button>`}
    </form></div></section>`;
}
export function riskForm(state,ui,venue) {
  const limits=brokerFor(state,venue)?.limits;
  if (!limits) return '<p class="btc-empty">Waiting for paper risk limits.</p>';
  const draft=ui.btcRiskDraft?.[venue] || {};
  return `<form data-btc-risk="${venue}" data-key="risk-${venue}"><div class="btc-ticket-fields">${contractLimits.map(([key,label,upper,cash])=>{
    const v=draft[key] ?? (cash ? (limits[key]/100).toFixed(2) : limits[key]);
    return `<label>${esc(label)}<input name="${key}" type="number" min="${cash?'.01':'1'}" max="${cash?upper/100:upper}" step="${cash?'.01':'1'}" value="${esc(v)}" required></label>`;
  }).join('')}</div><button type="submit" class="btn secondary"${ui.locked?' disabled':''}>Save ${esc(venues[venue].label)} limits</button></form>`;
}
export function riskPayload(venue,form) {
  if (!Object.hasOwn(venues,venue)) throw new Error('Unknown paper account.');
  const limits={};
  for (const [key,label,upper,cash] of contractLimits) {
    const raw=form.get(key),value=cash ? toCents(raw) : safeQuantity(raw)? Number(raw):null;
    if (!Number.isSafeInteger(value) || value<1 || value>upper) throw new Error(`Check ${label}.`);
    limits[key]=value;
  }
  return {venue,limits};
}
