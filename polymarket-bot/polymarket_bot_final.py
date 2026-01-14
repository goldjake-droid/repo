import os
import time
import logging
from typing import Dict, List, Optional
from decimal import Decimal
from dotenv import load_dotenv

import requests
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


class PolymarketBot:
    """
    A market-making bot for Polymarket that interacts with the CLOB API
    and executes trades on the Polygon network.
    """
    
    def __init__(self):
        """Initialize the bot with configuration from environment variables."""
        # Load configuration
        self.private_key = os.getenv("PRIVATE_KEY")
        self.rpc_url = os.getenv("RPC_URL", "https://polygon-rpc.com")
        self.clob_api_url = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
        self.chain_id = int(os.getenv("CHAIN_ID", "137"))  # Polygon mainnet
        self.enable_live_trading = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

        # Validate required environment variables
        validation_errors = self.validate_config()
        if validation_errors:
            error_block = "\n".join(f"- {error}" for error in validation_errors)
            raise ValueError(f"Configuration validation failed:\n{error_block}")

        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.account = Account.from_key(self.private_key)
        self.address = self.account.address

        logger.info("Initialized bot with address: %s", self.address)

        # Initialize CLOB client
        self.client = ClobClient(
            self.clob_api_url,
            key=self.private_key,
            chain_id=self.chain_id,
        )

        # Trading parameters (customize as needed)
        self.spread_percentage = Decimal(os.getenv("SPREAD_PERCENTAGE", "0.02"))  # 2% spread
        self.order_size = Decimal(os.getenv("ORDER_SIZE", "10"))  # Size in USDC
        self.min_profit_threshold = Decimal(os.getenv("MIN_PROFIT", "0.01"))

        # Contract addresses (Polygon mainnet)
        self.ctf_exchange = os.getenv(
            "CTF_EXCHANGE",
            "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        )
        self.collateral_token = os.getenv(
            "COLLATERAL_TOKEN",
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC on Polygon
        )

    def validate_config(self) -> List[str]:
        """Validate environment configuration before trading."""
        errors: List[str] = []
        required_fields = {
            "PRIVATE_KEY": self.private_key,
            "TOKEN_ID": os.getenv("TOKEN_ID"),
        }

        for key, value in required_fields.items():
            if not value:
                errors.append(f"{key} is required")
            if value and "YOUR_" in value:
                errors.append(f"{key} must be replaced with a real value")

        if not self.enable_live_trading and not self.dry_run:
            errors.append("Set ENABLE_LIVE_TRADING=true or DRY_RUN=true")

        if self.enable_live_trading and self.dry_run:
            logger.warning("Both ENABLE_LIVE_TRADING and DRY_RUN are set; DRY_RUN will be used.")

        return errors

    def preflight_checks(self) -> None:
        """Run preflight connectivity checks before trading."""
        rpc_ok = self.w3.is_connected()
        if not rpc_ok:
            raise ConnectionError("RPC connection failed. Check RPC_URL.")

        logger.info("RPC connection healthy.")

        if self.dry_run:
            logger.info("Dry run enabled; skipping CLOB API check.")
            return

        timeout = int(os.getenv("PREFLIGHT_TIMEOUT", "10"))
        try:
            response = requests.get(self.clob_api_url, timeout=timeout)
            response.raise_for_status()
            logger.info("CLOB API reachable.")
        except Exception as exc:
            raise ConnectionError(f"CLOB API check failed: {exc}") from exc
        
    def get_market_data(self, token_id: str) -> Dict:
        """
        Fetch current market data for a specific token.
        
        Args:
            token_id: The condition token ID
            
        Returns:
            Dictionary containing market data
        """
        try:
            # Get order book
            order_book = self.client.get_order_book(token_id)
            
            # Calculate mid price
            best_bid = Decimal(order_book['bids'][0]['price']) if order_book['bids'] else Decimal('0')
            best_ask = Decimal(order_book['asks'][0]['price']) if order_book['asks'] else Decimal('1')
            mid_price = (best_bid + best_ask) / 2
            
            logger.info(
                "Market data for %s: Bid=%s, Ask=%s, Mid=%s",
                token_id,
                best_bid,
                best_ask,
                mid_price,
            )
            
            return {
                'token_id': token_id,
                'best_bid': best_bid,
                'best_ask': best_ask,
                'mid_price': mid_price,
                'order_book': order_book
            }
        except Exception as e:
            logger.error("Error fetching market data: %s", e)
            return None
    
    def check_allowance(self) -> bool:
        """
        Check if the exchange has approval to spend USDC.
        
        Returns:
            True if approved, False otherwise
        """
        try:
            # USDC contract ABI (simplified - only need allowance and approve)
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [
                        {"name": "_owner", "type": "address"},
                        {"name": "_spender", "type": "address"}
                    ],
                    "name": "allowance",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "type": "function"
                },
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_spender", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "approve",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]
            
            usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.collateral_token),
                abi=erc20_abi
            )
            
            allowance = usdc_contract.functions.allowance(
                self.address,
                Web3.to_checksum_address(self.ctf_exchange)
            ).call()
            
            # Check if allowance is sufficient (check for max uint256)
            max_uint256 = 2**256 - 1
            return allowance > max_uint256 / 2
            
        except Exception as e:
            logger.error("Error checking allowance: %s", e)
            return False
    
    def approve_exchange(self) -> bool:
        """
        Approve the exchange to spend USDC.
        
        Returns:
            True if approval successful, False otherwise
        """
        try:
            if self.dry_run:
                logger.info("Dry run enabled; skipping approval transaction.")
                return True

            logger.info("Approving exchange to spend USDC...")
            
            # Use CLOB client's built-in approve function if available
            # Otherwise, construct transaction manually
            tx_hash = self.client.set_approval_for_all(
                enable=True,
                nonce=self.w3.eth.get_transaction_count(self.address)
            )
            
            logger.info("Approval transaction submitted: %s", tx_hash)
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                logger.info("Approval successful!")
                return True
            else:
                logger.error("Approval transaction failed")
                return False
                
        except Exception as e:
            logger.error("Error approving exchange: %s", e)
            return False
    
    def create_order(
        self,
        token_id: str,
        side: str,
        price: Decimal,
        size: Decimal
    ) -> Optional[str]:
        """
        Create and submit a limit order.
        
        Args:
            token_id: The condition token ID
            side: 'BUY' or 'SELL'
            price: Order price (0-1)
            size: Order size in outcome tokens
            
        Returns:
            Order ID if successful, None otherwise
        """
        try:
            # Validate inputs
            if price <= 0 or price >= 1:
                logger.error("Invalid price: %s. Must be between 0 and 1", price)
                return None
            
            logger.info("Creating %s order: %s @ %s for token %s", side, size, price, token_id)

            if self.dry_run:
                logger.info("Dry run enabled; order not submitted.")
                return "dry-run"
            
            # Create order using CLOB client
            order_args = OrderArgs(
                price=float(price),
                size=float(size),
                side=BUY if side.upper() == 'BUY' else SELL,
                token_id=token_id
            )
            
            # Sign and submit order
            signed_order = self.client.create_order(order_args)
            order_id = signed_order.get('orderID')
            
            logger.info("Order created successfully: %s", order_id)
            return order_id
            
        except Exception as e:
            logger.error("Error creating order: %s", e)
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an existing order.
        
        Args:
            order_id: The order ID to cancel
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info("Cancelling order: %s", order_id)
            if self.dry_run:
                logger.info("Dry run enabled; cancel not submitted.")
                return True

            self.client.cancel(order_id)
            logger.info("Order cancelled successfully")
            return True
        except Exception as e:
            logger.error("Error cancelling order: %s", e)
            return False
    
    def get_open_orders(self) -> List[Dict]:
        """
        Get all open orders for the account.
        
        Returns:
            List of open orders
        """
        try:
            if self.dry_run:
                return []

            orders = self.client.get_orders()
            return [o for o in orders if o.get('status') == 'LIVE']
        except Exception as e:
            logger.error("Error fetching open orders: %s", e)
            return []
    
    def market_making_strategy(self, token_id: str):
        """
        Simple market-making strategy: place orders on both sides of the book.
        
        Args:
            token_id: The condition token ID to trade
        """
        try:
            # Get current market data
            market_data = self.get_market_data(token_id)
            if not market_data:
                return
            
            mid_price = market_data['mid_price']
            
            # Calculate bid and ask prices with spread
            bid_price = mid_price * (1 - self.spread_percentage)
            ask_price = mid_price * (1 + self.spread_percentage)
            
            # Ensure prices are within valid range [0.01, 0.99]
            bid_price = max(Decimal('0.01'), min(Decimal('0.99'), bid_price))
            ask_price = max(Decimal('0.01'), min(Decimal('0.99'), ask_price))
            
            # Cancel existing orders first
            open_orders = self.get_open_orders()
            for order in open_orders:
                if order.get('asset_id') == token_id:
                    self.cancel_order(order['id'])
                    time.sleep(0.5)  # Rate limiting
            
            # Place new bid order
            bid_order_id = self.create_order(
                token_id=token_id,
                side='BUY',
                price=bid_price,
                size=self.order_size
            )
            
            time.sleep(0.5)  # Rate limiting
            
            # Place new ask order
            ask_order_id = self.create_order(
                token_id=token_id,
                side='SELL',
                price=ask_price,
                size=self.order_size
            )
            
            logger.info("Market making orders placed: Bid=%s, Ask=%s", bid_order_id, ask_order_id)
            
        except Exception as e:
            logger.error("Error in market making strategy: %s", e)
    
    def copy_trading_strategy(self, target_address: str, token_id: str):
        """
        Copy trades from a target address.
        
        Args:
            target_address: Address to copy trades from
            token_id: The token to monitor
        """
        try:
            # Get recent trades for the target address
            # Note: This requires access to trade history API endpoint
            url = f"{self.clob_api_url}/trades"
            params = {
                'market': token_id,
                'maker_address': target_address
            }
            
            if self.dry_run:
                logger.info("Dry run enabled; skipping copy trading fetch.")
                return

            response = requests.get(url, params=params)
            response.raise_for_status()
            trades = response.json()
            
            # Process recent trades (implement your logic here)
            for trade in trades[:5]:  # Last 5 trades
                side = 'BUY' if trade.get('side') == 'BUY' else 'SELL'
                price = Decimal(str(trade.get('price', 0)))
                size = Decimal(str(trade.get('size', 0))) * Decimal('0.5')  # Copy with 50% size
                
                logger.info(
                    "Copying trade from %s: %s %s @ %s",
                    target_address,
                    side,
                    size,
                    price,
                )
                
                # Place order
                self.create_order(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size
                )
                
                time.sleep(1)  # Rate limiting
                
        except Exception as e:
            logger.error("Error in copy trading strategy: %s", e)
    
    def run(self, token_id: str, strategy: str = 'market_making', interval: int = 30):
        """
        Main bot loop.
        
        Args:
            token_id: The token ID to trade
            strategy: Trading strategy ('market_making' or 'copy_trading')
            interval: Update interval in seconds
        """
        logger.info("Starting bot with strategy: %s", strategy)
        
        # Check and set approval if needed
        if not self.check_allowance():
            logger.info("Exchange not approved. Requesting approval...")
            if not self.approve_exchange():
                logger.error("Failed to approve exchange. Exiting.")
                return
        
        # Main loop
        try:
            while True:
                if strategy == 'market_making':
                    self.market_making_strategy(token_id)
                elif strategy == 'copy_trading':
                    target_address = os.getenv('TARGET_ADDRESS')
                    if not target_address:
                        logger.error("TARGET_ADDRESS not set for copy trading")
                        break
                    self.copy_trading_strategy(target_address, token_id)
                else:
                    logger.error("Unknown strategy: %s", strategy)
                    break
                
                logger.info("Waiting %s seconds before next update...", interval)
                time.sleep(interval)
                
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error("Unexpected error in main loop: %s", e)


def main():
    """Main entry point."""
    bot = PolymarketBot()

    token_id = os.getenv("TOKEN_ID", "")
    if not token_id:
        raise ValueError("TOKEN_ID must be set before running the bot")

    strategy = os.getenv("STRATEGY", "market_making")
    interval = int(os.getenv("INTERVAL", "30"))
    precheck_only = os.getenv("PRECHECK_ONLY", "false").lower() == "true"

    bot.preflight_checks()
    if precheck_only:
        logger.info("Preflight checks complete; exiting due to PRECHECK_ONLY=true.")
        return

    bot.run(
        token_id=token_id,
        strategy=strategy,
        interval=interval,
    )


if __name__ == "__main__":
    main()
