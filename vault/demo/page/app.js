'use strict';

const $ = id => document.getElementById(id);
const fmt = v => Number(v || 0).toLocaleString('en-US', {maximumFractionDigits: 10});
const fmt4 = v => Number(v || 0).toLocaleString('en-US', {maximumFractionDigits: 4});
const utc = v => new Date(v).toLocaleTimeString('en-GB', {timeZone: 'UTC'});
const short = v => (v || '').slice(0, 8);

let snap = null;
let frozen = false;
let held = [];
let ownMinimum = false;
let shownLoad = '';
let shownWaiting = '';
let shownCards = '';
let toastTimer;

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function toast(text) {
  $('toast').textContent = text;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4000);
}

async function send(data) {
  try {
    const r = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const body = await r.json();
    if (!r.ok) throw Error(body.error);
    toast('Sent. The ledger panel shows the result.');
  } catch (e) {
    toast(e.message);
  }
}

// Off-ledger estimate at the current price. The ledger computes the real output.
function estimate(reset = false) {
  if (reset) ownMinimum = false;
  if (!snap) return;
  const amount = Number($('amount').value);
  const price = Number(snap.price);
  const out = $('kind').value === 'redeem' ? amount * price : amount / price;
  $('estimate').textContent = 'Estimate ' + fmt(out) + ', minimum set 1% lower';
  if (!ownMinimum) $('minimum').value = (Math.floor(out * 0.99 * 1e10) / 1e10).toFixed(10).replace(/\.?0+$/, '') || '0';
}

function pick(party, kind) {
  $('holder').value = party;
  $('kind').value = kind;
  estimate(true);
  $('amount').focus();
}

function renderBook(s) {
  $('link').textContent = s.streamConnected ? 'Ledger connected' : 'Stream reconnecting';
  $('link').classList.toggle('warn', !s.streamConnected);
  $('managed').textContent = fmt4(s.managedAssets);
  $('managed').title = s.managedAssets;
  $('issued').textContent = fmt4(s.issuedShares);
  $('issued').title = s.issuedShares;
  $('price').textContent = Number(s.price).toFixed(4);
  $('halted').textContent = s.halted ? 'Halted' : 'Running';
  $('halted').classList.toggle('warn', s.halted);
  $('batch-count').textContent = s.batchCount;
  $('per-minute').textContent = s.batchesPerMinute;
  $('last-batch').textContent = s.lastBatchAt ? utc(s.lastBatchAt * 1000) : '-';
  $('ledger-cid').textContent = short(s.ledgerCid);
  $('ledger-cid').title = s.ledgerCid;
  $('autorun').textContent = s.autorun ? 'Stop batcher' : 'Start batcher';
  $('halt').textContent = s.halted ? 'Resume' : 'Halt';
  const b = s.balance;
  $('balance').textContent = (b.ok ? 'Balanced' : 'MISMATCH') + '. Reserve ' + fmt(b.reserve) + ', shares ' + fmt(b.supply) + ', ignored donations ' + fmt(b.donations);
  $('balance').className = 'note ' + (b.ok ? 'good' : 'bad');
}

function renderWallets(s) {
  const rows = s.wallets.map(w => {
    const tr = el('tr');
    tr.append(el('td', w.name));
    for (const [free, locked] of [['usd', 'lockedUsd'], ['vusd', 'lockedVusd']]) {
      const td = el('td', fmt(w[free]));
      if (Number(w[locked])) td.append(el('small', fmt(w[locked]) + ' locked'));
      tr.append(td);
    }
    tr.append(el('td', fmt(w.worth)));
    const actions = el('td');
    if (w.isHolder) {
      for (const [label, kind] of [['Deposit', 'deposit'], ['Redeem', 'redeem']]) {
        const b = el('button', label);
        b.onclick = () => pick(w.party, kind);
        actions.append(b);
      }
    }
    tr.append(actions);
    return tr;
  });
  $('wallets').replaceChildren(...rows);
  const holders = s.wallets.filter(w => w.isHolder);
  const current = $('holder').value;
  if ([...$('holder').options].map(o => o.value).join() !== holders.map(w => w.party).join()) {
    $('holder').replaceChildren(...holders.map(w => { const o = el('option', w.name); o.value = w.party; return o; }));
    if (holders.some(w => w.party === current)) $('holder').value = current;
    estimate();
  }
  $('active').max = holders.length;
  const usd = holders.reduce((t, w) => t + Number(w.usd) + Number(w.lockedUsd), 0);
  const vusd = holders.reduce((t, w) => t + Number(w.vusd) + Number(w.lockedVusd), 0);
  $('wallet-totals').replaceChildren(...[['Holders', holders.length], ['Holder USD, with locked', usd], ['Holder vUSD, with locked', vusd]]
    .map(([label, value]) => { const d = el('div', label); d.append(el('strong', fmt4(value))); return d; }));
}

