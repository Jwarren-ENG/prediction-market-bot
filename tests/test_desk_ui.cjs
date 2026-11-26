const {test, before, afterEach} = require('node:test');
const assert = require('node:assert/strict');

test('daily activity collapses only matching decisions and retains contract receipts', async () => {
  const {groupReceipts,receiptRows,historyPanel} = await import('../ui/activity-history.js');
  const row = (id,kind,at,message) => ({id,kind,at,message,market_id:id});
  const rows = [row('alpha','decision',1,'Watch. Needs history.'), row('beta','decision',1,'Watch. Needs history.'),
    row('gamma','decision',2,'Watch. Needs history.'),row('fill','fill',1,'Bought 2')];
  assert.deepEqual(groupReceipts(rows).map(g=>g.length),[2,1,1]);
  const html = receiptRows(rows,new Map()).join('');
  for (const id of ['alpha','beta','gamma','fill']) assert.ok(html.includes(id));
  const result = historyPanel({hours:24,bin_seconds:3600,start_at:60000,end_at:86460000,
    decisions:{counts:Array(24).fill(2)},scans:{counts:Array(24).fill(3)}},24);
  assert.match(result,/48<\/strong>/);
  assert.match(result,/72<\/strong>/);
  assert.match(result,/View interval counts/);
  assert.match(historyPanel(null,168),/Loading recorded activity/);
});

test('event filter stays visible when a shorter activity window has no matching receipts', async () => {
  const {activityView} = await import('../ui/views.js');
  const state = {mode:'replay', activity:[], markets:[], activity_history:{
    hours:24, start_at:60000, end_at:86460000, bin_seconds:3600, decisions:{counts:Array(24).fill(0)}}};
  const html = activityView(state,{activityHours:24,activityFilter:'fill'});
  assert.match(html,/<option value="fill" selected>Fill<\/option>/);
  assert.match(html,/No matching events/);
});

let time, scanButton, latestPriceMoves;
before(async () => {
  time = await import('../ui/time.js');
  ({scanButton} = await import('../ui/common.js'));
  ({latestPriceMoves} = await import('../ui/activity.js'));
});
afterEach(() => {
  delete global.window;
  time.reloadDisplayTimeZone();
});

test('Eastern clock observes daylight saving time and contract dates stay on their event day', () => {
  assert.equal(time.displayTimeZone(), 'America/New_York');
  assert.equal(time.clock('2026-11-26T01:15:06Z'), '20:15:06');
  assert.match(time.stamp('2026-11-26T01:15:06Z'), /Nov 25, 2026.*20:15:06 EST/);
  assert.equal(time.zoneLabel('2026-07-01T17:00:00Z'), 'EDT');
  assert.equal(time.day('2026-11-26'), 'Nov 26, 2026');
  assert.equal(time.clock('bad timestamp'), '—');
});

test('time zone setting survives reload, an invalid preference restores Eastern', () => {
  const saved = new Map();
  global.window = {localStorage:{getItem:key=>saved.get(key), setItem:(key,value)=>saved.set(key,value)}};
  assert.equal(time.setDisplayTimeZone('UTC'), true);
  time.reloadDisplayTimeZone();
  assert.equal(time.displayTimeZone(), 'UTC');
  assert.match(time.wallClock(new Date('2026-11-26T01:15:06Z')), /1:15:06 AM UTC/);
  assert.equal(time.setDisplayTimeZone('missing/place'), false);
  assert.equal(time.displayTimeZone(), 'UTC');
  saved.set('pmb.displayTimeZone','missing/place');
  time.reloadDisplayTimeZone();
  assert.equal(time.displayTimeZone(), 'America/New_York');
});

test('header UTC and selected zone use a common instant through DST and midnight', () => {
  time.setDisplayTimeZone('America/New_York');
  assert.equal(time.isUtcDisplay(),false);
  const instant=new Date('2026-11-26T21:13:55Z');
  assert.match(time.wallClock(instant),/4:13:55 PM EST/);
  assert.equal(time.utcClock(instant),'21:13:55 UTC');
  const summer=new Date('2026-07-05T00:01:02Z');
  assert.match(time.wallClock(summer),/8:01:02 PM EDT/);
  assert.equal(time.utcClock(summer),'00:01:02 UTC');
  time.setDisplayTimeZone('UTC');
  assert.equal(time.isUtcDisplay(),true);
});

