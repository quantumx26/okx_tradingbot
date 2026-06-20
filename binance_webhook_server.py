#!/usr/bin/env python3
"""
Binance Futures Webhook Server for TradingView Alerts
Supports: LONG + SHORT, Testnet & Live, Position Sizing (mit Leverage-Cap)
Entry: LIMIT Order mit Slippage-Toleranz (statt Market Order)
EXIT handled by TradingView webhooks (CLOSE_LONG/CLOSE_SHORT)
"""

import os
import sys
import time
import threading
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('binance_trading.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Konfiguration Limit-Entry ───────────────────────────────────────────────
ENTRY_SLIPPAGE_TOLERANCE = float(os.getenv('ENTRY_SLIPPAGE_TOLERANCE', '0.001'))  # 0.1% Standard
ENTRY_FILL_TIMEOUT_SECONDS = int(os.getenv('ENTRY_FILL_TIMEOUT_SECONDS', '300'))  # 5 Minuten Standard
ENTRY_FILL_POLL_INTERVAL = 0.5  # Sekunden zwischen Fill-Checks
MAX_FILL_DEVIATION = float(os.getenv('MAX_FILL_DEVIATION', '0.0015'))  # 0.15% — Sicherheitsnetz


class BinanceTrader:
    def __init__(self):
        self.api_key = os.getenv('BINANCE_API_KEY')
        self.api_secret = os.getenv('BINANCE_SECRET_KEY')
        self.testnet = os.getenv('BINANCE_TESTNET', 'true').lower() == 'true'
        self.webhook_secret = os.getenv('WEBHOOK_SECRET')

        if not all([self.api_key, self.api_secret, self.webhook_secret]):
            logger.error("❌ Missing required environment variables!")
            raise ValueError("BINANCE_API_KEY, BINANCE_SECRET_KEY, and WEBHOOK_SECRET must be set")

        try:
            if self.testnet:
                logger.info("🧪 Binance TESTNET Mode ENABLED")
                self.client = Client(
                    self.api_key,
                    self.api_secret,
                    testnet=True
                )
                self.client.API_URL = 'https://testnet.binancefuture.com'
            else:
                logger.info("💰 Binance LIVE Mode ENABLED")
                self.client = Client(self.api_key, self.api_secret)

            account = self.client.futures_account()
            balance = float(account['totalWalletBalance'])

            logger.info("✅ Connected to Binance successfully")
            logger.info(f"   Account Balance: ${balance:.2f} USDT")
            logger.info(f"   Testnet: {self.testnet}")
            logger.info(f"   Entry Slippage Tolerance: {ENTRY_SLIPPAGE_TOLERANCE*100:.2f}%")
            logger.info(f"   Entry Fill Timeout: {ENTRY_FILL_TIMEOUT_SECONDS}s")
            logger.info(f"   Max Fill Deviation (Sicherheitsnetz): {MAX_FILL_DEVIATION*100:.2f}%")

        except Exception as e:
            logger.error(f"❌ Failed to connect to Binance: {e}")
            raise

    def get_account_info(self):
        try:
            account = self.client.futures_account()
            balance = float(account['totalWalletBalance'])
            available = float(account['availableBalance'])

            return {
                'balance': balance,
                'available': available,
                'testnet': self.testnet
            }
        except Exception as e:
            logger.error(f"❌ Error getting account info: {e}")
            return None

    def get_current_price(self, symbol):
        try:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"❌ Error getting price for {symbol}: {e}")
            return None

    def get_symbol_precision(self, symbol):
        """Get quantity AND price precision/tick size for a symbol"""
        try:
            exchange_info = self.client.futures_exchange_info()
            symbol_info = next(
                (s for s in exchange_info['symbols'] if s['symbol'] == symbol),
                None
            )
            if not symbol_info:
                return None, None, None, None

            quantity_precision = symbol_info['quantityPrecision']
            price_precision = symbol_info['pricePrecision']

            tick_size = None
            for f in symbol_info['filters']:
                if f['filterType'] == 'PRICE_FILTER':
                    tick_size = float(f['tickSize'])
                    break

            return symbol_info, quantity_precision, price_precision, tick_size
        except Exception as e:
            logger.error(f"❌ Error getting symbol precision: {e}")
            return None, None, None, None

    def round_to_tick(self, price, tick_size):
        """Round price to the symbol's allowed tick size"""
        if not tick_size or tick_size == 0:
            return price
        return round(round(price / tick_size) * tick_size, 8)

    def calculate_position_size(self, symbol, entry_price, stop_loss, risk_usd):
        """
        Calculate position size based on risk AND available margin.
        """
        try:
            exchange_info = self.client.futures_exchange_info()
            symbol_info = next(
                (s for s in exchange_info['symbols'] if s['symbol'] == symbol),
                None
            )

            if not symbol_info:
                logger.error(f"❌ Symbol {symbol} not found")
                return None

            quantity_precision = symbol_info['quantityPrecision']

            risk_per_unit = abs(entry_price - stop_loss)

            if risk_per_unit <= 0:
                logger.error("❌ Invalid Stop Loss")
                return None

            position_size = risk_usd / risk_per_unit

            account = self.client.futures_account()
            available_balance = float(account['availableBalance'])

            leverage = 20

            max_position_value = available_balance * leverage * 0.95
            max_position_size = max_position_value / entry_price

            if position_size > max_position_size:
                logger.warning(
                    f"⚠️ Position capped from {position_size:.6f} to {max_position_size:.6f}"
                )
                position_size = max_position_size

            position_size = round(position_size, quantity_precision)

            min_notional = 5.0
            notional_value = position_size * entry_price

            if notional_value < min_notional:
                logger.warning(
                    f"⚠️ Position too small (${notional_value:.2f}), adjusting to minimum"
                )
                position_size = round(
                    min_notional / entry_price,
                    quantity_precision
                )

            logger.info(f"📊 Position Size: {position_size} {symbol}")
            logger.info(f"   Notional Value: ${position_size * entry_price:.2f}")
            logger.info(f"   Risk per Unit: ${risk_per_unit:.2f}")
            logger.info(f"   Total Risk: ${risk_usd:.2f}")
            logger.info(f"   Available Balance: ${available_balance:.2f}")
            logger.info(f"   Leverage: {leverage}x")

            return position_size

        except Exception as e:
            logger.error(f"❌ Error calculating position size: {e}")
            return None

    def close_position(self, symbol):
        """
        Close all positions for a symbol using MARKET order.
        (Exit bleibt Market — hier zaehlt schnelles Rauskommen mehr als Slippage-Schutz)
        """
        try:
            positions = self.client.futures_position_information(symbol=symbol)

            for pos in positions:
                pos_amt = float(pos['positionAmt'])

                if pos_amt != 0:
                    side = 'SELL' if pos_amt > 0 else 'BUY'
                    quantity = abs(pos_amt)

                    logger.info(f"📤 Closing position: {quantity} {symbol} (Side: {side})")

                    order = self.client.futures_create_order(
                        symbol=symbol,
                        side=side,
                        type='MARKET',
                        quantity=quantity
                    )

                    logger.info(f"✅ Position closed: Order ID {order['orderId']}")
                    return order

            logger.warning(f"⚠️ No open position found for {symbol}")
            return None

        except BinanceAPIException as e:
            logger.error(f"❌ Binance API Error closing position: {e.message}")
            return None
        except Exception as e:
            logger.error(f"❌ Error closing position: {e}")
            return None

    def monitor_and_cancel_if_unfilled(self, symbol, order_id, timeout_seconds, signal, entry, sl, tp, position_size):
        """
        Laeuft in einem Hintergrund-Thread: wartet bis zu `timeout_seconds`,
        prueft dann den Order-Status und storniert die Order falls sie noch
        nicht (vollstaendig) gefuellt ist. Blockiert NICHT den Webhook-Request.

        SICHERHEITS-CHECK: Falls die Order gefuellt wird, aber der echte
        Fill-Preis zu stark vom Signal-Entry abweicht (mehr als
        MAX_FILL_DEVIATION), wird die Position SOFORT wieder geschlossen.
        Grund: SL/TP werden NICHT als echte Binance-Orders gesetzt, sondern
        nur von TradingView anhand des Signal-Entry simuliert. Weicht der
        echte Fill zu stark ab, passen SL/TP nicht mehr zur echten Position
        (z.B. SL liegt dann ueber dem Entry bei einem Long) -> Position waere
        de facto ungeschuetzt bis zum naechsten TradingView-Signal.
        """
        poll_interval = 2.0
        elapsed = 0.0

        while elapsed < timeout_seconds:
            try:
                order = self.client.futures_get_order(symbol=symbol, orderId=order_id)
                status = order.get('status')

                if status == 'FILLED':
                    self._handle_fill(symbol, order, signal, entry, sl, tp)
                    return

                if status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                    logger.info(f"ℹ️ [Background] Order {order_id} bereits beendet (Status: {status}), kein Cancel noetig")
                    return

            except Exception as e:
                logger.warning(f"⚠️ [Background] Fehler beim Status-Check: {e}")

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timeout erreicht — finalen Status pruefen und ggf. stornieren
        try:
            order = self.client.futures_get_order(symbol=symbol, orderId=order_id)
            status = order.get('status')

            if status == 'FILLED':
                logger.info(f"✅ [Background] Order doch noch gefuellt kurz vor Timeout: {order_id}")
                self._handle_fill(symbol, order, signal, entry, sl, tp)
                return

            if status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                logger.info(f"ℹ️ [Background] Order {order_id} bereits beendet (Status: {status})")
                return

            logger.warning(f"⏱️ [Background] {timeout_seconds}s Timeout erreicht — storniere unfilled Order {order_id} (Status: {status})")
            self.client.futures_cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"🧹 [Background] Order erfolgreich storniert: {order_id}")

        except Exception as e:
            logger.warning(f"⚠️ [Background] Konnte Order nicht stornieren (evtl. bereits gefuellt/inaktiv): {e}")

    def _handle_fill(self, symbol, order, signal, entry, sl, tp):
        """
        Wird aufgerufen sobald eine Entry-Order gefuellt wurde.
        Prueft ob der echte Fill-Preis zu stark vom Signal-Entry abweicht.
        Falls ja: Position SOFORT wieder schliessen (Sicherheitsnetz).
        """
        order_id = order.get('orderId')
        fill_price = float(order.get('avgPrice', 0))
        if fill_price == 0:
            fill_price = float(order.get('price', entry))

        deviation = abs(fill_price - entry)
        deviation_pct = deviation / entry

        logger.info(f"✅ [Background] Entry order FILLED: {order_id}")
        logger.info(f"   Signal Entry: ${entry:.2f}")
        logger.info(f"   Fill Price: ${fill_price:.2f}")
        logger.info(f"   Abweichung: ${deviation:.2f} ({deviation_pct*100:.3f}%)")

        if deviation_pct > MAX_FILL_DEVIATION:
            logger.error(f"🚨 [Background] Fill weicht zu stark ab! ({deviation_pct*100:.3f}% > Limit {MAX_FILL_DEVIATION*100:.3f}%)")
            logger.error(f"🚨 [Background] SL/TP wuerden nicht mehr zur echten Position passen — schliesse SOFORT wieder!")
            close_result = self.close_position(symbol)
            if close_result:
                logger.warning(f"🛡️ [Background] Position aus Sicherheitsgruenden geschlossen: {close_result.get('orderId')}")
            else:
                logger.error(f"❌ [Background] KONNTE Position NICHT automatisch schliessen — manuell pruefen!")
            return

        logger.info("ℹ️  Stop Loss and Take Profit will be managed by TradingView EXIT webhooks")
        logger.info(f"   Expected TP: ${tp:.2f}, Expected SL: ${sl:.2f}")

    def place_order(self, signal, symbol, entry, sl, tp, risk_usd):
        """
        Place a LIMIT entry order with a small slippage tolerance buffer.
        Der Webhook-Request antwortet SOFORT nach dem Platzieren der Order,
        ein Hintergrund-Thread ueberwacht den Fill-Status und storniert die
        Order automatisch nach ENTRY_FILL_TIMEOUT_SECONDS (Standard 5 Min)
        falls sie bis dahin nicht gefuellt wurde. Falls die Order zu einem
        Preis fuellt der zu stark vom Signal-Entry abweicht, wird die
        Position automatisch wieder geschlossen (siehe _handle_fill).

        Long:  Limit-Preis = entry * (1 + TOLERANCE)  -> bereit etwas mehr zu zahlen
        Short: Limit-Preis = entry * (1 - TOLERANCE)  -> bereit etwas weniger zu bekommen

        Das deckelt die maximale Slippage exakt auf die Toleranz, statt wie bei
        Market Orders unbegrenzt offen zu sein.
        """
        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None

            position_size = self.calculate_position_size(symbol, entry, sl, risk_usd)
            if not position_size:
                return None

            symbol_info, quantity_precision, price_precision, tick_size = self.get_symbol_precision(symbol)
            if symbol_info is None:
                logger.error(f"❌ Could not load symbol precision for {symbol}")
                return None

            side = 'BUY' if signal == 'LONG' else 'SELL'

            # Limit-Preis mit Toleranz-Puffer berechnen
            if signal == 'LONG':
                limit_price = entry * (1 + ENTRY_SLIPPAGE_TOLERANCE)
            else:
                limit_price = entry * (1 - ENTRY_SLIPPAGE_TOLERANCE)

            limit_price = self.round_to_tick(limit_price, tick_size)

            logger.info(f"📊 Placing {signal} LIMIT order for {symbol}")
            logger.info(f"   Side: {side}")
            logger.info(f"   Quantity: {position_size}")
            logger.info(f"   Signal Entry: ${entry:.2f}")
            logger.info(f"   Current Price: ${current_price:.2f}")
            logger.info(f"   Limit Price (mit {ENTRY_SLIPPAGE_TOLERANCE*100:.2f}% Toleranz): ${limit_price:.2f}")
            logger.info(f"   Stop Loss: ${sl:.2f}")
            logger.info(f"   Take Profit: ${tp:.2f}")
            logger.info(f"   Unfilled-Timeout: {ENTRY_FILL_TIMEOUT_SECONDS}s ({ENTRY_FILL_TIMEOUT_SECONDS/60:.1f} Min)")

            # Bestehende Positionen zuerst schliessen
            try:
                positions = self.client.futures_position_information(symbol=symbol)
                for pos in positions:
                    pos_amt = float(pos['positionAmt'])
                    if pos_amt != 0:
                        close_side = 'SELL' if pos_amt > 0 else 'BUY'
                        logger.info(f"📤 Closing existing position: {abs(pos_amt)} {symbol}")
                        self.client.futures_create_order(
                            symbol=symbol,
                            side=close_side,
                            type='MARKET',
                            quantity=abs(pos_amt)
                        )
            except Exception as e:
                logger.warning(f"⚠️ Error checking/closing positions: {e}")

            # LIMIT Entry Order platzieren
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='LIMIT',
                price=limit_price,
                quantity=position_size,
                timeInForce='GTC'
            )

            logger.info(f"📝 Limit order placed: {order['orderId']}")
            logger.info(f"   Ueberwachung laeuft im Hintergrund (Auto-Cancel nach {ENTRY_FILL_TIMEOUT_SECONDS}s falls unfilled)")

            # Hintergrund-Thread starten, der die Order ueberwacht und ggf. storniert
            monitor_thread = threading.Thread(
                target=self.monitor_and_cancel_if_unfilled,
                args=(symbol, order['orderId'], ENTRY_FILL_TIMEOUT_SECONDS, signal, entry, sl, tp, position_size),
                daemon=True
            )
            monitor_thread.start()

            # Sofort zurueckgeben — der Fill wird im Hintergrund-Thread bestaetigt/storniert
            return {
                'entry_order': order,
                'sl_order': None,
                'tp_order': None,
                'position_size': position_size,
                'entry_price': limit_price,
                'note': f'LIMIT order placed, monitored in background for {ENTRY_FILL_TIMEOUT_SECONDS}s'
            }

        except BinanceAPIException as e:
            logger.error(f"❌ Binance API Error: {e.message}")
            return None
        except Exception as e:
            logger.error(f"❌ Order failed: {e}")
            return None


