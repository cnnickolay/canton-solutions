"""Loopback web dashboard for the demo vault."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_DOWN, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import random
import signal
import threading
import time
from urllib.parse import urlparse

import websocket

from stream import LedgerStream
from world import TICK, ApiError, BatchRunner, DemoVault, JsonApi, Unresolved

PAGE = Path(__file__).resolve().parent / 'page'
STATIC = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
          '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}


class Dashboard:
    def __init__(self, url, folder):
        self.stream = LedgerStream()
        self.stop = threading.Event()
        self.snapshot = {'ready': False}
        self.pool = ThreadPoolExecutor(max_workers=12)
        self.slots = threading.BoundedSemaphore(32)
        self.busy, self.busy_lock = set(), threading.Lock()
        print('Setting up parties, registries and the vault...', flush=True)
        self.vault = DemoVault(JsonApi(url), folder, self.stream.emit)

        class ToStream(logging.Handler):
            def emit(handler, record):
                self.stream.emit('error', record.getMessage())
        logging.getLogger('batcher').addHandler(ToStream())
        for loop in (lambda: self.stream.follow(self.vault, self.stop), self.refresh_loop,
                     self.batch_loop, self.load_loop):
            threading.Thread(target=loop, daemon=True).start()

    def queue_action(self, data):
        if not self.slots.acquire(blocking=False):
            raise ValueError('Too many actions waiting. Try again in a moment.')
        self.pool.submit(self.perform, data)

    def perform(self, data):
        try:
            result = self.vault.act(data)
            self.stream.emit('control', f"Done: {data['action']}")
            return result
        except Exception as error:
            self.stream.emit('error', f"{data['action']} failed: {str(error)[:800]}")
            logging.exception('demo action failed')
        finally:
            self.slots.release()

    def refresh_loop(self):
        while not self.stop.is_set():
            try:
                snapshot = self.vault.snapshot()
                recent = sum(t > time.time() - 60 for t in list(self.stream.batch_times))
                snapshot.update(ready=True, batchCount=self.stream.batch_count,
                                lastBatchAt=self.stream.last_batch_at, batchesPerMinute=recent,
                                prices=list(self.stream.prices), streamConnected=self.stream.connected,
                                batchCards=list(self.stream.batch_cards))
                self.snapshot = snapshot
            except Exception as error:
                self.stream.emit('error', f'Snapshot failed: {str(error)[:300]}')
            self.stop.wait(1)

    def batch_loop(self):
        last = None
        while not self.stop.is_set():
            if self.vault.autorun:
                try:
                    status = self.vault.run_batch()['status']
                    if status == 'halted' and last != 'halted':
                        self.stream.emit('halt', 'Batcher waiting while the vault is halted. Orders stay open.')
                    last = status
                except Exception as error:
                    # Contention, unknown outcomes and dropped connections are worth retrying.
                    retry = (isinstance(error, (Unresolved, OSError, TimeoutError, websocket.WebSocketException))
                             or (isinstance(error, ApiError) and BatchRunner.retryable(error)))
                    self.vault.autorun = retry
                    label = 'Batcher will retry or reconcile' if retry else 'Batcher stopped, fix the error and start it again'
                    self.stream.emit('error', f'{label}: {str(error)[:800]}')
            self.stop.wait(2)

    def load_loop(self):
        next_top_up = time.monotonic()
        while not self.stop.is_set():
            if not self.vault.loading or not self.snapshot.get('ready'):
                self.stop.wait(.2)
                next_top_up = time.monotonic()
                continue
            settings = dict(self.vault.load_settings)
            party = random.choice(self.vault.holders[:settings['active']])
            now = time.monotonic()
            if settings['topUpEvery'] and now >= next_top_up:
                party = self.vault.sponsor
                next_top_up = now + settings['topUpEvery']
            with self.busy_lock:
                free = party not in self.busy
                if free:
                    self.busy.add(party)
            if free:
                self.start_load_order(party, settings)
            self.stop.wait(1 / settings['rate'])

    def start_load_order(self, party, settings):
        wallet = next((w for w in self.snapshot['wallets'] if w['party'] == party), {})
        if party == self.vault.sponsor:
            kind = 'TopUp'
        else:
            kind = 'Deposit' if random.random() * 100 < settings['mix'] else 'Redeem'
        funds = Decimal(wallet.get('vusd' if kind == 'Redeem' else 'usd', 0))
        if kind == 'TopUp':
            size = Decimal(settings['topUpAmount'])
        else:
            low, high = float(settings['low']), float(settings['high'])
            size = Decimal(str(random.uniform(low, high))).quantize(TICK, rounding=ROUND_DOWN)
        size = min(size, funds)
        if size <= 0:
            with self.busy_lock:
                self.busy.discard(party)
            return
        price = Decimal(self.snapshot['price'])
        expected = size * price if kind == 'Redeem' else (size / price if price else Decimal(0))
        minimum = (expected * Decimal('.99')).quantize(TICK, rounding=ROUND_DOWN)
        self.pool.submit(self.load_order, party, kind, str(size), str(minimum))

    def load_order(self, party, kind, size, minimum):
        try:
            self.vault.order(party, kind, size, minimum)
        except Exception as error:
            self.stream.emit('error', f'Load {kind.lower()} for {self.vault.name(party)} failed: {str(error)[:500]}')
        finally:
            with self.busy_lock:
                self.busy.discard(party)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type='application/json'):
        raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        dashboard = self.server.dashboard
        path = urlparse(self.path).path
        if path in STATIC:
            name, content_type = STATIC[path]
            self.reply(200, (PAGE / name).read_bytes(), content_type)
        elif path == '/api/state':
            self.reply(200, dashboard.snapshot)
        elif path == '/api/events':
            self.reply(200, list(dashboard.stream.rows))
        elif path == '/events':
            self.server_sent_events(dashboard)
        else:
            self.reply(404, {'error': 'not found'})

    def server_sent_events(self, dashboard):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            last = int(self.headers.get('Last-Event-ID', '0'))
            while not dashboard.stop.is_set():
                with dashboard.stream.condition:
                    rows = [r for r in dashboard.stream.rows if r['id'] > last]
                for row in rows:
                    self.wfile.write(f"id: {row['id']}\nevent: ledger\ndata: {json.dumps(row, default=str)}\n\n".encode())
                    last = row['id']
                self.wfile.write(f"event: state\ndata: {json.dumps(dashboard.snapshot, default=str)}\n\n".encode())
                self.wfile.flush()
                dashboard.stop.wait(1)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass

    def do_POST(self):
        if self.path != '/api/action':
            self.reply(404, {'error': 'not found'})
            return
        origin = self.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            self.reply(403, {'error': 'open the dashboard on its own address'})
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('send JSON')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('bad request size')
            self.server.dashboard.queue_action(json.loads(self.rfile.read(length)))
            self.reply(202, {'queued': True})
        except (ValueError, KeyError) as error:
            self.reply(400, {'error': str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', default='http://127.0.0.1:18803')
    parser.add_argument('--port', type=int, default=8088)
    parser.add_argument('--data', required=True, help='folder for action journals')
    args = parser.parse_args()
    dashboard = Dashboard(args.ledger, args.data)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.dashboard = dashboard

    def shutdown(*_):
        dashboard.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'Dashboard ready at http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        dashboard.stop.set()
        server.server_close()
        dashboard.pool.shutdown(wait=True, cancel_futures=True)


if __name__ == '__main__':
    main()
