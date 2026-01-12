import os
import threading
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import time
from datetime import datetime
from collections import deque
from dotenv import load_dotenv
import base64  # for screenshot logging

load_dotenv()

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIG
# ==========================================
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
CMC_API_KEY = os.getenv('CMC_API_KEY')

if not ETHERSCAN_API_KEY:
    print("⚠️ ETHERSCAN_API_KEY missing → whale tracking disabled")
if not CMC_API_KEY:
    print("⚠️ CMC_API_KEY missing → using CoinGecko fallback")

ETHERSCAN_URL = 'https://api.etherscan.io/api'
CMC_URL = 'https://pro-api.coinmarketcap.com'

REQUESTS_PER_SECOND = 2
request_times = deque(maxlen=REQUESTS_PER_SECOND * 2)

cache = {
    'whale_transactions': {},
    'prices': {},
}

TOKEN_ADDRESSES = {
    'ETH': '0x0000000000000000000000000000000000000000',
    'USDT': '0xdac17f958d2ee523a2206206994597c13d831ec7',
    'USDC': '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
    'LINK': '0x514910771af9ca656af840dff83e8264ecf986ca',
    'UNI': '0x1f9840a85d5af5bf1d1762f925bdaddc4201f984',
    'AAVE': '0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9',
    'WBTC': '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599',
    'DAI': '0x6b175474e89094c44da98b954eedeac495271d0f',
}

COIN_SYMBOLS = [
    'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'DOGE', 'AVAX', 'MATIC', 'LINK',
    'DOT', 'ATOM', 'ALGO', 'ICP', 'LTC', 'ETC', 'VET', 'FIL', 'UNI', 'AAVE',
    'GRT', 'APT', 'NEAR', 'LDO', 'TON', 'CAKE', 'CFX', 'JTO', 'ORDI', 'KAS',
    'SAND', 'QNT', 'PYTH', 'TRB', 'TRX', 'SUI', 'APE', 'OP', 'JUP', 'TIA',
    'WIF', 'OM', 'MYRO', 'SEI', 'INJ', 'RUNE', 'ARB', 'PEPE', 'SHIB', 'CRV',
    'MKR', 'RAY', 'PENDLE', 'STRK', 'FET', 'TAO', 'ARKM', 'IMX', 'RNDR'
]

data_lock = threading.Lock()

def wait_for_rate_limit():
    now = time.time()
    with data_lock:
        while len(request_times) >= REQUESTS_PER_SECOND:
            oldest = request_times[0]
            if now - oldest < 1:
                time.sleep(1 - (now - oldest) + 0.1)
            else:
                request_times.popleft()
        request_times.append(now)

# ==========================================
# FREE BINANCE FUNDING
# ==========================================
def get_binance_funding(symbol):
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}USDT"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        d = r.json()
        if 'lastFundingRate' in d:
            rate = float(d['lastFundingRate']) * 100
            print(f"✅ Binance funding: {symbol} = {rate:.4f}%")
            return rate
    except Exception as e:
        print(f"Binance funding failed for {symbol}: {e}")
    return None

# ==========================================
# CLIENTS
# ==========================================
class CoinMarketCapClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({
                'X-CMC_PRO_API_KEY': self.api_key,
                'Accept': 'application/json'
            })

    def get_quotes_latest(self, symbols):
        if not self.api_key:
            print("CMC: no key")
            return None
        wait_for_rate_limit()
        try:
            params = {'symbol': symbols.upper(), 'convert': 'USD'}
            r = self.session.get(f"{CMC_URL}/v2/cryptocurrency/quotes/latest", params=params, timeout=10)
            r.raise_for_status()
            d = r.json()
            return d.get('data', {})
        except Exception as e:
            print(f"❌ CMC error: {e}")
            return {}

class EtherscanClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()

    def _make_request(self, params):
        wait_for_rate_limit()
        params['apikey'] = self.api_key
        try:
            response = self.session.get(ETHERSCAN_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if data.get('status') == '1':
                return data.get('result')
            else:
                print(f"⚠️ Etherscan: {data.get('message')}")
                return None
        except Exception as e:
            print(f"❌ Etherscan failed: {e}")
            return None

    def get_whale_transactions(self, token_address, hours=24):
        latest_block = self._make_request({'module': 'proxy', 'action': 'eth_blockNumber'})
        if not latest_block:
            return []
        try:
            end_block = int(latest_block, 16)
        except:
            return []
        blocks_per_hour = 300
        start_block = end_block - (blocks_per_hour * hours)
        params = {
            'module': 'account',
            'action': 'tokentx',
            'contractaddress': token_address,
            'startblock': start_block,
            'endblock': end_block,
            'sort': 'desc'
        }
        return self._make_request(params) or []

# ==========================================
# WHALE TRACKER
# ==========================================
class WhaleTracker:
    def __init__(self, etherscan_client):
        self.client = etherscan_client
        self.whale_threshold_usd = 500000

    def get_exchange_wallets(self):
        return {
            '0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be'.lower(),
            '0xd551234ae421e3bcba99a0da6d736074f22192ff'.lower(),
            '0x564286362092d8e7936f0549571a803b203aaced'.lower(),
            '0x881d40237659c251811cec9c364ef91dc08d300c'.lower()
        }

    def analyze_whale_flows(self, token_symbol, hours=24):
        token_address = TOKEN_ADDRESSES.get(token_symbol.upper())
        if not token_address:
            return {'net_flow': 0, 'buy_pressure': 0, 'sell_pressure': 0, 'whale_count': 0}

        cache_key = f"whale_{token_symbol}_{hours}"
        now = time.time()
        if cache_key in cache['whale_transactions']:
            cached_data, timestamp = cache['whale_transactions'][cache_key]
            if now - timestamp < 300:
                return cached_data

        transactions = self.client.get_whale_transactions(token_address, hours)
        if not transactions:
            return {'net_flow': 0, 'buy_pressure': 0, 'sell_pressure': 0, 'whale_count': 0}

        net_flow = buy_pressure = sell_pressure = 0
        whale_wallets = set()
        exchange_wallets = self.get_exchange_wallets()

        for tx in transactions:
            try:
                value = int(tx.get('value', 0)) / 10**int(tx.get('tokenDecimal', 18))
                from_addr = tx.get('from', '').lower()
                to_addr = tx.get('to', '').lower()
                price = cache['prices'].get(token_symbol, {}).get('price', 2000)
                usd_value = value * price

                if usd_value > self.whale_threshold_usd:
                    whale_wallets.add(from_addr)
                    whale_wallets.add(to_addr)

                    if to_addr in exchange_wallets:
                        sell_pressure += usd_value
                        net_flow -= usd_value
                    elif from_addr in exchange_wallets:
                        buy_pressure += usd_value
                        net_flow += usd_value
            except Exception as e:
                print(f"Transaction parse error: {e}")
                continue

        result = {
            'net_flow': net_flow,
            'buy_pressure': buy_pressure,
            'sell_pressure': sell_pressure,
            'whale_count': len(whale_wallets),
            'timestamp': datetime.now().isoformat()
        }

        cache['whale_transactions'][cache_key] = (result, now)
        return result

# ==========================================
# DATA AGGREGATOR
# ==========================================
class DataAggregator:
    def __init__(self):
        self.cmc = CoinMarketCapClient(CMC_API_KEY)
        self.cg_session = requests.Session()
        self.cg_base = 'https://api.coingecko.com/api/v3'
        self.whale_tracker = WhaleTracker(EtherscanClient(ETHERSCAN_API_KEY)) if ETHERSCAN_API_KEY else None

    def get_price_data(self, symbol):
        coin_id = symbol.upper()

        # CMC priority
        quotes = self.cmc.get_quotes_latest(coin_id)
        if quotes and coin_id in quotes:
            quote = quotes[coin_id][0]['quote']['USD']
            price = quote.get('price')
            if price is not None:
                result = {
                    'price': price,
                    'change24h': quote.get('percent_change_24h', 0)
                }
                cache['prices'][symbol] = result
                print(f"✅ CMC Price fetched: {symbol} = ${price:.2f}")
                return result
            else:
                print(f"⚠️ CMC returned null price for {symbol}")

        # CoinGecko fallback
        coin_id_lower = symbol.lower()
        try:
            url = f"{self.cg_base}/simple/price?ids={coin_id_lower}&vs_currencies=usd&include_24hr_change=true"
            response = self.cg_session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            if coin_id_lower in data and 'usd' in data[coin_id_lower]:
                result = {
                    'price': data[coin_id_lower]['usd'],
                    'change24h': data[coin_id_lower].get('usd_24h_change', 0)
                }
                cache['prices'][symbol] = result
                print(f"✅ CoinGecko fallback: {symbol} = ${result['price']:.2f}")
                return result
        except Exception as e:
            print(f"❌ CoinGecko fallback error for {symbol}: {e}")

        print(f"⚠️ No price data for {symbol}")
        return None

    def aggregate_coin_data(self, symbol):
        price_data = self.get_price_data(symbol)
        if not price_data:
            return None

        funding = get_binance_funding(symbol)

        whale_flows = self.whale_tracker.analyze_whale_flows(symbol) if self.whale_tracker and symbol.upper() in TOKEN_ADDRESSES else None

        return {
            'symbol': symbol,
            'price': price_data['price'],
            'change24h': price_data['change24h'],
            'funding': funding,
            'whale_flows': whale_flows,
            'source': 'CMC' if CMC_API_KEY else 'CoinGecko',
            'timestamp': datetime.now().isoformat()
        }

# ==========================================
# SIGNAL GENERATOR (with mild fallback)
# ==========================================
class SignalGenerator:
    def __init__(self, whale_tracker):
        self.whale_tracker = whale_tracker
        self.btc_regime = 'distribution'

    def generate_signals(self, coin_data, timeframe):
        base_signal = self._generate_base_signal(coin_data, timeframe)
        if coin_data['symbol'] in TOKEN_ADDRESSES and self.whale_tracker:
            whale_flows = coin_data.get('whale_flows')
            if whale_flows and whale_flows['whale_count'] > 0:
                return self._enhance_with_whale_data(base_signal, whale_flows)
        return base_signal

    def _generate_base_signal(self, coin_data, timeframe):
        symbol = coin_data['symbol']
        change24h = coin_data['change24h']
        funding = coin_data.get('funding')

        signal = {
            'direction': 'FLAT',
            'label': 'NO EDGE',
            'score': 0,
            'risk': 'MEDIUM',
            'target': '',
            'whaleProb': 60
        }

        if timeframe == 'scalp' and funding is not None:
            abs_funding = abs(funding)
            if abs_funding > 0.05:
                direction = 'SHORT' if funding > 0 else 'LONG'
                signal = {
                    'direction': direction,
                    'label': 'EXTREME SQUEEZE',
                    'score': 90,
                    'risk': 'HIGH',
                    'target': f"2-3% {'down' if funding > 0 else 'up'}",
                    'whaleProb': 85
                }
            elif abs_funding > 0.025:
                direction = 'SHORT' if funding > 0 else 'LONG'
                signal = {
                    'direction': direction,
                    'label': 'FUNDING PRESSURE',
                    'score': 70,
                    'risk': 'MEDIUM',
                    'target': f"1.5-2% {'down' if funding > 0 else 'up'}",
                    'whaleProb': 70
                }
            elif abs_funding > 0.005:
                direction = 'SHORT' if funding > 0 else 'LONG'
                signal = {
                    'direction': direction,
                    'label': 'LIGHT FUNDING BIAS',
                    'score': 55,
                    'risk': 'LOW',
                    'target': '0.5-1.5% move',
                    'whaleProb': 60
                }

        elif timeframe == 'day':
            abs_change = abs(change24h)
            if abs_change > 12:
                direction = 'LONG' if change24h > 0 else 'SHORT'
                signal = {
                    'direction': direction,
                    'label': 'BREAKOUT' if change24h > 0 else 'CAPITULATION',
                    'score': 80,
                    'risk': 'HIGH',
                    'target': f"4-6% {'up' if change24h > 0 else 'down'}",
                    'whaleProb': 80
                }
            elif abs_change > 6:
                if change24h > 0 and self.btc_regime == 'accumulation':
                    signal = {'direction': 'LONG', 'label': 'BTC BETA PLAY', 'score': 65, 'risk': 'MEDIUM', 'target': '3-4% up', 'whaleProb': 65}
                elif change24h < 0 and self.btc_regime == 'distribution':
                    signal = {'direction': 'SHORT', 'label': 'DISTRIBUTION SELL', 'score': 65, 'risk': 'MEDIUM', 'target': '3-4% down', 'whaleProb': 65}
            elif abs_change > 2:
                direction = 'LONG' if change24h > 0 else 'SHORT'
                signal = {
                    'direction': direction,
                    'label': 'MILD MOMENTUM',
                    'score': 52,
                    'risk': 'LOW',
                    'target': f"1-3% {'up' if change24h > 0 else 'down'}",
                    'whaleProb': 60
                }

        elif timeframe == 'swing':
            if symbol == 'BTC':
                direction = 'LONG' if self.btc_regime == 'accumulation' else 'SHORT'
                signal = {
                    'direction': direction,
                    'label': f'{self.btc_regime.upper()} PHASE',
                    'score': 90,
                    'risk': 'LOW',
                    'target': f"20-40% {'up' if direction == 'LONG' else 'down'}",
                    'whaleProb': 95
                }
            elif self.btc_regime == 'accumulation' and 5 < change24h < 15:
                signal = {'direction': 'LONG', 'label': 'POSITION BUILD', 'score': 75, 'risk': 'MEDIUM', 'target': '15-30% up', 'whaleProb': 75}
            elif abs(change24h) > 3:
                direction = 'LONG' if change24h > 0 else 'SHORT'
                signal = {
                    'direction': direction,
                    'label': 'SWING MOMENTUM',
                    'score': 55,
                    'risk': 'MEDIUM',
                    'target': f"5-15% {'up' if change24h > 0 else 'down'}",
                    'whaleProb': 65
                }

        return signal

    def _enhance_with_whale_data(self, signal, whale_flows):
        net_flow = whale_flows['net_flow']
        whale_count = whale_flows['whale_count']
        flow_boost = 0
        if signal['direction'] == 'LONG' and net_flow > 100000:
            flow_boost = min(20, net_flow / 100000)
        elif signal['direction'] == 'SHORT' and net_flow < -100000:
            flow_boost = min(20, abs(net_flow) / 100000)
        whale_boost = min(10, whale_count * 2)
        enhanced_score = min(95, signal['score'] + flow_boost + whale_boost)
        enhanced = {
            **signal,
            'score': int(enhanced_score),
            'whale_flow': net_flow,
            'whale_count': whale_count
        }
        print(f"Whale enhanced {signal['direction']} signal: {signal['score']}% → {enhanced['score']}%")
        return enhanced

# ==========================================
# INITIALIZE
# ==========================================
aggregator = DataAggregator()
signal_generator = SignalGenerator(aggregator.whale_tracker)

# ==========================================
# ROUTES
# ==========================================
@app.route('/')
def serve_frontend():
    return send_from_directory('.', 'index.html')

@app.route('/api/debug')
def debug_apis():
    results = {}
    try:
        quotes = aggregator.cmc.get_quotes_latest('BTC')
        results['cmc'] = {'status': 'Connected' if quotes else 'Failed', 'btc_price': quotes['BTC'][0]['quote']['USD']['price'] if quotes else 'N/A'}
    except Exception as e:
        results['cmc'] = {'status': 'Failed', 'error': str(e)}
    try:
        price = aggregator.get_price_data('BTC')
        results['coingecko'] = {'status': 'Connected' if price else 'Failed', 'btc_price': price['price'] if price else 'N/A'}
    except Exception as e:
        results['coingecko'] = {'status': 'Failed', 'error': str(e)}
    results['etherscan'] = 'Connected' if ETHERSCAN_API_KEY else 'Disabled'
    return jsonify(results)

@app.route('/api/signals/<timeframe>')
def get_signals(timeframe):
    if timeframe not in ['scalp', 'day', 'swing']:
        return jsonify({'error': 'Invalid timeframe'}), 400

    threshold = int(request.args.get('threshold', 40))  # lowered
    limit = int(request.args.get('limit', 20))

    print(f"\n=== Scanning {timeframe.upper()} signals (threshold {threshold}%, limit {limit}) ===")

    signals = []
    errors = []

    for symbol in COIN_SYMBOLS[:limit]:
        print(f"Processing {symbol}...")
        coin_data = aggregator.aggregate_coin_data(symbol)
        if coin_data:
            signal = signal_generator.generate_signals(coin_data, timeframe)
            print(f"  → {signal['direction']} {signal['score']}%")
            signals.append({**coin_data, 'signal': signal})  # show ALL for now
        else:
            errors.append(symbol)
            print("  → No data")

    print(f"Complete: {len(signals)} signals, {len(errors)} errors")

    return jsonify({
        'timeframe': timeframe,
        'phase': signal_generator.btc_regime,
        'signals': sorted(signals, key=lambda x: x['signal']['score'], reverse=True),
        'errors': errors,
        'generated_at': datetime.now().isoformat()
    })

@app.route('/api/coin/<symbol>')
def get_single_coin(symbol):
    coin_data = aggregator.aggregate_coin_data(symbol.upper())
    if not coin_data:
        return jsonify({'error': 'Coin not found'}), 404

    signals = {
        tf: signal_generator.generate_signals(coin_data, tf)
        for tf in ['scalp', 'day', 'swing']
    }

    return jsonify({
        'symbol': symbol.upper(),
        'price_data': coin_data,
        'signals': signals,
        'analyzed_at': datetime.now().isoformat()
    })

if __name__ == '__main__':
    print("\n" + "="*60)
    print("Institutional Edge V5.3 - REAL DATA (CMC + Binance funding)")
    print("="*60)
    print(f"✅ Etherscan: {'Connected' if ETHERSCAN_API_KEY else 'DISABLED'}")
    print(f"✅ CMC: {'Connected' if CMC_API_KEY else 'DISABLED/Fallback'}")
    print(f"📊 Tracking {len(COIN_SYMBOLS)} coins | 🐋 Whale tokens: {len(TOKEN_ADDRESSES)}")
    print("Open http://localhost:5000")
    print("="*60)
    app.run(debug=True, host='0.0.0.0', port=5000)
