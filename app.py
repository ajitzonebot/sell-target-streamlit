import streamlit as st
import requests
import pandas as pd
import logging
from kiteconnect import KiteConnect


KITE_API_KEY = st.secrets.get("KITE_API_KEY", "")
KITE_API_SECRET = st.secrets.get("KITE_API_SECRET", "")
INDIAN_API_KEY = st.secrets.get("INDIAN_API_KEY", "")


TARGET_PRICE_URL = "https://stock.indianapi.in/stock_target_price"

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# --- Sanity checks for keys ---
if not API_KEY or not API_SECRET:
    raise RuntimeError("❌ KITE_API_KEY or KITE_API_SECRET is missing in userdata.")

if not INDIAN_API_KEY:
    raise RuntimeError("❌ INDIAN_API_KEY is missing in userdata.")

# --- Kite Setup ---
kite = KiteConnect(api_key=API_KEY)
print("Login URL:", kite.login_url())
request_token = input("Enter the request token from the redirected URL after login: ").strip()
session = kite.generate_session(request_token, api_secret=API_SECRET)
kite.set_access_token(session["access_token"])


# --- Helpers ---
def fetch_price_target(symbol: str):
    """
    Fetch price target stats from IndianAPI.

    Based on your sample, the structure is:
    {
      "priceTarget": { ... },
      "priceTargetSnapshots": { ... },
      ...
    }
    We just return the 'priceTarget' dict.
    """
    try:
        resp = requests.get(
            TARGET_PRICE_URL,
            headers={"x-api-key": INDIAN_API_KEY},
            params={"stock_id": symbol},  # Change this key if API docs say otherwise
            timeout=30,
        )
        resp.raise_for_status()
        js = resp.json()

        if not isinstance(js, dict):
            log.warning(f"API response for {symbol} is not a dict: {type(js)}")
            return {}

        price_target = js.get("priceTarget")
        if isinstance(price_target, dict):
            return price_target

        log.warning(f"No 'priceTarget' key found in API response for {symbol}")
        return {}

    except Exception as e:
        log.warning(f"Failed to fetch target for {symbol}: {e}")
        return {}


def build_gtt_index(existing_gtts):
    """Index existing GTTs by tradingsymbol for faster lookup."""
    index = {}
    for gtt in existing_gtts:
        cond = gtt.get("condition", {})
        symbol = cond.get("tradingsymbol", "").upper()
        if symbol:
            index.setdefault(symbol, []).append(gtt["id"])
    return index


def delete_existing_gtts(symbol: str, gtt_index: dict):
    """Delete all GTTs for a given symbol."""
    for tid in gtt_index.get(symbol.upper(), []):
        try:
            kite.delete_gtt(tid)
            log.info(f"🗑️ Deleted existing GTT {tid} for {symbol}")
            print(f"Deleted existing GTT {tid} for {symbol}")
        except Exception as e:
            log.error(f"❌ Failed to delete GTT {tid} for {symbol}: {e}")
            print(f"Failed to delete GTT {tid} for {symbol}: {e}")


def calculate_sell_target(target_data: dict, avg_price: float):
    """Compute optimal sell target from target_data and average buy price."""
    if not target_data or avg_price is None:
        return None

    mean = target_data.get("Mean")
    median = target_data.get("Median")
    std_dev = target_data.get("StandardDeviation") or target_data.get("StdDev")
    high = target_data.get("High")

    if None in (mean, median, std_dev, high):
        log.info(f"Incomplete target data: {target_data}")
        return None

    base = max(mean, median)
    sell_target = min(base + std_dev, high)

    # Ensure target is above average price; otherwise fall back to High if that helps
    if sell_target <= avg_price:
        return high if high and high > avg_price else None
    return sell_target


def place_gtt(symbol, exchange, ltp, quantity, sell_target):
    """Place a fresh sell GTT."""
    try:
        resp = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=exchange,
            trigger_values=[sell_target],
            last_price=ltp,
            orders=[
                {
                    "exchange": exchange,
                    "tradingsymbol": symbol,
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity": quantity,
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "product": kite.PRODUCT_CNC,
                    "price": sell_target,
                }
            ],
        )
        log.info(f"✅ GTT placed for {symbol}: {resp}")
        print(f"GTT placed for {symbol}: {resp}")
    except Exception as e:
        log.error(f"❌ Error placing GTT for {symbol}: {e}")
        print(f"Error placing GTT for {symbol}: {e}")


