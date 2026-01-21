import streamlit as st
import requests
import pandas as pd
import logging
from kiteconnect import KiteConnect

# ---------------------------
# PAGE SETUP
# ---------------------------
st.set_page_config(page_title="Sell Target Assistant", layout="wide")

# ---------------------------
# SECRETS (Streamlit Cloud)
# ---------------------------
API_KEY = st.secrets.get("KITE_API_KEY", "")
API_SECRET = st.secrets.get("KITE_API_SECRET", "")
INDIAN_API_KEY = st.secrets.get("INDIAN_API_KEY", "").strip()

TARGET_PRICE_URL = "https://stock.indianapi.in/stock_target_price"

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------
# SANITY CHECKS
# ---------------------------
if not API_KEY or not API_SECRET:
    st.error("❌ Missing KITE_API_KEY or KITE_API_SECRET in Streamlit Secrets.")
    st.stop()

if not INDIAN_API_KEY:
    st.error("❌ Missing INDIAN_API_KEY in Streamlit Secrets.")
    st.stop()

# ---------------------------
# HELPERS
# ---------------------------
def fetch_price_target(symbol: str) -> dict:
    """Fetch price target stats from IndianAPI and return 'priceTarget' dict."""
    try:
        resp = requests.get(
            TARGET_PRICE_URL,
            headers={"x-api-key": INDIAN_API_KEY},
            params={"stock_id": symbol},
            timeout=30,
        )
        resp.raise_for_status()
        js = resp.json()

        if not isinstance(js, dict):
            log.warning("API response for %s not a dict: %s", symbol, type(js))
            return {}

        price_target = js.get("priceTarget")
        if isinstance(price_target, dict):
            return price_target

        return {}
    except Exception as e:
        log.warning("Failed to fetch target for %s: %s", symbol, e)
        return {}

def calculate_sell_target(target_data: dict, avg_price: float):
    """Compute sell target from target_data and average buy price."""
    if not target_data or avg_price is None:
        return None

    mean = target_data.get("Mean")
    median = target_data.get("Median")
    std_dev = target_data.get("StandardDeviation") or target_data.get("StdDev")
    high = target_data.get("High")

    # Need all values
    if None in (mean, median, std_dev, high):
        return None

    base = max(mean, median)
    sell_target = min(base + std_dev, high)

    # Ensure target above avg price
    if sell_target <= avg_price:
        return high if high and high > avg_price else None

    return sell_target

def build_gtt_index(existing_gtts):
    """Index existing GTTs by tradingsymbol."""
    index = {}
    for gtt in existing_gtts or []:
        cond = gtt.get("condition", {})
        symbol = (cond.get("tradingsymbol") or "").upper()
        gid = gtt.get("id")
        if symbol and gid:
            index.setdefault(symbol, []).append(gid)
    return index

def delete_existing_gtts(kite: KiteConnect, symbol: str, gtt_index: dict) -> int:
    """Delete all GTTs for a symbol; return count deleted."""
    deleted = 0
    for tid in gtt_index.get(symbol.upper(), []):
        try:
            kite.delete_gtt(tid)
            deleted += 1
        except Exception as e:
            log.error("Failed to delete GTT %s for %s: %s", tid, symbol, e)
    return deleted