function renderOrders(s) {
  $('open-count').textContent = s.orders.length;
  const rows = s.orders.map(o => {
    const tr = el('tr');
    const who = el('td', o.name); who.append(el('small', o.kind)); tr.append(who);
    const amount = el('td', fmt(o.amount));
    if (o.kind !== 'TopUp') amount.append(el('small', 'min ' + fmt(o.kind === 'Deposit' ? o.minShares : o.minAssets)));
    tr.append(amount);
    const left = Math.ceil((Date.parse(o.deadline) - Date.now()) / 1000);
    const deadline = el('td', left <= 0 ? 'Expired' : left + 's', left < 60 ? 'bad' : '');
    deadline.title = o.deadline;
    tr.append(deadline);
    const allocation = el('td', short(o.allocation)); allocation.title = o.allocation; tr.append(allocation);
    const cell = el('td');
    const withdraw = el('button', 'Withdraw');
    withdraw.disabled = left >= 0;
    withdraw.onclick = () => send({action: 'withdraw', cid: o.cid});
    cell.append(withdraw);
    tr.append(cell);
    return tr;
  });
  if (!rows.length) { const tr = el('tr'); const td = el('td', 'No open orders', 'muted'); td.colSpan = 5; tr.append(td); rows.push(tr); }
  $('orders').replaceChildren(...rows);
}

function renderRoles(s) {
  $('roles').replaceChildren(...s.roles.map(g => {
    const chip = el('div', undefined, 'chip');
    chip.append(el('span', g.name), el('small', g.permission));
    const b = el('button', 'Revoke', 'danger');
    b.onclick = () => send({action: 'revoke', cid: g.cid});
    chip.append(b);
    return chip;
  }));
}

function renderLoad(s) {
  const settings = s.loadSettings || {};
  const signature = JSON.stringify(settings);
  if (settings.rate && signature !== shownLoad) {
    for (const [id, key] of [['active', 'active'], ['rate', 'rate'], ['mix', 'mix'], ['low', 'low'], ['high', 'high'], ['top-up-every', 'topUpEvery'], ['top-up-amount', 'topUpAmount']]) $(id).value = settings[key];
    for (const id of ['active', 'rate', 'mix']) $(id + '-out').textContent = $(id).value + (id === 'mix' ? '%' : '');
    shownLoad = signature;
  }
  $('load').textContent = s.loading ? 'Stop load' : 'Start load';
  $('load-state').textContent = s.loading ? 'Running' : 'Stopped';
  $('unresolved').classList.toggle('hidden', !s.unresolved.length);
}

function tile(item, waiting = false) {
  const kind = item.kind.toLowerCase();
  const status = waiting ? 'waiting' : item.status;
  const t = el('div', undefined, 'tile ' + kind + ' ' + status);
  const owner = item.owner || item.name || '?';
  const words = owner.split(' ');
  const label = words.length > 1 && /^\d+$/.test(words.at(-1)) ? words[0].slice(0, 2) + words.at(-1) : owner.slice(0, 8);
  let text;
  if (waiting) text = fmt(item.amount);
  else if (status === 'deferred') text = item.reason || 'not executed';
  else if (kind === 'topup') text = '+' + fmt(item.amount) + ' USD';
  else if (kind === 'redeem') text = '-' + fmt(item.amount) + ' vUSD';
  else text = '+' + fmt(item.output) + ' vUSD';
  t.append(el('b', label), el('span', text));
  t.title = owner + ', ' + item.kind + ', ' + text + ', ' + (item.cid || '');
  t.dataset.order = item.cid || '';
  return t;
}

function tiles(items, waiting = false) {
  const box = el('div', undefined, 'tiles');
  box.append(...items.slice(0, 200).map(i => { const t = tile(i, waiting); if (items.length > 30) t.classList.add('dense'); return t; }));
  return box;
}

function renderBatches(s) {
  const waitingSig = JSON.stringify(s.orders);
  if (waitingSig !== shownWaiting) {
    shownWaiting = waitingSig;
    $('waiting-count').textContent = s.orders.length + ' open' + (s.orders.length > 200 ? ', first 200 shown' : '');
    const box = tiles(s.orders.map(o => ({...o, owner: o.name})), true);
    $('waiting').replaceChildren(...(s.orders.length ? box.childNodes : [el('span', 'Nothing waiting', 'muted')]));
  }
  const cardsSig = JSON.stringify(s.batchCards || []);
  if (cardsSig === shownCards) return;
  shownCards = cardsSig;
  const cards = (s.batchCards || []).map(b => {
    const card = el('article', undefined, 'card-batch');
    card.dataset.batch = b.number;
    const line = el('div', undefined, 'line');
    line.append(el('b', 'Batch ' + b.number + ', ' + utc(b.time)), el('span', b.executed + ' executed, ' + b.deferred + ' deferred'));
    const price = p => p === null ? '?' : Number(p).toFixed(4);
    const move = el('div', 'ledger ' + short(b.ledgerBefore) + ' to ' + short(b.ledgerAfter) + ', price ' + price(b.priceBefore) + ' to ' + price(b.priceAfter), 'move');
    move.title = b.ledgerBefore + ' to ' + b.ledgerAfter;
    card.append(line, move, tiles(b.tiles));
    return card;
  });
  $('cards').replaceChildren(...(cards.length ? cards : [el('div', 'Batches show up here once they run', 'empty')]));
}