# --- Main ---
def main():
    holdings = kite.holdings()
    if not holdings:
        print("No holdings found. Exiting.")
        return

    symbols = [
        {
            "symbol": h["tradingsymbol"].split("-")[0].upper(),
            "ltp": h.get("last_price"),
            "avg_price": h.get("average_price"),
            "quantity": h.get("quantity"),
            "exchange": h.get("exchange") or "NSE",
        }
        for h in holdings
    ]

    # Prompt user for the number of stocks to process
    while True:
        try:
            num_stocks = int(input("Enter the number of stocks to process: "))
            if num_stocks > 0:
                break
            else:
                print("Please enter a positive number.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    # Select the first 'num_stocks' from the list
    filtered_symbols = symbols[:num_stocks]

    # Build GTT index for existing GTTs
    try:
        existing_gtts = kite.get_gtts()
    except Exception as e:
        existing_gtts = []
        log.error(f"Failed to fetch existing GTTs: {e}")
        print(f"Failed to fetch existing GTTs: {e}")

    gtt_index = build_gtt_index(existing_gtts)

    # Tracking lists
    no_target_data = []        # No price target returned from API
    no_valid_sell_target = []  # Have target data but failed to compute a valid sell target
    error_symbols = []         # Unexpected errors while processing

    for item in filtered_symbols:
        try:
            symbol = item["symbol"]
            ltp = item["ltp"]
            avg_price = item["avg_price"]
            qty = item["quantity"]
            exch = item["exchange"]

            log.info(f"\n🔍 Checking {symbol}...")
            print(f"\nChecking {symbol}...")

            targets = fetch_price_target(symbol)

            if not targets:
                log.info(f"→ Skipping {symbol}: No price target data.")
                print(f"Skipping {symbol}: No price target data.")
                no_target_data.append(symbol)
                continue

            sell_target = calculate_sell_target(targets, avg_price)

            if not sell_target or not qty:
                log.info(f"→ Skipping {symbol}: No valid sell target or zero quantity.")
                print(f"Skipping {symbol}: No valid sell target or zero quantity.")
                no_valid_sell_target.append(symbol)
                continue

            profit = ((sell_target - avg_price) / avg_price) * 100
            log.info(
                f"📊 {symbol} → LTP: {ltp}, Avg: {avg_price}, Target: {sell_target}, Profit: {profit:.2f}%"
            )
            print(
                f"{symbol} → LTP: {ltp}, Avg: {avg_price}, Target: {sell_target}, Profit: {profit:.2f}%"
            )

            delete_existing_gtts(symbol, gtt_index)
            place_gtt(symbol, exch, ltp, qty, sell_target)

        except Exception as e:
            log.error(f"⚠️ Unexpected error processing {item.get('symbol')}: {e}")
            print(f"Unexpected error processing {item.get('symbol')}: {e}")
            error_symbols.append(item.get("symbol", "UNKNOWN"))
            continue  # move on to next stock

    # --- Summary output ---
    if no_target_data:
        print("\n--- Stocks with NO price target data (GTT not placed) ---")
        for symbol in no_target_data:
            print(symbol)

    if no_valid_sell_target:
        print("\n--- Stocks with NO valid sell target (GTT not placed) ---")
        for symbol in no_valid_sell_target:
            print(symbol)

    combined_no_gtt = sorted(set(no_target_data + no_valid_sell_target))
    if combined_no_gtt:
        print("\n=== ALL stocks where GTT was NOT placed due to missing target or invalid sell price ===")
        for symbol in combined_no_gtt:
            print(symbol)

    if error_symbols:
        print("\n--- Stocks with unexpected errors during processing ---")
        for symbol in error_symbols:
            print(symbol)


if __name__ == "__main__":
    main()
