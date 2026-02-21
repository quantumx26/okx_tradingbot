#!/usr/bin/env python3
"""
Bybit Futures Webhook Server for TradingView Alerts
Supports: LONG + SHORT, Testnet & Live, Position Sizing, SL/TP Management
"""

import os
import sys
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bybit_trading.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

class BybitTrader:
    def __init__(self):
        """Initialize Bybit client with API credentials"""
        self.api_key = os.getenv('BYBIT_API_KEY')
        self.api_secret = os.getenv('BYBIT_SECRET_KEY')
        self.testnet = os.getenv('BYBIT_TESTNET', 'true').lower() == 'true'
        self.webhook_secret = os.getenv('WEBHOOK_SECRET')
        
        if not all([self.api_key, self.api_secret, self.webhook_secret]):
            logger.error("❌ Missing required environment variables!")
            raise ValueError("BYBIT_API_KEY, BYBIT_SECRET_KEY, and WEBHOOK_SECRET must be set")
        
        # Initialize Bybit client
        try:
            if self.testnet:
                logger.info("🧪 Bybit TESTNET Mode ENABLED")
                self.client = HTTP(
                    testnet=True,
                    api_key=self.api_key,
                    api_secret=self.api_secret
                )
            else:
                logger.info("💰 Bybit LIVE Mode ENABLED")
                self.client = HTTP(
                    testnet=False,
                    api_key=self.api_key,
                    api_secret=self.api_secret
                )
            
            # Test connection
            balance = self.get_account_info()
            
            logger.info("✅ Connected to Bybit successfully")
            logger.info(f"   Account Balance: ${balance['balance']:.2f} USDT")
            logger.info(f"   Available: ${balance['available']:.2f} USDT")
            logger.info(f"   Testnet: {self.testnet}")
            
        except Exception as e:
            logger.error(f"❌ Failed to connect to Bybit: {e}")
            raise
    
    def get_account_info(self):
        """Get account balance and info"""
        try:
            response = self.client.get_wallet_balance(
                accountType="UNIFIED"
            )
            
            if response['retCode'] != 0:
                logger.error(f"❌ Error getting balance: {response['retMsg']}")
                return None
            
            # Get USDT balance
            coins = response['result']['list'][0]['coin']
            usdt_balance = next((c for c in coins if c['coin'] == 'USDT'), None)
            
            if usdt_balance:
                balance = float(usdt_balance['walletBalance'])
                available = float(usdt_balance['availableToWithdraw'])
            else:
                balance = 0.0
                available = 0.0
            
            return {
                'balance': balance,
                'available': available,
                'testnet': self.testnet
            }
        except Exception as e:
            logger.error(f"❌ Error getting account info: {e}")
            return {'balance': 0, 'available': 0, 'testnet': self.testnet}
    
    def get_current_price(self, symbol):
        """Get current market price for symbol"""
        try:
            response = self.client.get_tickers(
                category="linear",
                symbol=symbol
            )
            
            if response['retCode'] == 0 and response['result']['list']:
                return float(response['result']['list'][0]['lastPrice'])
            
            logger.error(f"❌ Error getting price for {symbol}")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error getting price: {e}")
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
            response = self.client.get_instruments_info(
                category="linear",
                symbol=symbol
            )
            
            if response['retCode'] != 0:
                logger.error(f"❌ Symbol {symbol} not found")
                return None
            
            instrument = response['result']['list'][0]
            
            # Get qty step (precision)
            qty_step = float(instrument['lotSizeFilter']['qtyStep'])
            
            # Determine precision from qty_step
            if '.' in str(qty_step):
                precision = len(str(qty_step).split('.')[1].rstrip('0'))
            else:
                precision = 0
            
            # Calculate risk per unit
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit == 0:
                logger.error("❌ Risk per unit is 0, invalid SL")
                return None
            
            # Calculate position size
            position_size = risk_usd / risk_per_unit
            
            # Round to symbol precision
            position_size = round(position_size, precision)
            
            # Check minimum order qty
            min_qty = float(instrument['lotSizeFilter']['minOrderQty'])
            if position_size < min_qty:
                logger.warning(f"⚠️ Position too small, adjusting to minimum: {min_qty}")
                position_size = min_qty
            
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
            side = 'Buy' if signal == 'LONG' else 'Sell'
            
            logger.info(f"📊 Placing {signal} order for {symbol}")
            logger.info(f"   Side: {side}")
            logger.info(f"   Quantity: {position_size}")
            logger.info(f"   Entry: ${current_price:.2f}")
            logger.info(f"   Stop Loss: ${sl:.2f}")
            logger.info(f"   Take Profit: ${tp:.2f}")
            
            # Close any existing positions first
            try:
                positions = self.client.get_positions(
                    category="linear",
                    symbol=symbol
                )
                
                if positions['retCode'] == 0:
                    for pos in positions['result']['list']:
                        pos_size = float(pos['size'])
                        if pos_size > 0:
                            close_side = 'Sell' if pos['side'] == 'Buy' else 'Buy'
                            logger.info(f"📤 Closing existing position: {pos_size} {symbol}")
                            
                            self.client.place_order(
                                category="linear",
                                symbol=symbol,
                                side=close_side,
                                orderType="Market",
                                qty=str(pos_size),
                                reduceOnly=True
                            )
            except Exception as e:
                logger.warning(f"⚠️ Error checking/closing positions: {e}")
            
            # Place market entry order with SL and TP
            order = self.client.place_order(
                category="linear",
                symbol=symbol,
                side=side,
                orderType="Market",
                qty=str(position_size),
                stopLoss=str(sl),
                takeProfit=str(tp)
            )
            
            if order['retCode'] == 0:
                logger.info(f"✅ Entry order placed: {order['result']['orderId']}")
                logger.info(f"✅ Stop Loss set at ${sl:.2f}")
                logger.info(f"✅ Take Profit set at ${tp:.2f}")
                
                return {
                    'entry_order': order['result'],
                    'position_size': position_size,
                    'entry_price': current_price
                }
            else:
                logger.error(f"❌ Order failed: {order['retMsg']}")
                return None
            
        except Exception as e:
            logger.error(f"❌ Order failed: {e}")
            return None

# Initialize trader
try:
    trader = BybitTrader()
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
        
        response = trader.client.get_positions(
            category="linear"
        )
        
        if response['retCode'] != 0:
            return jsonify({'error': response['retMsg']}), 500
        
        # Filter only positions with non-zero size
        active_positions = [
            {
                'symbol': p['symbol'],
                'size': float(p['size']),
                'side': p['side'],
                'entry_price': float(p['avgPrice']),
                'unrealized_pnl': float(p['unrealisedPnl']),
                'leverage': p['leverage']
            }
            for p in response['result']['list']
            if float(p['size']) > 0
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
        'message': 'Bybit Webhook Server is running',
        'testnet': trader.testnet if trader else None
    }), 200

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
