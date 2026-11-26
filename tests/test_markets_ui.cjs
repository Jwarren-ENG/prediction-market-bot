const {before, test} = require('node:test');
const assert = require('node:assert/strict');

let quoteRows, quoteWindow, marketsView, paperSwitch, setDisplayTimeZone;
before(async () => {
  ({quoteRows, quoteWindow, marketsView, paperSwitch} = await import('../ui/markets.js'));
  ({setDisplayTimeZone} = await import('../ui/common.js'));
  setDisplayTimeZone('UTC');
});

const base = Date.UTC(2026,10,26,15,0);
const at = seconds => new Date(base + seconds * 1000).toISOString();
const point = (seconds, bid, ask) => ({at:at(seconds), bid_cents:bid, ask_cents:ask});

function snapshot(history, overrides = {}) {
  const latest = history.at(-1);
  return {
    mode:'forward', health:{online:true, heartbeat:latest.at}, settings:{allow_market:true},
    markets:[{id:'fixture-A'}],
    quotes:{status:'live', last_checked_at:latest.at, last_success_at:latest.at, interval_seconds:15,
      markets:[{id:'fixture-A',label:'40 to 41°F',event_date:'2026-11-26',last_trading_at:at(-30),close_at:at(3600),
        observed_at:latest.at, bid_cents:latest.bid_cents, ask_cents:latest.ask_cents, history, ...overrides}]},
  };
}
function screen(state, options = {}) {
  const ui = {quoteSelected:'fixture-A', quoteRange:15, connected:true, ...options};
  return marketsView(state,ui,quoteRows(state,ui.connected));
}

test('side changes use consecutive comparable quotes, including an unchanged or fractional price', () => {
  const state = snapshot([point(0,32.5,36), point(15,32.5,35.25)]);
  const [row] = quoteRows(state,true);
  assert.equal(row.bidChange,0);
  assert.equal(row.askChange,-.75);
  const [oneSided] = quoteRows(snapshot([point(0,null,36),point(15,32,34)]),true);
  assert.equal(oneSided.bidChange,null);
  assert.equal(oneSided.askChange,-2);
  const [delayed] = quoteRows(snapshot([point(0,30,33),point(31,32,34)]),true);
  assert.equal(delayed.bidChange,null);
  assert.equal(delayed.askChange,null);
});

test('missing sides, sentinel endpoints and crossed books stay missing', () => {
  const state = snapshot([point(0,0,100),point(15,52,50),point(30,null,55)]);
  const [row] = quoteRows(state,true);
  assert.deepEqual(row.history.map(p=>[p.bid,p.ask]),[[null,null],[null,null],[null,55]]);
  assert.equal(row.bid,null);
  assert.equal(row.ask,55);
  assert.equal(row.askChange,null);
  const html = screen(state);
  assert.match(html,/One-sided book/);
  assert.doesNotMatch(html,/class="quote-(?:point|line) bid"/);
  assert.match(html,/class="quote-point ask"/);
});

test('the chart splits each side at explicit gaps and gaps longer than two polls', () => {
  const state = snapshot([point(0,10,15),point(15,12,17),point(30,null,18),point(45,14,19),point(90,15,null),point(105,16,21)]);
  const html = screen(state);
  assert.equal((html.match(/class="quote-line bid"/g) || []).length,2);
  assert.equal((html.match(/class="quote-point bid"/g) || []).length,1);
  assert.equal((html.match(/class="quote-line ask"/g) || []).length,1);
  assert.equal((html.match(/class="quote-point ask"/g) || []).length,1);
});

test('the chart plots a lone valid sample as a point and filters the selected time window', () => {
  const state = snapshot([point(0,20,null),point(1800,null,null)]);
  assert.doesNotMatch(screen(state),/class="quote-point bid"/);
  const hour = screen(state,{quoteRange:60});
  assert.match(hour,/class="quote-point bid"/);
  assert.doesNotMatch(hour,/class="quote-line bid"/);
  assert.doesNotMatch(hour,/class="quote-point ask"/);
});

test('interrupted feeds preserve history while hiding obsolete current prices', () => {
  const state = snapshot([point(0,20,26), point(15,22,25)]);
  for (const status of ['stale','error','rate_limited','offline','waiting','empty']) {
    state.quotes.status = status;
    const [m] = quoteRows(state,true);
    assert.equal(m.bid,null);
    assert.equal(m.ask,null);
    assert.equal(m.history.length,2);
    assert.match(screen(state),/Current prices unavailable/);
  }
  state.quotes.status = 'live';
  assert.equal(quoteRows(state,false)[0].bid,null);
  state.quotes.last_success_at = at(100);
  assert.equal(quoteRows(state,true)[0].ask,null);
  state.settings.allow_market = false;
  assert.deepEqual(quoteRows(state,true),[]);
  assert.doesNotMatch(screen(state),/quote-line bid/);
});

test('research drilldown, trading cutoff and later listed close identify the correct account and timestamp', () => {
  const state = snapshot([point(0,30,32)]);
  const html = screen(state);
  assert.match(html,/Trading window closed/);
  assert.match(html,/Last trading time/);
  assert.match(html,/Venue listing close/);
  assert.match(html,/data-action="quote-research"/);
  state.mode = 'replay';
  assert.doesNotMatch(screen(state),/data-action="quote-research"/);
  state.mode = 'forward';
  state.markets = [];
  assert.doesNotMatch(screen(state),/data-action="quote-research"/);
});