function chart(points) {
  const c = $('chart');
  const ratio = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  c.width = w * ratio; c.height = h * ratio;
  const ctx = c.getContext('2d');
  ctx.scale(ratio, ratio);
  if (!points.length) return;
  const prices = points.map(p => Number(p.price));
  const low = Math.min(...prices) * 0.995, high = Math.max(...prices) * 1.005;
  const xy = prices.map((p, i) => [i * w / Math.max(1, prices.length - 1), h - 8 - (p - low) / (high - low || 1) * (h - 16)]);
  ctx.strokeStyle = '#6fd9b8';
  ctx.lineWidth = 2;
  ctx.beginPath();
  xy.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.stroke();
}

function render(s) {
  if (!s.ready) return;
  snap = s;
  renderBook(s);
  renderWallets(s);
  renderOrders(s);
  renderRoles(s);
  renderLoad(s);
  renderBatches(s);
  estimate();
  chart(s.prices);
}

function addRow(row) {
  if (frozen) { held.push(row); if (held.length > 600) held.shift(); return; }
  const feed = $('feed');
  const atTop = feed.scrollTop < 20, before = feed.scrollHeight;
  feed.querySelector('.empty')?.remove();
  const item = el('article', undefined, 'event ' + row.kind);
  const top = el('div', undefined, 'top');
  const ref = row.command ? 'cmd ' + short(row.command) : row.updateId ? 'tx ' + short(row.updateId) : 'service';
  top.append(el('span', row.kind, 'kind'), el('span', utc(row.ledgerTime || row.time * 1000)), el('code', ref));
  item.append(top);
  for (const line of row.lines) item.append(el('div', line));
  if (row.details?.length) {
    const d = el('details');
    d.append(el('summary', row.details.length + ' contract events'));
    for (const text of row.details) d.append(el('div', text));
    item.append(d);
  }
  feed.prepend(item);
  while (feed.children.length > 250) feed.lastChild.remove();
  if (!atTop) feed.scrollTop += feed.scrollHeight - before;
}

const events = new EventSource('/events');
events.addEventListener('state', e => render(JSON.parse(e.data)));
events.addEventListener('ledger', e => addRow(JSON.parse(e.data)));
events.onerror = () => { $('link').textContent = 'Dashboard reconnecting'; };

$('place').onclick = () => send({action: $('kind').value, party: $('holder').value, amount: $('amount').value, minimum: $('minimum').value, lifetime: Number($('lifetime').value)});
for (const id of ['kind', 'amount', 'holder']) $(id).oninput = () => estimate(true);
$('minimum').oninput = () => { ownMinimum = true; };
$('autorun').onclick = () => send({action: 'autorun', enabled: !snap?.autorun});
$('halt').onclick = () => send({action: snap?.halted ? 'resume' : 'halt'});
$('send-top-up').onclick = () => send({action: 'topup', amount: $('top-up').value});
$('dust').onclick = () => send({action: 'dust', party: $('holder').value});
$('donate').onclick = () => send({action: 'donate', party: $('holder').value, amount: '1'});
$('add-one').onclick = () => send({action: 'wallets', name: $('new-name').value, amount: $('new-funds').value});
for (const b of document.querySelectorAll('[data-add]')) b.onclick = () => send({action: 'wallets', count: Number(b.dataset.add), amount: $('new-funds').value, name: 'Holder'});
for (const b of document.querySelectorAll('[data-action]')) b.onclick = () => send({action: b.dataset.action});
for (const b of document.querySelectorAll('[data-role]')) b.onclick = () => send({action: 'replace', role: b.dataset.role});
for (const id of ['active', 'rate', 'mix']) $(id).oninput = () => { $(id + '-out').textContent = $(id).value + (id === 'mix' ? '%' : ''); };
$('load').onclick = () => send({action: 'load', enabled: !snap?.loading, active: Number($('active').value), rate: Number($('rate').value), mix: Number($('mix').value), low: $('low').value, high: $('high').value, topUpEvery: Number($('top-up-every').value), topUpAmount: $('top-up-amount').value});
$('freeze').onclick = () => {
  frozen = !frozen;
  $('freeze').textContent = frozen ? 'Resume view' : 'Pause view';
  if (!frozen) { const rows = held; held = []; rows.forEach(addRow); }
};
