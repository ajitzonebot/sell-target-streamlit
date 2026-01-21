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
def safe_float(x):
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

@st.cache_data(ttl=6 * 60 * 60)  # cache 6 hours
def fetch_price_target_cached(symbol: str, indian_api_key: str) -> dict:
    """Fetch price target stats from IndianAPI and return 'priceTarget' dict (cached)."""
    try:
        resp = requests.get(
            TARGET_PRICE_URL,
            headers={"x-api-key": indian_api_key},
            params={"stock_id": symbol},
            timeout=30,
        )
        resp.raise_for_status()
        js = resp.json()
        if not isinstance(js, dict):
            return {}
        price_target = js.get("priceTarget")
        return price_target if isinstance(price_target, dict) else {}
    except Exception:
        return {}

def calculate_sell_target(target_data: dict, avg_price: float):
    """Compute sell target from target_data and average buy price."""
    if not target_data or avg_price is None:
        return None

    mean = target_data.get("Mean")
    median = target_data.get("Median")
    std_dev = target_data.get("StandardDeviation") or target_data.get("StdDev")
    high = target_data.get("High")

    if None in (mean, median, std_dev, high):
        return None

    base = max(mean, median)
    sell_target = min(base + std_dev, high)

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

def init_state():
    st.session_state.setdefault("access_token", None)
    st.session_state.setdefault("targets_fetched", False)
    st.session_state.setdefault("targets_results", [])
    st.session_state.setdefault("targets_df", None)
    st.session_state.setdefault("last_fetch_count", 0)

init_state()

# ---------------------------
# UI
# ---------------------------
st.title("📈 Sell Target Assistant (Streamlit Website)")
st.caption("Connect Zerodha → view holdings → fetch targets once → optionally update GTTs.")

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

colA, colB, colC = st.columns([1.2, 1.2, 3])

with colA:
    gen = st.button("Generate Session", type="primary")
with colB:
    clear_session = st.button("Clear Session")

if clear_session:
    st.session_state.access_token = None
    st.session_state.targets_fetched = False
    st.session_state.targets_results = []
    st.session_state.targets_df = None
    st.success("Session cleared. Please login again.")
    st.stop()

if gen:
    if not request_token:
        st.error("Please paste request_token.")
        st.stop()
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        st.session_state.access_token = session["access_token"]
        st.session_state.targets_fetched = False
        st.session_state.targets_results = []
        st.session_state.targets_df = None
        st.success("✅ Session generated. You can now load holdings.")
    except Exception as e:
        st.error(f"❌ Failed to generate session: {e}")
        st.stop()

if not st.session_state.access_token:
    st.info("Not connected yet.")
    st.stop()

kite.set_access_token(st.session_state.access_token)
st.success("✅ Connected for this session.")

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
# TARGETS SECTION (Fetch once on click)
# ---------------------------
st.subheader("3) Sell Targets (fetch only when you click)")

num = st.slider("How many holdings to process?", 1, len(df), min(10, len(df)))
selected = df.head(num).copy()

colx, coly, colz = st.columns([1.5, 1.5, 5])

with colx:
    fetch_btn = st.button("Fetch / Refresh targets", type="primary")
with coly:
    clear_targets = st.button("Clear targets")

if clear_targets:
    st.session_state.targets_fetched = False
    st.session_state.targets_results = []
    st.session_state.targets_df = None
    st.success("Cleared cached targets for this session.")
    st.stop()

with colz:
    if st.session_state.targets_fetched and st.session_state.targets_df is not None:
        st.success("Targets loaded. They will NOT refetch unless you click Refresh.")
    else:
        st.info("Click 'Fetch / Refresh targets' to call IndianAPI once.")

# Fetch existing GTTs once per run
try:
    existing_gtts = kite.get_gtts()
except Exception as e:
    existing_gtts = []
    log.error("Failed to fetch existing GTTs: %s", e)

gtt_index = build_gtt_index(existing_gtts)

if fetch_btn:
    results = []
    with st.spinner("Fetching targets and computing sell targets..."):
        for _, r in selected.iterrows():
            symbol = r["symbol"]
            exch = r["exchange"]
            qty = int(r["quantity"])
            avg_price = safe_float(r["avg_price"])
            ltp = safe_float(r["ltp"])

            # cached API call
            target_data = fetch_price_target_cached(symbol, INDIAN_API_KEY)
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

    st.session_state.targets_results = results
    st.session_state.targets_df = pd.DataFrame(results)
    st.session_state.targets_fetched = True
    st.session_state.last_fetch_count += 1

# Show stored results (no refetch)
if st.session_state.targets_fetched and st.session_state.targets_df is not None:
    st.dataframe(st.session_state.targets_df, use_container_width=True)
else:
    st.stop()

# ---------------------------
# GTT ACTIONS
# ---------------------------
st.divider()
st.subheader("4) Update GTTs")

dry_run = st.toggle("Dry run (do NOT place orders)", value=True)
confirm_all = st.checkbox("I confirm I want to update GTTs for ALL valid targets", value=False)

results = st.session_state.targets_results

valid_rows = [
    r for r in results
    if r.get("sell_target") is not None and (r.get("qty") or 0) > 0 and r.get("ltp") is not None
]

st.write(f"Valid targets ready for GTT: **{len(valid_rows)}** / {len(results)}")

update_all_disabled = (len(valid_rows) == 0) or ((not dry_run) and (not confirm_all))
update_all = st.button("Update ALL GTTs for valid targets", disabled=update_all_disabled)

if update_all:
    if dry_run:
        st.warning("DRY RUN: No orders will be placed.")
    else:
        st.info("Placing GTTs… please do not refresh.")

    progress = st.progress(0)
    status = st.empty()

    success, failed = 0, 0

    for i, row in enumerate(valid_rows, start=1):
        symbol = row["symbol"]
        exch = row["exchange"]
        qty = int(row["qty"])
        ltp = row["ltp"]
        sell_target = row["sell_target"]

        status.write(f"Processing **{symbol}** ({i}/{len(valid_rows)})")

        try:
            if not dry_run:
                delete_existing_gtts(kite, symbol, gtt_index)
                place_gtt(kite, symbol, exch, ltp, qty, sell_target)
            success += 1
        except Exception as e:
            failed += 1
            log.error("Update ALL failed for %s: %s", symbol, e)

        progress.progress(i / len(valid_rows))

    status.write("Done.")
    st.success(f"Update ALL complete. Success: {success}, Failed: {failed}")

st.divider()
st.subheader("5) Update individual stocks")

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

    disabled = (sell_target is None) or (qty <= 0) or (ltp is None) or ((not dry_run) and False)

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
