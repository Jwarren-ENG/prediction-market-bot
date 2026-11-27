import {clock, day, esc, instant, money, stamp, tone} from './common.js';
import {quoteRanges} from './markets.js';
import {isUtcDisplay, utcClock} from './time.js';
import {venues} from './bitcoin-trading.js';

export const equityWindows = new Map([[15,'15m'],[60,'1h'],[240,'4h'],[1440,'1D'],[2880,'48h'],[10080,'1W']]);
export const equityAccounts = new Map([['bankroll','My paper bankroll'],['bitcoin_total','Combined $10,000 trials'],...Object.entries(venues).map(([id,v])=>[id,v.label]),['spot','Bitstamp spot']]);
const fullTime = at => isUtcDisplay() ? stamp(at) : stamp(at)+' · '+utcClock(at);
const figure = new Intl.NumberFormat('en-US',{maximumFractionDigits:4});
const percentChange = (change, initial) => initial > 0 ? ' ('+(change<0?'−':change>0?'+':'')+figure.format(Math.abs(change/initial*100))+'%)' : '';

export function equityPlot(series) {
  const points = series.points;
  if (!points.length) return null;
  const values = points.map(p=>p[1]), low = Math.min(...values), high = Math.max(...values);
  const pad = low===high ? 100 : Math.max(1,(high-low)*.08);
  const rawStep = Math.max(1,(high-low+2*pad)/4);
  const unit = 10**Math.floor(Math.log10(rawStep));
  const step = [1,2,5,10].find(n=>n*unit>=rawStep)*unit;
  const floor = Math.max(0,Math.floor((low-pad)/step)*step);
  const ceiling = Math.ceil((high+pad)/step)*step;
  const end = series.end_ms;
  const start = Math.min(end-60000, Math.max(series.start_ms,instant(series.first_at)?.getTime()??series.start_ms));
  const x = t => Math.max(0,Math.min(100,(t-start)/(end-start)*100));
  const y = value => (ceiling-value)/(ceiling-floor)*100;
  const ticks = [];
  for (let n=ceiling;n>=floor;n-=step) ticks.push(n);
  return {points,start,end,x,y,ticks,low,high};
}

function timeAxis(plot) {
  const duration=plot.end-plot.start, dates=duration>=86400000 || day(plot.start)!==day(plot.end);
  return [0,25,50,75,100].map(p=>{
    const at=plot.start+duration*p/100;
    const label=dates ? day(at,true)+(duration<3*86400000?' · '+clock(at).slice(0,5):'') : duration<300000?clock(at):clock(at).slice(0,5);
    return '<span style="left:'+p+'%">'+esc(label)+'</span>';
  }).join('');
}

function drawing(plot, label) {
  let previousSegment = null;
  const lines=[], singletons=[];
  let run=[];
  function finish() {
    if (!run.length) return;
    if (run.length===1) singletons.push('M'+run[0]+'h.001');
    else lines.push('M'+run.join(' L'));
    run=[];
  }
  for (const [at,cents,segment] of plot.points) {
    if (segment!==previousSegment) finish();
    run.push((plot.x(at)*10).toFixed(3)+' '+(plot.y(cents)*2).toFixed(3));
    previousSegment=segment;
  }
  finish();
  const first=plot.points[0], last=plot.points.at(-1);
  const rows=plot.ticks.map(n=>'<span style="top:'+plot.y(n)+'%">'+esc(money(n))+'</span>').join('');
  const grid=plot.ticks.map(n=>'<line x1="0" x2="1000" y1="'+(plot.y(n)*2)+'" y2="'+(plot.y(n)*2)+'"/>').join('');
  const baseline=(plot.y(first[1])*2).toFixed(3);
  return `<div class="equity-graph"><div class="equity-scale" aria-hidden="true">${rows}</div>
    <div class="equity-track" data-equity-track tabindex="0" role="slider" aria-label="${esc(label)} recorded portfolio values" aria-describedby="equity-help" aria-valuemin="1" aria-valuemax="${plot.points.length}" aria-valuenow="${plot.points.length}" aria-valuetext="${esc(money(last[1])+' · '+fullTime(last[0]))}">
      <svg viewBox="0 0 1000 200" preserveAspectRatio="none" aria-hidden="true"><g class="equity-grid">${grid}</g>
        <path class="equity-baseline" d="M0 ${baseline}H1000"/><path class="equity-line" d="${lines.join(' ')}"/>
        <path class="equity-single" d="${singletons.join(' ')}"/></svg>
      <span class="equity-end" style="left:${plot.x(last[0])}%;top:${plot.y(last[1])}%"></span>
      <div class="equity-cursor" hidden><i></i></div>
    </div><div class="equity-axis" aria-hidden="true">${timeAxis(plot)}</div></div>
    <p id="equity-help" class="sr-only">Move over the line or use the left and right arrow keys to inspect recorded values. Home and End select the first and last value.</p>`;
}

