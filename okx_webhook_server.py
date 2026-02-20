"""
TradingView Webhook Server → OKX Demo Trading
Automated Crypto Futures Trading
"""

from flask import Flask, request, jsonify
import ccxt
import logging
from datetime import datetime
import json
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('okx_trading.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

class OKXTrader:
    """OKX Trading execution"""

    def __init__(self):
        self.api_key = os.getenv('OKX_API_KEY')
        self.secret = os.getenv('OKX_SECRET')
        self.passphrase = os.getenv('OKX_PASSPHRASE')
        self.demo = os.getenv('OKX_DEMO', 'true').lower() == 'true'

        # Build exchange config
        exchange_config = {
            'apiKey': self.api_key,
            'secret': self.secret,
            'password': self.passphrase,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',  # Perpetual Futures
            }
        }

        # Demo Mode: OKX uses same URLs as Live, only header differs
        # DO NOT use set_sandbox_mode(True) — it points to wrong URLs
        if self.demo:
            exchange_config['headers'] = {
                'x-simulated-trading': '1'
            }
            logging.info("✅ OKX Demo Trading Mode ENABLED")
        else:
            logging.info("⚠️  OKX LIVE Trading Mode")

        self.exchange = ccxt.okx(exchange_config)

        # Test connection
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get('USDT', {}).get('free', 0)
            logging.info(f"✅ Connected to OKX successfully")
            logging.info(f"   Demo Mode: {self.demo}")
            logging.info(f"   USDT Balance: {usdt:.2f}")
        except Exception as e:
            logging.error(f"❌ Connection failed: {str(e)}")

    def get_balance(self):
        """Get USDT balance"""
        try:
            balance = self.exchange.fetch_balance()
            return balance.get('USDT', {}).get('free', 0)
        except Exception as e:
            logging.error(f"Error getting balance: {str(e)}")
            return None

    def calculate_position_size(self, symbol, risk_usd, entry_price, stop_loss):
        """Calculate position size based on risk amount"""
        try:
            markets = self.exchange.load_markets()
            market = markets.get(symbol)

            if not market:
                logging.warning(f"Market {symbol} not found, using default 0.01")
                return 0.01

            contract_size = market.get('contractSize', 1)
            risk_price = abs(entry_price - stop_loss)

            if risk_price == 0:
                logging.warning("SL == Entry price, using default 0.01")
                return 0.01

            contracts = risk_usd / (contract_size * risk_price)
            contracts = self.exchange.amount_to_precision(symbol, contracts)
            contracts = max(float(contracts), 0.01)

            logging.info(f"Position Size: {contracts} contracts (Risk: ${risk_usd:.2f})")
            return contracts

        except Exception as e:
            logging.error(f"Error calculating position: {str(e)}")
            return 0.01

    def place_market_order(self, symbol, side, amount, stop_loss=None, take_profit=None):
        """Place market order with optional SL and TP"""
        try:
            # Main market order
            order = self.exchange.create_market_order(
                symbol=symbol,
                side=side,
                amount=amount
            )

            logging.info(f"✅ Order placed: {side.upper()} {amount} {symbol}")
            logging.info(f"   Order ID: {order['id']}")

            # Stop Loss
            if stop_loss:
                sl_side = 'sell' if side == 'buy' else 'buy'
                try:
                    sl_order = self.exchange.create_order(
                        symbol=symbol,
                        type='stop_market',
                        side=sl_side,
                        amount=amount,
                        params={
                            'stopPrice': stop_loss,
                            'reduceOnly': True
                        }
                    )
                    logging.info(f"   ✅ SL placed at: {stop_loss}")
                except Exception as e:
                    logging.warning(f"   ⚠️  SL failed: {str(e)}")

            # Take Profit
            if take_profit:
                tp_side = 'sell' if side == 'buy' else 'buy'
                try:
                    tp_order = self.exchange.create_limit_order(
                        symbol=symbol,
                        side=tp_side,
                        amount=amount,
                        price=take_profit,
                        params={
                            'reduceOnly': True
                        }
                    )
                    logging.info(f"   ✅ TP placed at: {take_profit}")
                except Exception as e:
                    logging.warning(f"   ⚠️  TP failed: {str(e)}")

            return order

        except Exception as e:
            logging.error(f"❌ Order failed: {str(e)}")
            return None

    def close_position(self, symbol):
        """Close all open positions for a symbol"""
        try:
            positions = self.exchange.fetch_positions([symbol])
            closed = 0

            for position in positions:
                contracts = float(position.get('contracts', 0))
                if contracts > 0:
                    side = 'sell' if position['side'] == 'long' else 'buy'
                    self.exchange.create_market_order(
                        symbol=symbol,
                        side=side,
                        amount=contracts,
                        params={'reduceOnly': True}
                    )
                    logging.info(f"✅ Closed {position['side'].upper()}: {contracts} {symbol}")
                    closed += 1

            if closed == 0:
                logging.info(f"ℹ️  No open positions found for {symbol}")

            return True

        except Exception as e:
            logging.error(f"❌ Close failed: {str(e)}")
            return False

    def get_current_price(self, symbol):
        """Get last price for symbol"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            logging.error(f"Error getting price: {str(e)}")
            return None


# Initialize trader
trader = OKXTrader()

# Webhook secret
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'your-secret-key')


@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive and process TradingView alerts"""
    try:
        data = request.get_json()

        if not data:
            return jsonify({'error': 'No data received'}), 400

        # Security check
        if data.get('secret') != WEBHOOK_SECRET:
            logging.warning(f"⚠️  Invalid secret from {request.remote_addr}")
            return jsonify({'error': 'Invalid secret'}), 403

        # Extract signal data
        signal    = data.get('signal', '').upper()
        symbol    = data.get('symbol', 'BTC/USDT:USDT')
        entry     = float(data.get('entry', 0))
        sl        = float(data.get('sl', 0))
        tp        = float(data.get('tp', 0))
        risk_usd  = float(data.get('risk_usd', 100))

        # Validate signal
        if signal not in ['LONG', 'SHORT', 'CLOSE']:
            return jsonify({'error': f'Invalid signal: {signal}'}), 400

        logging.info(f"📊 Webhook received: {signal} {symbol}")
        logging.info(f"   Entry: {entry} | SL: {sl} | TP: {tp} | Risk: ${risk_usd}")

        # CLOSE signal
        if signal == 'CLOSE':
            result = trader.close_position(symbol)
            if result:
                return jsonify({'status': 'success', 'action': 'closed', 'symbol': symbol}), 200
            else:
                return jsonify({'error': 'Failed to close position'}), 500

        # Calculate position size
        amount = trader.calculate_position_size(symbol, risk_usd, entry, sl)

        # Determine order side
        side = 'buy' if signal == 'LONG' else 'sell'

        # Place order
        result = trader.place_market_order(
            symbol=symbol,
            side=side,
            amount=amount,
            stop_loss=sl if sl else None,
            take_profit=tp if tp else None
        )

        if result:
            return jsonify({
                'status': 'success',
                'signal': signal,
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'order_id': result.get('id')
            }), 200
        else:
            return jsonify({'error': 'Order placement failed'}), 500

    except Exception as e:
        logging.error(f"❌ Webhook error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    """Health check endpoint"""
    balance = trader.get_balance()
    btc_price = trader.get_current_price('BTC/USDT:USDT')

    return jsonify({
        'status': 'running',
        'timestamp': datetime.now().isoformat(),
        'demo_mode': trader.demo,
        'balance_usdt': balance,
        'btc_price': btc_price
    }), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({'message': 'OKX Trading Bot is running 🚀', 'webhook': '/webhook', 'status': '/status'}), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))

    logging.info("=" * 50)
    logging.info("🚀 OKX Trading Bot Starting")
    logging.info(f"   Port:      {port}")
    logging.info(f"   Demo Mode: {trader.demo}")
    logging.info(f"   Webhook:   http://localhost:{port}/webhook")
    logging.info(f"   Status:    http://localhost:{port}/status")
    logging.info("=" * 50)

    app.run(host='0.0.0.0', port=port, debug=False)
