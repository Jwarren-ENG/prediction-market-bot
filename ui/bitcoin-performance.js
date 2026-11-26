import {arr, count, dash, day, esc, money, stamp} from './common.js';
import {venues} from './bitcoin-trading.js';

function dayTable(rows, previous) {
  if (!rows.length) return '';
  return `<div class="table-scroll" tabindex="0" role="region" aria-label="${previous?'Last completed':'Current'} Eastern day by Bitcoin contract account">
    <table class="data-table btc-trials"><thead><tr><th scope="col">Paper account</th><th scope="col">Eastern date</th><th scope="col">Opening equity</th><th scope="col">${previous?'Closing':'Marked'} equity</th><th scope="col">Day gain / loss</th><th scope="col">Fees</th><th scope="col">Fills</th></tr></thead><tbody>${rows.map(([venue,record])=>{
      const when=record?.as_of?stamp(record.as_of):'';
      return `<tr><th scope="row">${esc(venues[venue].label)}</th><td>${record?.date?esc(day(record.date,true)):dash}</td><td>${money(record?.opening_equity_cents)}</td>
        <td title="${esc(when)}">${money(record?.equity_cents)}</td><td>${money(record?.net_gain_cents,true)}${record?.unmarked_positions>0?'<small>Awaiting marks</small>':''}</td><td>${money(record?.fees_cents)}</td><td>${count(record?.filled_orders)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
}
export function trialPerformance(state) {
  if (!state.bitcoin_brokers) return '';
  const accounts=Object.keys(venues), today=accounts.map(v=>[v,state.bitcoin_brokers[v]?.daily]);
  const previous=[];
  for (const v of accounts) {
    const broker=state.bitcoin_brokers[v], latest=arr(broker?.daily_history).filter(d=>d?.date && (!broker.daily?.date || d.date<broker.daily.date)).sort((a,b)=>b.date.localeCompare(a.date))[0];
    if (latest) previous.push([v,latest]);
  }
  return `<section class="panel btc-trial-performance" aria-labelledby="btc-trials-title"><div class="panel-head"><h2 id="btc-trials-title">Bitcoin contract trials</h2><span class="small-note">Day ends at midnight Eastern</span></div>
    <div class="panel-body"><p class="btc-note">Each contract trial starts with its own $10,000 in paper cash. Day gain or loss includes fees and changes in the value of open positions.</p>${dayTable(today,false)}
    ${previous.length?`<details class="btc-benchmark" data-key="completed-eastern-day"><summary>Last completed Eastern day</summary>${dayTable(previous,true)}</details>`:''}</div></section>`;
}