test('scans spin while running and report the actual durable result', () => {
  const render = (status, ui = {}) => scanButton({health:{scan:{id:'one',status,finished_at:status === 'running' ? null : '2025-01-01T00:00:00Z'}}}, ui);
  assert.match(render('running'), /scan-in-progress/);
  assert.match(render('running'), /disabled/);
  assert.match(render('complete'), /Run scan/);
  assert.match(render('complete',{scanReceipt:'one'}), /Scan complete</);
  assert.match(render('partial',{scanReceipt:'one'}), /Scan complete with gaps</);
  assert.match(render('failed',{scanReceipt:'one'}), /Scan failed</);
  const recovered = scanButton({health:{recent_scans:[{id:'one',status:'complete',finished_at:'2025-01-01T00:00:00Z'}]}},{scanReceipt:'one'});
  assert.match(recovered, /Scan complete</);
  assert.match(scanButton({health:{}},{scanSubmitting:true}), /Starting scan/);
  assert.match(scanButton({health:{online:false}},{}), /disabled/);
});

test('Activity lists observed moves with their source and skips unchanged, unknown and interrupted intervals', () => {
  const point = (seconds,bid,ask) => ({at:seconds*1000,bid,ask});
  const events = latestPriceMoves([{id:'one',label:'Cold day',history:[
    point(300,40,45),point(315,40,45),point(330,42,null),point(390,60,70),point(405,60,69),
  ],candles:[point(0,30,37),point(60,31,37),point(240,40,40),point(300,41,45)]}],15);
  assert.deepEqual(events.map(({at,detail,source})=>[at,detail,source]), [
    [405000,'Ask 70¢ → 69¢','Live poll'],
    [330000,'Bid 40¢ → 42¢','Live poll'],
    [60000,'Bid 30¢ → 31¢','One-minute close'],
  ]);
});

test('Portfolio explains the forward sample requirement without counting expired contracts or replay results', async () => {
  const {portfolioView} = await import('../ui/views.js');
  const base = {mode:'forward',portfolio:{mark_as_of:'2026-11-26T12:00:00Z'},markets:[]};
  const contract = (day,samples) => ({event_date:day,close_at:day+'T23:59:00Z',forecast:{samples,minimum_samples:30}});
  const state = {...base,markets:[contract('2026-11-25',89),contract('2026-11-26',7),contract('2026-11-27',3)]};
  assert.match(portfolioView(state), /7 \/ 30/);
  assert.match(portfolioView(state), /0 \/ 2/);
  assert.doesNotMatch(portfolioView({...state,mode:'replay'}), /Evidence for paper entries/);
  assert.doesNotMatch(portfolioView(base), /Evidence for paper entries/);
});