export function portfolioChart(state, ui, desk) {
  const bitcoin=desk==='bitcoin', account=bitcoin?(ui.equityAccount||(state.bankroll?'bankroll':'kalshi')):state.mode;
  const label=bitcoin?equityAccounts.get(account):state.mode==='replay'?'Central Park weather · Replay':'Central Park weather';
  const minutes=ui.equityMinutes||240, raw=state.portfolio_history;
  const s=raw?.account===account && raw.minutes===minutes ? raw : null;
  const selector=bitcoin ? `<div class="equity-account"><label for="equity-account">Paper account</label><select id="equity-account">${[...equityAccounts].map(([key,name])=>`<option value="${key}"${key===account?' selected':''}>${esc(name)}</option>`).join('')}</select></div>` : '';
  const choices=[...equityWindows].map(([n,label])=>`<button type="button" data-action="equity-range" data-value="${n}" aria-label="${n===10080?'1 week':n===1440?'1 day':quoteRanges.get(n)}" aria-pressed="${n===minutes}">${label}</button>`).join('');
  const header=`<div class="equity-header"><h2 id="equity-title">Portfolio value</h2>${selector}</div>`;
  const picker=`<div class="equity-ranges" role="group" aria-label="Portfolio time range">${choices}</div>`;
  const plot=s && equityPlot(s), first=plot?.points[0], last=plot?.points.at(-1);
  let reading='Loading account history…', change='';
  if (s) {
    reading=last?'Last known value. Awaiting a fresh valuation.':'Awaiting a complete valuation.';
    if (first && s.current_cents!==null && plot.points.length>1) {
      const diff=s.current_cents-first[1];
      change=`<strong class="${tone(diff)}">${money(diff,true)}${percentChange(diff,first[1])}</strong>`;
      reading='since '+stamp(first[0]);
    } else if (first && s.current_cents!==null) reading='First valuation recorded.';
  }
  const amount=s?.current_cents??last?.[1];
  const stampLine=s?(ui.connected===false?'Worker disconnected':account==='replay'?'Replay valuation':s.current_cents===null?'Waiting for a fresh mark':'Updated'):'';
  const asOf=s?.current_cents===null ? last?.[0] : s?.end_ms;
  const lastText=asOf==null?stampLine:stampLine+' · '+stamp(asOf);
  const lowHigh=plot ? `<div class="equity-bounds"><span>Low <b>${money(plot.low)}</b></span><span>High <b>${money(plot.high)}</b></span></div>` : '';
  return `<section class="panel equity-card" data-key="portfolio-equity-${esc(account)}" aria-labelledby="equity-title">${header}
    <div class="equity-top"><div><p class="equity-amount">${esc(money(amount))}</p>
      <p class="equity-change">${change} ${esc(reading)}</p></div>${picker}</div>
    ${plot?drawing(plot,label):`<div class="equity-empty">${s?'No complete portfolio valuation in this window.':'Loading the recorded line…'}</div>`}
    <div class="equity-bottom"><span class="equity-readout" data-default="${esc(lastText)}">${esc(lastText)}</span>${lowHigh}</div>
    ${account==='bitcoin_total'?'<p class="equity-gap">Total across the three Bitcoin contract accounts and the Bitstamp spot strategy.</p>':''}
    ${s?.has_gaps?'<p class="equity-gap">Breaks in the line show periods without a complete valuation.</p>':''}</section>`;
}

// Keep the native focus target and inspected instant stable when the workspace patches its DOM.
export function installEquityInspector(getSeries) {
  let cursor=null, previousKey='';
  const getPlot=()=>{
    const track=document.querySelector('[data-equity-track]'), s=getSeries();
    if (!track || !s) { cursor=null;previousKey='';return null; }
    const key=s.account+'/'+s.minutes;
    if (key!==previousKey) { cursor=null;previousKey=key; }
    return {track,plot:equityPlot(s)};
  };
  function nearest(points,at) {
    let lo=0,hi=points.length-1;
    while (lo<hi) {
      const mid=(lo+hi)>>1;
      if (points[mid][0]<at) lo=mid+1;else hi=mid;
    }
    return lo>0 && at-points[lo-1][0]<points[lo][0]-at ? lo-1 : lo;
  }
  function show(context,index) {
    const {track,plot}=context, cursorLine=track.querySelector('.equity-cursor');
    const readout=track.closest('.equity-card').querySelector('.equity-readout');
    const point=plot.points[index];
    cursor=point?.[0]??null;
    const active=point??plot.points.at(-1);
    const label=money(active[1])+' · '+fullTime(active[0]);
    track.setAttribute('aria-valuenow',String(point?index+1:plot.points.length));
    track.setAttribute('aria-valuetext',label);
    readout.textContent=point?label:readout.dataset.default;
    cursorLine.hidden=!point;
    if (point) { cursorLine.style.left=plot.x(point[0])+'%';cursorLine.firstElementChild.style.top=plot.y(point[1])+'%'; }
  }
  function pointer(event) {
    if (!event.target.closest?.('[data-equity-track]')) return;
    const c=getPlot();
    if (!c?.plot) return;
    const r=c.track.getBoundingClientRect(), at=c.plot.start+(event.clientX-r.left)/r.width*(c.plot.end-c.plot.start);
    show(c,nearest(c.plot.points,at));
  }
  document.addEventListener('pointermove',pointer);
  document.addEventListener('pointerdown',pointer);
  document.addEventListener('pointerout',event=>{
    const node=event.target.closest?.('[data-equity-track]');
    if (node && !node.contains(event.relatedTarget) && event.pointerType!=='touch') { const c=getPlot();if (c?.plot) show(c,-1); }
  });
  document.addEventListener('focusout',event=>{if(event.target.matches('[data-equity-track]')) { const c=getPlot();if(c?.plot) show(c,-1); }});
  document.addEventListener('keydown',event=>{
    if (!event.target.matches('[data-equity-track]') || !['ArrowLeft','ArrowRight','Home','End','Escape'].includes(event.key)) return;
    const c=getPlot();if (!c?.plot) return;
    event.preventDefault();
    const points=c.plot.points, last=points.length-1, position=cursor===null?last:nearest(points,cursor);
    const choices={Home:0,End:last,ArrowLeft:Math.max(0,position-1),ArrowRight:Math.min(last,position+1),Escape:-1};
    show(c,choices[event.key]);
  });
  return ()=>{
    const c=getPlot();if(c?.plot && cursor!==null) show(c,nearest(c.plot.points,cursor));
  };
}
