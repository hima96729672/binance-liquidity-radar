import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SPOT = os.getenv('BINANCE_SPOT_URL', 'https://api.binance.com').rstrip('/')
FUTURES = os.getenv('BINANCE_FUTURES_URL', 'https://fapi.binance.com').rstrip('/')
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = os.environ['TELEGRAM_CHAT_ID']
MAX_SYMBOLS = int(os.getenv('MAX_SYMBOLS', '120'))
MIN_QUOTE_VOLUME = float(os.getenv('MIN_QUOTE_VOLUME', '3000000'))
SPIKE_RATIO = float(os.getenv('SPIKE_RATIO', '3.0'))
COOLDOWN_HOURS = float(os.getenv('COOLDOWN_HOURS', '6'))
STATE_FILE = 'state.json'


def get_json(url, params=None):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'BinanceLiquidityRadar/1.0', 'Accept': 'application/json'})
    host = urllib.parse.urlparse(url).netloc
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            raw = response.read()
            print('Data API:', host, 'HTTP:', response.status, 'Content-Type:', response.headers.get('Content-Type'), flush=True)
    except urllib.error.HTTPError as exc:
        preview = exc.read(180).decode('utf-8', errors='replace')
        raise RuntimeError(f'Data API {host} returned HTTP {exc.code}: {preview!r}') from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:180].decode('utf-8', errors='replace')
        raise RuntimeError(f'Data API {host} returned invalid JSON: {preview!r}') from exc


def telegram(message):
    body = urllib.parse.urlencode({'chat_id': CHAT, 'text': message, 'disable_web_page_preview': 'true'}).encode()
    req = urllib.request.Request('https://api.telegram.org/bot' + TOKEN + '/sendMessage', data=body)
    with urllib.request.urlopen(req, timeout=18) as response:
        return json.load(response)


def main():
    now = time.time()
    state = {}
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            print('Warning: invalid state file; starting with empty state', flush=True)
    futures = get_json(FUTURES + '/fapi/v1/exchangeInfo')
    if not isinstance(futures, dict) or not isinstance(futures.get('symbols'), list):
        raise RuntimeError('Futures API returned unexpected data structure')
    active = {s['symbol'] for s in futures['symbols'] if s['status'] == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s['quoteAsset'] == 'USDT'}
    spot = get_json(SPOT + '/api/v3/ticker/24hr')
    if not isinstance(spot, list):
        raise RuntimeError('Spot API returned unexpected data structure')
    candidates = sorted((s for s in spot if s['symbol'] in active and float(s.get('quoteVolume', 0)) >= MIN_QUOTE_VOLUME), key=lambda s: float(s['quoteVolume']), reverse=True)[:MAX_SYMBOLS]
    alerts = []
    for item in candidates:
        symbol = item['symbol']
        try:
            bars = get_json(SPOT + '/api/v3/klines', {'symbol': symbol, 'interval': '5m', 'limit': 26})
            if len(bars) < 26:
                continue
            closed = bars[:-1]
            recent = float(closed[-1][7])
            baseline = sum(float(b[7]) for b in closed[:-1]) / (len(closed) - 1)
            ratio = recent / baseline if baseline else 0
            if ratio < SPIKE_RATIO or recent < 50000:
                continue
            previous_alert = float(state.get(symbol, 0))
            if now - previous_alert < COOLDOWN_HOURS * 3600:
                continue
            price_change = (float(closed[-1][4]) / float(closed[-1][1]) - 1) * 100
            alerts.append((ratio, symbol, recent, price_change, float(item['quoteVolume'])))
        except Exception as exc:
            print('Skip', symbol, type(exc).__name__, str(exc)[:120], flush=True)
        time.sleep(0.12)
    for ratio, symbol, recent, change, volume24 in sorted(alerts, reverse=True)[:8]:
        msg = (f'🚨 Binance Spot Liquidity Watch\n'
               f'Coin: {symbol}\n'
               f'5m volume spike: {ratio:.1f}x baseline\n'
               f'Last CLOSED 5m spot volume: ${recent:,.0f}\n'
               f'24h Binance spot volume: ${volume24:,.0f}\n'
               f'5m candle change: {change:+.2f}%\n'
               f'Futures USDT perpetual: active\n'
               f'⚠️ Watch only — NOT a confirmed trade. Market cap, OI, spread and depth NOT verified.')
        telegram(msg)
        state[symbol] = now
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    print(datetime.now(timezone.utc).isoformat(), 'checked', len(candidates), 'alerts', min(len(alerts), 8))


if __name__ == '__main__':
    main()