function bitcoinAccounts() {
  const now=Date.UTC(2026,10,26,21,42),at=offset=>new Date(now+offset*1000).toISOString();
  const state={health:{online:true,heartbeat:at(0)},bitcoin:{enabled:true,online:true,auto_enabled:false,
    max_entry_cents:50000,quote:{fresh:true,observed_at:at(0),source_at:at(-1),bid_usd:'60000',ask_usd:'60010'},
    portfolio:{cash_cents:1000000,open_cost_cents:0,btc:'0.0023'},activity:[]},bitcoin_brokers:{}};
  for(const [v,key,minutes] of [['kalshi','bitcoin_contracts',15],['polymarket_us','polymarket_contracts',5],['polymarket_us_hourly','polymarket_hourly_contracts',60]]){
    const id=v+'-test',current={id,open_at:at(-60),close_at:at((minutes-1)*60),observed_at:at(-1),source_at:at(-1),yes_bid_cents:48,yes_ask_cents:50,no_bid_cents:50,no_ask_cents:52,history:[]};
    state[key]={enabled:true,status:'live',interval_seconds:10,stale_after_seconds:20,current};
    state.bitcoin_brokers[v]={auto_enabled:v==='polymarket_us_hourly',limits:{max_order_qty:20,max_order_cents:2500,max_window_cents:5000,max_open_cents:10000,daily_loss_cents:5000},
      portfolio:{cash_cents:1000000,equity_cents:1000000,filled_orders:0},positions:[{market_id:id,side:'no',qty:4,cost_cents:200}],
      markets:[{market_id:id,tradable:true,signal:'Observed signal'}],daily:{date:'2026-11-26',net_gain_cents:20,fees_cents:2,filled_orders:1},
      daily_history:[{date:'2026-11-25',net_gain_cents:-25,filled_orders:2}],performance:{closed_windows:4,net_cents:220,max_drawdown_cents:12,
      auto_only:{closed_windows:3,profitable_windows:1,loss_windows:1,flat_windows:1,net_cents:80,win_rate_pct:100/3}}};
  }
  return {state,ui:{connected:true,locked:false,now,btcDrafts:new Map(),btcMessages:new Map()}};
}
test('Bitcoin venue tickets require their exact open contract, independent paper account, fresh book, valid outcome and limit', async()=>{
  const {state,ui}=bitcoinAccounts(),{makeOrder,defaultTicketLimit,orderTicket}=await import('../ui/bitcoin-trading.js');
  const v='polymarket_us_hourly',id=v+'-test',draft={action:'sell',side:'no',qty:'2',limit:'30.25'};
  assert.equal(defaultTicketLimit(state,v,draft),'50');
  assert.deepEqual(makeOrder(state,ui,v,id,draft,'manual-a').value,{client_id:'manual-a',venue:v,market_id:id,action:'sell',side:'no',qty:2,limit_cents:30.25});
  assert.throws(()=>makeOrder(state,ui,v,'polymarket_us-test',draft,'manual-b'),/current contract/);
  assert.throws(()=>makeOrder(state,ui,v,id,{...draft,qty:'2.5'},'manual-b'),/whole number/);
  assert.throws(()=>makeOrder(state,ui,v,id,{...draft,limit:'100'},'manual-b'),/99.99/);
  assert.throws(()=>makeOrder(state,ui,v,id,{...draft,side:'yes'},'manual-b'),/holds no YES/);
  assert.throws(()=>makeOrder(state,{...ui,now:ui.now+25000},v,id,draft,'manual-b'),/fresh/);
  state.polymarket_hourly_contracts.status='restricted';assert.match(orderTicket(state,ui,v),/type="submit" class="btn primary" disabled/);
  state.polymarket_hourly_contracts.enabled=false;assert.doesNotMatch(orderTicket(state,ui,v),/polymarket_us_hourly-test/);
});
test('spot budget and BTC quantity preserve decimal units and enforce the remaining independent entry allowance',async()=>{
  const {state,ui}=bitcoinAccounts(),{makeOrder,defaultTicketLimit}=await import('../ui/bitcoin-trading.js');
  const d={action:'buy',budget:'8.25',limit:'61000'};
  assert.equal(defaultTicketLimit(state,'spot',d),'60040.01');
  assert.deepEqual(makeOrder(state,ui,'spot','',d,'uuid').value,{client_id:'uuid',side:'buy',budget_cents:825,limit_usd:'61000'});
  state.bitcoin.portfolio.open_cost_cents=49900;
  assert.throws(()=>makeOrder(state,ui,'spot','',d,'uuid'),/\$1.00/);
  assert.equal(makeOrder(state,ui,'spot','',{action:'sell',full:true,limit:'59900'},'uuid').value.quantity_btc,'all');
  assert.equal(makeOrder(state,ui,'spot','',{action:'sell',amount:'0.00000001',limit:'59900'},'uuid').value.quantity_btc,'0.00000001');
  assert.throws(()=>makeOrder(state,ui,'spot','',{action:'sell',amount:'0.000000001',limit:'59900'},'uuid'),/eight decimal/);
});
test('independent ticket drafts preserve typed prices across a refresh and keep accepted orders pending by the same ID',async()=>{
  const {state,ui}=bitcoinAccounts(),{orderTicket,updateTicket,ticketDraft,savePendingOrders,loadPendingOrders}=await import('../ui/bitcoin-trading.js');
  const key='kalshi/kalshi-test';orderTicket(state,ui,'kalshi');updateTicket(ui,key,'limit','41.25');updateTicket(ui,key,'qty','3');
  state.bitcoin_contracts.current.yes_ask_cents=60;
  assert.match(orderTicket(state,ui,'kalshi'),/name="limit"[^>]*value="41.25"/);assert.match(orderTicket(state,ui,'kalshi'),/name="qty"[^>]*value="3"/);
  assert.equal(ticketDraft(ui,'polymarket_us/polymarket_us-test').qty,'1');
  const rec={key,action:'btc-contract-order',value:{client_id:'order-01',venue:'kalshi',limit_cents:41.25}};
  const cache=new Map();global.sessionStorage={getItem:k=>cache.get(k),setItem:(k,v)=>cache.set(k,v),removeItem:k=>cache.delete(k)};
  try{savePendingOrders(new Map([['kalshi',rec]]));assert.deepEqual(loadPendingOrders().get('kalshi'),rec);assert.match(orderTicket(state,ui,'kalshi'),/Retry same paper order/);
    assert.doesNotMatch(orderTicket(state,ui,'polymarket_us'),/Retry same paper order|type="submit" class="btn primary" disabled/);
    savePendingOrders(new Map());assert.equal(loadPendingOrders().size,0);}finally{delete global.sessionStorage;}
});
test('Bitcoin portfolio, research, activity and independent comparison tab distinguish all three trial ledgers',async()=>{
  const {state,ui}=bitcoinAccounts(),{bitcoinAccountView}=await import('../ui/bitcoin-account.js'),{comparisonView}=await import('../ui/bitcoin.js');
  let html=bitcoinAccountView(state,{...ui,btcView:'Portfolio'});for(const label of ['Kalshi 15-minute','Polymarket US 5-minute','Polymarket US hourly','Open contract positions','Last completed Eastern day'])assert.ok(html.includes(label));
  html=bitcoinAccountView(state,{...ui,btcView:'Research'});assert.match(html,/1 profitable of 3 completed automatic-only windows/);assert.match(html,/Worst recorded drawdown/);
  const r={client_id:'same-fill',source:'manual',status:'filled',price_cents:49,fee_cents:2,at:state.health.heartbeat,side:'yes',qty:1,action:'buy'};
  state.bitcoin_brokers.kalshi.fills=[r];state.bitcoin_brokers.kalshi.activity=[{id:8,at:r.at,kind:'fill',message:'Known fill',data:r}];
  html=bitcoinAccountView(state,{...ui,btcView:'Activity',btcActivityFilter:'kalshi'});assert.equal((html.match(/same-fill/g)||[]).length,1);assert.match(html,/Manual/);
  html=bitcoinAccountView(state,{...ui,btcView:'Worker'});for(const v of Object.keys(state.bitcoin_brokers))assert.match(html,new RegExp('data-btc-risk="'+v+'"'));
  const compare=comparisonView({...state,comparison:{accounts:{},bitcoin_managed:true}},ui);assert.match(compare,/Bitcoin contract trials/);assert.match(compare,/managed spot account includes manual/);assert.doesNotMatch(compare,/data-btc-risk|data-btc-ticket/);
});