def place_gtt(kite: KiteConnect, symbol, exchange, ltp, quantity, sell_target):
    """Place a fresh sell GTT."""
    return kite.place_gtt(
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

def safe_float(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

# ---------------------------
# UI
# ---------------------------
st.title("📈 Sell Target Assistant (Streamlit Website)")
st.caption("Connect Zerodha → view holdings → compute sell targets → optionally place sell GTTs.")

with st.expander("⚠️ Disclaimer / Safety", expanded=False):
    st.write(
        "- This is an automation/analysis tool, not investment advice.\n"
        "- Verify every order before placing.\n"
        "- Kite access tokens usually expire daily; you may need to login again."
    )

# ---------------------------
# KITE LOGIN
# ---------------------------
kite = KiteConnect(api_key=API_KEY)

st.subheader("1) Connect Zerodha (Kite)")
st.write("Click login, authorize, then paste `request_token` from the redirected URL.")

st.link_button("Open Zerodha Login", kite.login_url())

request_token = st.text_input(
    "Paste request_token (from redirected URL after login)",
    placeholder="e.g. 3e9c9c1f....",
)

colA, colB = st.columns([1, 3])

if "access_token" not in st.session_state:
    st.session_state.access_token = None

with colA:
    gen = st.button("Generate Session", type="primary")

with colB:
    if st.session_state.access_token:
        st.success("✅ Connected for this session (access_token set).")
    else:
        st.info("Not connected yet.")

if gen:
    if not request_token:
        st.error("Please paste request_token.")
        st.stop()
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        st.session_state.access_token = session["access_token"]
        st.success("✅ Session generated. You can now load holdings.")
    except Exception as e:
        st.error(f"❌ Failed to generate session: {e}")
        st.stop()

if not st.session_state.access_token:
    st.stop()

kite.set_access_token(st.session_state.access_token)

# ---------------------------
# HOLDINGS
# ---------------------------
st.subheader("2) Portfolio Holdings")

try:
    holdings = kite.holdings()
except Exception as e:
    st.error(f"❌ Failed to fetch holdings: {e}")
    st.stop()

if not holdings:
    st.info("No holdings found.")
    st.stop()

rows = []
for h in holdings:
    symbol = (h.get("tradingsymbol") or "").split("-")[0].upper()
    rows.append(
        {
            "symbol": symbol,
            "exchange": h.get("exchange") or "NSE",
            "quantity": int(h.get("quantity") or 0),
            "avg_price": safe_float(h.get("average_price")),
            "ltp": safe_float(h.get("last_price")),
        }
    )

df = pd.DataFrame(rows).sort_values("symbol")
st.dataframe(df, use_container_width=True)

# ---------------------------
# PROCESS TARGETS
# ---------------------------
st.subheader("3) Sell Targets + GTT")

num = st.slider("How many holdings to process?", 1, len(df), min(10, len(df)))
selected = df.head(num).copy()

# fetch existing GTTs once
try:
    existing_gtts = kite.get_gtts()
except Exception as e:
    existing_gtts = []
    log.error("Failed to fetch existing GTTs: %s", e)

gtt_index = build_gtt_index(existing_gtts)

# compute targets
results = []
with st.spinner("Fetching targets and computing sell zones..."):
    for _, r in selected.iterrows():
        symbol = r["symbol"]
        exch = r["exchange"]
        qty = int(r["quantity"])
        avg_price = safe_float(r["avg_price"])
        ltp = safe_float(r["ltp"])

        target_data = fetch_price_target(symbol)
        sell_target = calculate_sell_target(target_data, avg_price) if avg_price else None

        profit_pct = None
        if sell_target is not None and avg_price:
            profit_pct = ((sell_target - avg_price) / avg_price) * 100

        results.append(
            {
                "symbol": symbol,
                "exchange": exch,
                "qty": qty,
                "avg_price": avg_price,
                "ltp": ltp,
                "sell_target": sell_target,
                "profit_%": round(profit_pct, 2) if profit_pct is not None else None,
                "has_target_data": bool(target_data),
            }
        )

res_df = pd.DataFrame(results)
st.dataframe(res_df, use_container_width=True)

# ---------------------------
# GTT ACTIONS
# ---------------------------
st.divider()
st.subheader("4) Place / Update GTT (careful)")

dry_run = st.toggle("Dry run (do NOT place orders)", value=True)
st.caption("If Dry run is ON, buttons will simulate actions without placing GTT.")

for row in results:
    symbol = row["symbol"]
    sell_target = row["sell_target"]
    qty = row["qty"]
    exch = row["exchange"]
    ltp = row["ltp"]

    c1, c2, c3, c4 = st.columns([2, 2, 2, 6])
    c1.write(f"**{symbol}**")
    c2.write(f"Target: **{sell_target if sell_target else '—'}**")
    c3.write(f"Qty: **{qty}**")

    disabled = (sell_target is None) or (qty <= 0) or (ltp is None)

    if c4.button(f"Delete old + Place GTT for {symbol}", key=f"gtt_{symbol}", disabled=disabled):
        if dry_run:
            st.warning(f"DRY RUN: Would delete old GTTs for {symbol} and place new GTT at {sell_target}.")
            continue

        try:
            deleted = delete_existing_gtts(kite, symbol, gtt_index)
            resp = place_gtt(kite, symbol, exch, ltp, qty, sell_target)
            st.success(f"✅ Done for {symbol}. Deleted {deleted} old GTT(s). Response: {resp}")
        except Exception as e:
            st.error(f"❌ Failed for {symbol}: {e}")