# Initialize trader
try:
    trader = BinanceTrader()
except Exception as e:
    logger.error(f"❌ Failed to initialize trader: {e}")
    trader = None


@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle TradingView webhook alerts"""
    try:
        if trader is None:
            logger.error("❌ Trader not initialized — check Binance API credentials / connection")
            return jsonify({'error': 'Trader not initialized — check server logs'}), 500

        data = request.get_json()

        if not data:
            logger.error("❌ No JSON data received")
            return jsonify({'error': 'No data'}), 400

        if data.get('secret') != trader.webhook_secret:
            logger.error("❌ Invalid webhook secret")
            return jsonify({'error': 'Unauthorized'}), 401

        signal = data.get('signal')
        symbol = data.get('symbol')

        if signal == 'CLOSE_LONG':
            logger.info(f"📤 EXIT Signal: Close LONG position for {symbol}")
            result = trader.close_position(symbol)

            if result:
                return jsonify({
                    'status': 'success',
                    'action': 'close_long',
                    'symbol': symbol,
                    'order_id': result.get('orderId')
                }), 200
            else:
                return jsonify({'error': 'Failed to close position'}), 500

        elif signal == 'CLOSE_SHORT':
            logger.info(f"📤 EXIT Signal: Close SHORT position for {symbol}")
            result = trader.close_position(symbol)

            if result:
                return jsonify({
                    'status': 'success',
                    'action': 'close_short',
                    'symbol': symbol,
                    'order_id': result.get('orderId')
                }), 200
            else:
                return jsonify({'error': 'Failed to close position'}), 500

        entry = float(data.get('entry', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        risk_usd = float(data.get('risk_usd', 100))

        logger.info(f"📊 Webhook: {signal} {symbol}")
        logger.info(f"   Entry: {entry}, SL: {sl}, TP: {tp}")

        if not all([signal, symbol, entry, sl, tp]):
            logger.error("❌ Missing required fields")
            return jsonify({'error': 'Missing fields'}), 400

        if signal not in ['LONG', 'SHORT']:
            logger.error(f"❌ Invalid signal: {signal}")
            return jsonify({'error': 'Invalid signal'}), 400

        result = trader.place_order(signal, symbol, entry, sl, tp, risk_usd)

        if result:
            return jsonify({
                'status': 'success',
                'signal': signal,
                'symbol': symbol,
                'position_size': result['position_size'],
                'entry_price': result['entry_price']
            }), 200
        else:
            return jsonify({'error': 'Order failed or not filled within tolerance/timeout'}), 500

    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    """Get bot and account status"""
    try:
        if not trader:
            return jsonify({'error': 'Trader not initialized'}), 500

        account = trader.get_account_info()
        btc_price = trader.get_current_price('BTCUSDT')

        return jsonify({
            'status': 'running',
            'testnet': trader.testnet,
            'account': account,
            'btc_price': btc_price,
            'entry_slippage_tolerance': ENTRY_SLIPPAGE_TOLERANCE,
            'entry_fill_timeout_seconds': ENTRY_FILL_TIMEOUT_SECONDS,
            'timestamp': datetime.utcnow().isoformat()
        }), 200

    except Exception as e:
        logger.error(f"❌ Status error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/positions', methods=['GET'])
def positions():
    """Get current open positions"""
    try:
        if not trader:
            return jsonify({'error': 'Trader not initialized'}), 500

        positions = trader.client.futures_position_information()

        active_positions = [
            {
                'symbol': p['symbol'],
                'size': float(p['positionAmt']),
                'entry_price': float(p['entryPrice']),
                'unrealized_pnl': float(p['unRealizedProfit']),
                'leverage': int(p['leverage'])
            }
            for p in positions
            if float(p['positionAmt']) != 0
        ]

        return jsonify({
            'positions': active_positions,
            'count': len(active_positions)
        }), 200

    except Exception as e:
        logger.error(f"❌ Positions error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/test', methods=['GET'])
def test():
    """Test endpoint"""
    return jsonify({
        'status': 'ok',
        'message': 'Binance Webhook Server is running',
        'testnet': trader.testnet if trader else None
    }), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
