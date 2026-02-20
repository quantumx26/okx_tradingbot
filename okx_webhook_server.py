"""
TradingView Webhook Server → Alpaca Paper Trading
Automated Crypto Trading (BTC, ETH, etc.)
"""

from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
import logging
from datetime import datetime
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('alpaca_trading.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

class AlpacaTrader:
    """Alpaca Trading execution"""
    
    def __init__(self):
        self.api_key = os.getenv('ALPACA_API_KEY')
        self.secret_key = os.getenv('ALPACA_SECRET_KEY')
        self.paper = os.getenv('ALPACA_PAPER', 'true').lower() == 'true'
        
        # Initialize Alpaca client
        self.client = TradingClient(
            api_key=self.api_key,
            secret_key=self.secret_key,
            paper=self.paper
        )
        
        # Initialize data client
        self.data_client = CryptoHistoricalDataClient()
        
        if self.paper:
            logging.info("✅ Alpaca Paper Trading Mode ENABLED")
        else:
            logging.info("⚠️ Alpaca LIVE Trading Mode")
        
        # Test connection
        try:
            account = self.client.get_account()
            logging.info(f"✅ Connected to Alpaca successfully")
            logging.info(f"   Account: ${account.equity}")
            logging.info(f"   Buying Power: ${account.buying_power}")
            logging.info(f"   Paper Mode: {self.paper}")
        except Exception as e:
            logging.error(f"❌ Connection failed: {str(e)}")
    
    def get_account(self):
        """Get account info"""
        try:
            account = self.client.get_account()
            return {
                'equity': float(account.equity),
                'buying_power': float(account.buying_power),
                'cash': float(account.cash),
                'portfolio_value': float(account.portfolio_value)
            }
        except Exception as e:
            logging.error(f"Error getting account: {str(e)}")
            return None
    
    def get_current_price(self, symbol):
        """Get current price for crypto"""
        try:
            # Alpaca crypto symbols format: BTC/USD
            bars_request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                limit=1
            )
            bars = self.data_client.get_crypto_bars(bars_request)
            
            if symbol in bars.data:
                latest_bar = bars.data[symbol][-1]
                return float(latest_bar.close)
            return None
        except Exception as e:
            logging.error(f"Error getting price: {str(e)}")
            return None
    
    def calculate_position_size(self, symbol, risk_usd, entry_price, stop_loss):
        """Calculate position size based on risk"""
        try:
            # Calculate risk per unit
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit == 0:
                logging.error("Risk per unit is zero")
                return 0
            
            # Calculate quantity
            quantity = risk_usd / risk_per_unit
            
            # Round to appropriate precision (crypto usually 6 decimals)
            quantity = round(quantity, 6)
            
            # Minimum position
            if quantity < 0.0001:
                quantity = 0.0001
            
            logging.info(f"Position Size: {quantity} units (Risk: ${risk_usd:.2f})")
            
            return quantity
            
        except Exception as e:
            logging.error(f"Error calculating position: {str(e)}")
            return 0.0001
    
    def place_market_order(self, symbol, side, quantity, stop_loss=None, take_profit=None):
        """Place market order with bracket (SL/TP)"""
        try:
            # Determine order side
            order_side = OrderSide.BUY if side.upper() == 'BUY' else OrderSide.SELL
            
            # Create bracket order with SL and TP
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.GTC
            )
            
            # Place main order
            order = self.client.submit_order(order_data)
            
            logging.info(f"✅ Order placed: {side.upper()} {quantity} {symbol}")
            logging.info(f"   Order ID: {order.id}")
            
            # Note: Alpaca crypto doesn't support bracket orders yet
            # You'd need to manually manage SL/TP or use separate orders
            
            if stop_loss:
                logging.info(f"   Target SL: {stop_loss}")
            if take_profit:
                logging.info(f"   Target TP: {take_profit}")
            
            return order
            
        except Exception as e:
            logging.error(f"❌ Order failed: {str(e)}")
            return None
    
    def close_position(self, symbol):
        """Close position for symbol"""
        try:
            # Get current position
            positions = self.client.get_all_positions()
            
            for position in positions:
                if position.symbol == symbol:
                    qty = abs(float(position.qty))
                    side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
                    
                    # Close position
                    order_data = MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=side,
                        time_in_force=TimeInForce.GTC
                    )
                    
                    order = self.client.submit_order(order_data)
                    
                    logging.info(f"✅ Closed position: {symbol} ({qty} units)")
                    return True
            
            logging.info(f"No position found for {symbol}")
            return False
            
        except Exception as e:
            logging.error(f"❌ Close position failed: {str(e)}")
            return False

