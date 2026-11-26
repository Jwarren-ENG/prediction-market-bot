const {test, before, afterEach} = require('node:test');
const assert = require('node:assert/strict');

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
