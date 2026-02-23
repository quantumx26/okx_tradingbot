#!/usr/bin/env python3
"""
Binance Futures Webhook Server for TradingView Alerts
Supports: LONG + SHORT, Testnet & Live, Position Sizing, SL/TP Management
"""

import os
import sys
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
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

class BinanceTrader:
    def __init__(self):
        """Initialize Binance client with API credentials"""
        self.api_key = os.getenv('BINANCE_API_KEY')
        self.api_secret = os.getenv('BINANCE_SECRET_KEY')
        self.testnet = os.getenv('BINANCE_TESTNET', 'true').lower() == 'true'
        self.webhook_secret = os.getenv('WEBHOOK_SECRET')
        
        if not all([self.api_key, self.api_secret, self.webhook_secret]):
            logger.error("❌ Missing required environment variables!")
            raise ValueError("BINANCE_API_KEY, BINANCE_SECRET_KEY, and WEBHOOK_SECRET must be set")
        
        # Initialize Binance client
        try:
            if self.testnet:
                logger.info("🧪 Binance TESTNET Mode ENABLED")
                self.client = Client(
                    self.api_key, 
                    self.api_secret,
                    testnet=True
                )
                # Set testnet URL
                self.client.API_URL = 'https://testnet.binancefuture.com'
            else:
                logger.info("💰 Binance LIVE Mode ENABLED")
                self.client = Client(self.api_key, self.api_secret)
            
            # Test connection
            account = self.client.futures_account()
            balance = float(account['totalWalletBalance'])
            
            logger.info("✅ Connected to Binance successfully")
            logger.info(f"   Account Balance: ${balance:.2f} USDT")
            logger.info(f"   Testnet: {self.testnet}")
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Binance: {e}")
            raise
    
    def get_account_info(self):
        """Get account balance and info"""
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
        """Get current market price for symbol"""
        try:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"❌ Error getting price for {symbol}: {e}")
            return None
    
    def calculate_position_size(self, symbol, entry_price, stop_loss, risk_usd):
        """
        Calculate position size based on risk
        
        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            entry_price: Entry price
            stop_loss: Stop loss price
            risk_usd: Amount to risk in USD
        
        Returns:
            Position size in base currency
        """
        try:
            # Get symbol info for precision
            exchange_info = self.client.futures_exchange_info()
            symbol_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            
            if not symbol_info:
                logger.error(f"❌ Symbol {symbol} not found")
                return None
            
            # Get precision
            quantity_precision = symbol_info['quantityPrecision']
            
            # Calculate risk per unit
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit == 0:
                logger.error("❌ Risk per unit is 0, invalid SL")
                return None
            
            # Calculate position size
            position_size = risk_usd / risk_per_unit
            
            # Round to symbol precision
            position_size = round(position_size, quantity_precision)
            
            # Check minimum notional
            min_notional = 5.0  # Binance minimum ~$5
            notional_value = position_size * entry_price
            
            if notional_value < min_notional:
                logger.warning(f"⚠️ Position too small (${notional_value:.2f}), adjusting to minimum")
                position_size = round(min_notional / entry_price, quantity_precision)
            
            logger.info(f"📊 Position Size: {position_size} {symbol}")
            logger.info(f"   Notional Value: ${position_size * entry_price:.2f}")
            logger.info(f"   Risk per Unit: ${risk_per_unit:.2f}")
            logger.info(f"   Total Risk: ${risk_usd:.2f}")
            
            return position_size
            
        except Exception as e:
            logger.error(f"❌ Error calculating position size: {e}")
            return None
    
    def place_order(self, signal, symbol, entry, sl, tp, risk_usd):
        """
        Place market order with stop loss and take profit
        
        Args:
            signal: 'LONG' or 'SHORT'
            symbol: Trading pair (e.g., 'BTCUSDT')
            entry: Entry price (for reference)
            sl: Stop loss price
            tp: Take profit price
            risk_usd: Risk amount in USD
        
        Returns:
            Order response or None
        """
        try:
            # Get current price for entry
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None
            
            # Calculate position size
            position_size = self.calculate_position_size(symbol, current_price, sl, risk_usd)
            if not position_size:
                return None
            
            # Determine side
            side = 'BUY' if signal == 'LONG' else 'SELL'
            
            logger.info(f"📊 Placing {signal} order for {symbol}")
            logger.info(f"   Side: {side}")
            logger.info(f"   Quantity: {position_size}")
            logger.info(f"   Entry: ${current_price:.2f}")
            logger.info(f"   Stop Loss: ${sl:.2f}")
            logger.info(f"   Take Profit: ${tp:.2f}")
            
            # Close any existing positions for this symbol first
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
            
            # Place market entry order
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type='MARKET',
                quantity=position_size
            )
            
            logger.info(f"✅ Entry order placed: {order['orderId']}")
            
            # Place stop loss order
            sl_side = 'SELL' if signal == 'LONG' else 'BUY'
            sl_order = self.client.futures_create_order(
                symbol=symbol,
                side=sl_side,
                type='STOP_MARKET',
                stopPrice=sl,
                closePosition=True
            )
            
            logger.info(f"✅ Stop Loss set at ${sl:.2f}")
            
            # Place take profit order (using LIMIT instead of TAKE_PROFIT_MARKET)
            tp_order = self.client.futures_create_order(
                symbol=symbol,
                side=sl_side,
                type='LIMIT',
                price=tp,
                quantity=position_size,
                timeInForce='GTC',
                reduceOnly=True
            )
            
            logger.info(f"✅ Take Profit set at ${tp:.2f}")
            
            return {
                'entry_order': order,
                'sl_order': sl_order,
                'tp_order': tp_order,
                'position_size': position_size,
                'entry_price': current_price
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
        # Get JSON data
        data = request.get_json()
        
        if not data:
            logger.error("❌ No JSON data received")
            return jsonify({'error': 'No data'}), 400
        
        # Verify webhook secret
        if data.get('secret') != trader.webhook_secret:
            logger.error("❌ Invalid webhook secret")
            return jsonify({'error': 'Unauthorized'}), 401
        
        # Extract signal data
        signal = data.get('signal')
        symbol = data.get('symbol')
        entry = float(data.get('entry', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        risk_usd = float(data.get('risk_usd', 100))
        
        logger.info(f"📊 Webhook: {signal} {symbol}")
        logger.info(f"   Entry: {entry}, SL: {sl}, TP: {tp}")
        
        # Validate data
        if not all([signal, symbol, entry, sl, tp]):
            logger.error("❌ Missing required fields")
            return jsonify({'error': 'Missing fields'}), 400
        
        if signal not in ['LONG', 'SHORT']:
            logger.error(f"❌ Invalid signal: {signal}")
            return jsonify({'error': 'Invalid signal'}), 400
        
        # Place order
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
            return jsonify({'error': 'Order failed'}), 500
            
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
        
        # Filter only positions with non-zero amount
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