test('portfolio line uses dollar equity, signed period return and separate segments', async () => {
  const {equityPlot,portfolioChart} = await import('../ui/portfolio-chart.js');
  const end=Date.UTC(2026,10,26,21), beginning=end-120000;
  const series={account:'kalshi',minutes:240,start_ms:end-14400000,end_ms:end,first_at:new Date(beginning).toISOString(),
    current_cents:999999,has_gaps:true,points:[[beginning,1000000,0],[beginning+30000,1000002,0],[end,999999,1]]};
  const model=equityPlot(series);
  assert.equal(model.start,beginning);
  assert.equal(model.x(beginning),0);
  assert.equal(model.x(end),100);
  assert.ok(model.y(999999)>model.y(1000002));
  assert.ok(model.ticks.every(Number.isInteger));
  const html=portfolioChart({portfolio_history:series},{equityAccount:'kalshi'},'bitcoin');
  assert.match(html,/−\$0\.01 \(−0\.0001%\)/);
  assert.match(html,/equity-single" d="M1000/);
  assert.match(html,/aria-label="1 week"/);
  assert.match(html,/Breaks in the line/);
  assert.match(html,/\$9,999\.99/);
  const pending=portfolioChart({portfolio_history:series},{equityAccount:'polymarket_us'},'bitcoin');
  assert.match(pending,/Loading the recorded line/);
  assert.doesNotMatch(pending,/9,999/);
  const missing=portfolioChart({portfolio_history:{...series,current_cents:null}},{},'bitcoin');
  assert.match(missing,/Last known value/);
  assert.doesNotMatch(missing,/−0\.0001%/);
});

test('unchanged and single portfolio valuations have a legible scale and truthful change', async () => {
  const {equityPlot,portfolioChart}=await import('../ui/portfolio-chart.js');
  const end=Date.UTC(2026,10,26,22);
  const series={account:'replay',minutes:15,start_ms:end-900000,end_ms:end,first_at:new Date(end).toISOString(),current_cents:1000000,points:[[end,1000000,0]]};
  const model=equityPlot(series);
  assert.ok(model.y(1000000)>0 && model.y(1000000)<100);
  let html=portfolioChart({mode:'replay',portfolio_history:series},{equityMinutes:15},'weather');
  assert.match(html,/First valuation recorded/);
  assert.match(html,/Replay valuation/);
  assert.doesNotMatch(html,/\(0%\)/);
  series.points.unshift([end-30000,1000000,0]);
  html=portfolioChart({mode:'replay',portfolio_history:series},{equityMinutes:15},'weather');
  assert.match(html,/\$0\.00 \(0%\)/);
});