test('all three windows end at the same real receipt and use only observations inside the selected span', () => {
  const state = snapshot([point(-15,20,22),point(0,20,22)],{candles:[
    point(-14460,1,2),point(-12000,4,8),point(-3600,9,12),point(-900,18,21),point(60,97,98),
  ]});
  const [row] = quoteRows(state,true);
  for (const [minutes,count,low] of [[15,3,18],[60,4,9],[240,5,4]]) {
    const w = quoteWindow(row,state.quotes,minutes);
    assert.equal(w.end,base);
    assert.equal(w.start,base-minutes*60000);
    assert.equal(w.observed.length,count);
    assert.equal(w.bid.low,low);
    assert.equal(w.bid.high,20);
    assert.equal(w.ask.high,22);
    assert.match(screen(state,{quoteRange:minutes}),new RegExp(`data-value="${minutes}" aria-pressed="true"`));
  }
  assert.match(screen(state,{quoteRange:undefined}),/4 hours<\/strong> <span>·<\/span> 11:00:00–15:00:00 UTC/);
  assert.equal(row.bidChange,0);
});

test('local coverage takes precedence and a missing minute or source change breaks the drawn line', () => {
  const state = snapshot([point(-75,30,34),point(-60,31,35),point(-45,32,36),point(-30,33,null),point(0,34,38)],{candles:[
    point(-300,10,14),point(-240,11,15),point(-120,15,18),point(-60,90,95),point(-50,90,94),point(-15,34,37),
  ]});
  const [row] = quoteRows(state,true);
  const w = quoteWindow(row,state.quotes);
  assert.deepEqual(w.points.filter(p=>p.source==='minute').map(p=>p.at),[-300,-240,-120,-15].map(s=>base+s*1000));
  assert.equal(w.ask.high,38);
  const html = screen(state);
  assert.equal((html.match(/class="quote-line bid" data-source="minute"/g)||[]).length,1);
  assert.equal((html.match(/class="quote-point bid" data-source="minute"/g)||[]).length,2);
  assert.match(html,/Kalshi minute close/);
  assert.match(html,/Worker poll/);
  assert.doesNotMatch(html,/95¢/);
});

test('late or one-sided observations keep the full clock scale and never become a live quote', () => {
  const state = snapshot([point(0,null,null)],{candles:[point(-120,null,48)]});
  const [row] = quoteRows(state,true);
  const w = quoteWindow(row,state.quotes);
  assert.equal(w.start,base-240*60000);
  assert.equal(w.bid,null);
  assert.deepEqual(w.ask,{low:48,high:48,count:1});
  assert.equal(row.ask,null);
  assert.equal(row.askChange,null);
  const html = screen(state,{quoteRange:240});
  assert.match(html,/No current ask/);
  assert.match(html,/One observation at 14:58:00 UTC/);
  assert.match(html,/quote-point ask" data-source="minute" d="M98/);
  assert.doesNotMatch(html,/quote-line ask/);
});

test('history failures leave current polling independent and report missing history', () => {
  const state = snapshot([point(0,11,15)]);
  for (const [status,label] of [['loading','Loading Kalshi'],['partial','Some Kalshi'],['error','could not be refreshed'],['rate_limited','has delayed']]) {
    state.quotes.history_status=status;
    const html=screen(state);
    assert.match(html,new RegExp(label));
    assert.match(html,/Two-sided book/);
    assert.match(html,/1 worker poll/);
  }
});

test('the switch uses authoritative forward state, repeats no optimistic color, and routes risk to Worker', () => {
  const state = {mode:'replay',paused:true,paper_automation:{available:true,enabled:true,manual_pause:false,risk_pause_reason:null}};
  const ui = {connected:true,locked:false};
  assert.match(paperSwitch(state,ui),/is-on[\s\S]+aria-checked="true"[\s\S]+>Yes</);
  assert.match(paperSwitch(state,{...ui,locked:true,paperSwitchPending:true}),/aria-checked="true"[^>]*disabled/);
  assert.match(paperSwitch(state,{...ui,connected:false}),/unknown[\s\S]+disabled[\s\S]+>Unknown</);
  assert.doesNotMatch(paperSwitch({mode:'forward'},ui),/role="switch"/);
  state.paper_automation={available:true,enabled:false,manual_pause:true,risk_pause_reason:null};
  assert.match(paperSwitch(state,ui),/is-off[\s\S]+aria-checked="false"[\s\S]+>No</);
  assert.doesNotMatch(paperSwitch(state,ui),/disabled/);
  state.paper_automation.risk_pause_reason='Daily loss stop';
  assert.match(paperSwitch(state,ui),/aria-checked="false"[^>]*disabled/);
  assert.match(paperSwitch(state,ui),/Daily loss stop[\s\S]+Review risk in Worker/);
  state.paper_automation={available:false,enabled:false,reason:'Market capture is off'};
  assert.match(paperSwitch(state,ui),/disabled[\s\S]+Market capture is off/);
});

test('changing display zone shifts market chart labels without moving captured instants', () => {
  const state=snapshot([point(0,15,22)]);
  try {
    setDisplayTimeZone('America/New_York');
    const eastern=screen(state,{quoteRange:240});
    assert.match(eastern,/06:00:00–10:00:00 EST/);
    assert.match(eastern,/Cents per contract · EST/);
    assert.match(eastern,/datetime="2026-11-26T11:00:00.000Z"/);
    assert.doesNotMatch(eastern,/Time \(UTC\)/);
  } finally {setDisplayTimeZone('UTC');}
});