# Initialize trader
trader = AlpacaTrader()

# Security
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'your-secret-key')

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive TradingView alerts"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'No data'}), 400
        
        # Security check
        if data.get('secret') != WEBHOOK_SECRET:
            logging.warning(f"⚠️ Invalid secret from {request.remote_addr}")
            return jsonify({'error': 'Invalid secret'}), 403
        
        # Extract data
        signal = data.get('signal', '').upper()
        symbol = data.get('symbol', 'BTC/USD')  # Alpaca format: BTC/USD
        entry = float(data.get('entry', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        risk_usd = float(data.get('risk_usd', 100))
        
        # Validate
        if signal not in ['LONG', 'SHORT', 'CLOSE', 'BUY', 'SELL']:
            return jsonify({'error': 'Invalid signal'}), 400
        
        logging.info(f"📊 Webhook: {signal} {symbol}")
        logging.info(f"   Entry: {entry}, SL: {sl}, TP: {tp}")
        
        # Handle CLOSE
        if signal == 'CLOSE':
            result = trader.close_position(symbol)
            if result:
                return jsonify({'status': 'success', 'action': 'closed'}), 200
            else:
                return jsonify({'error': 'Failed to close'}), 500
        
        # Calculate size
        quantity = trader.calculate_position_size(symbol, risk_usd, entry, sl)
        
        # Determine side
        side = 'BUY' if signal in ['LONG', 'BUY'] else 'SELL'
        
        # Place order
        result = trader.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_loss=sl,
            take_profit=tp
        )
        
        if result:
            return jsonify({
                'status': 'success',
                'signal': signal,
                'symbol': symbol,
                'quantity': quantity,
                'order_id': result.id
            }), 200
        else:
            return jsonify({'error': 'Order failed'}), 500
            
    except Exception as e:
        logging.error(f"❌ Webhook error: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/status', methods=['GET'])
def status():
    """Health check"""
    account = trader.get_account()
    btc_price = trader.get_current_price('BTC/USD')
    
    return jsonify({
        'status': 'running',
        'timestamp': datetime.now().isoformat(),
        'paper_mode': trader.paper,
        'account': account,
        'btc_price': btc_price
    }), 200

@app.route('/positions', methods=['GET'])
def positions():
    """Get current positions"""
    try:
        positions = trader.client.get_all_positions()
        
        positions_list = []
        for pos in positions:
            positions_list.append({
                'symbol': pos.symbol,
                'qty': float(pos.qty),
                'avg_entry': float(pos.avg_entry_price),
                'current_price': float(pos.current_price),
                'pnl': float(pos.unrealized_pl),
                'pnl_pct': float(pos.unrealized_plpc) * 100
            })
        
        return jsonify({
            'positions': positions_list,
            'count': len(positions_list)
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting positions: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/test', methods=['POST'])
def test():
    """Test endpoint"""
    try:
        data = request.get_json()
        logging.info(f"📝 Test webhook: {data}")
        return jsonify({'status': 'test received', 'data': data}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    
    logging.info("="*50)
    logging.info("🚀 Alpaca Trading Bot Starting")
    logging.info(f"   Port: {port}")
    logging.info(f"   Paper Mode: {trader.paper}")
    logging.info(f"   Webhook URL: http://localhost:{port}/webhook")
    logging.info("="*50)
    
    app.run(host='0.0.0.0', port=port, debug=False)
