import time
from datetime import datetime, timedelta, date

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
import streamlit as st

# ------------------------- STREAMLIT PAGE CONFIG ------------------------------
st.set_page_config(
    page_title="Ultimatic Option Math Magic",
    page_icon="📈",
    layout="wide"
)

st.title("Option Magic")
st.markdown("Option Brahmastra")

# ------------------------- SECRETS ------------------------------
API_KEY = st.secrets["angel"]["API_KEY"]
CLIENT_CODE = st.secrets["angel"]["CLIENT_CODE"]
PIN = st.secrets["angel"]["PIN"]
TOTP_SECRET = st.secrets["angel"]["TOTP_SECRET"]
# ----------------------------------------------------------------

# ------------------------- SIDEBAR INPUTS ------------------------------
st.sidebar.header("Lookup Parameters")
INDICES = {
    "NIFTY": dict(opt_exch="NFO", step=50),
    "SENSEX": dict(opt_exch="BFO", step=100),
}
N_AROUND = 3                 # strikes on each side
CALL_DELAY = 1.2             # seconds between API calls (Angel rate-limits aggressively)
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

index_name = st.sidebar.selectbox("Index", list(INDICES.keys()))
step = INDICES[index_name]["step"]

# Generate 5-minute interval time options from 09:15 to 15:30
time_options = []
t_curr = datetime.strptime("09:15", "%H:%M")
t_end = datetime.strptime("15:30", "%H:%M")
while t_curr <= t_end:
    time_options.append(t_curr.strftime("%H:%M"))
    t_curr += timedelta(minutes=5)

t_text = st.sidebar.selectbox("Candle Start Time (HH:MM)", options=time_options, index=1)
d_text = st.sidebar.date_input("Date", value=date.today())
centre = st.sidebar.number_input(f"Strike Price (multiple of {step})", value=25000, step=step)

# Expiry option dropdown for Current Week and Next Week
expiry_option = st.sidebar.selectbox("Expiry", options=["Current Week", "Next Week"])

min_total = st.sidebar.slider("Minimum Total for Pair Match", min_value=1, max_value=8, value=6)

run_button = st.sidebar.button("Run Lookup & Analysis", type="primary")

# ------------------------- CORE FUNCTIONS ------------------------------

def login(api_key, client_code, pin, totp_secret):
    smart = SmartConnect(api_key=api_key)
    res = smart.generateSession(client_code, pin, pyotp.TOTP(totp_secret).now())
    if not res or not res.get("status"):
        raise RuntimeError(f"Login failed: {res}")
    return smart


def load_option_map(index_name, expiry_selection):
    """Return (expiry_date, {(strike, 'CE'/'PE'): (token, symbol)})."""
    df = pd.DataFrame(requests.get(SCRIP_URL, timeout=90).json())
    cfg = INDICES[index_name]
    df = df[(df["instrumenttype"] == "OPTIDX") & (df["name"] == index_name)
            & (df["exch_seg"] == cfg["opt_exch"])].copy()
    df["expiry_d"] = pd.to_datetime(df["expiry"], format="%d%b%Y").dt.date
    df = df[df["expiry_d"] >= date.today()]
    expiries = sorted(df["expiry_d"].unique())
    if not expiries:
        raise RuntimeError("No running expiries found")

    if expiry_selection == "Next Week" and len(expiries) > 1:
        expiry = expiries[1]
    else:
        expiry = expiries[0]

    df = df[df["expiry_d"] == expiry]
    df["strike_f"] = df["strike"].astype(float) / 100.0
    df["otype"] = df["symbol"].str[-2:]
    cmap = {(r.strike_f, r.otype): (r.token, r.symbol) for r in df.itertuples()}
    return expiry, cmap


def fetch_candle(smart, exch, token, candle_start):
    """Return dict of the 5-min candle starting at candle_start, else None."""
    frm = max(candle_start - timedelta(minutes=10), candle_start.replace(hour=9, minute=15))
    params = {
        "exchange": exch,
        "symboltoken": token,
        "interval": "FIVE_MINUTE",
        "fromdate": frm.strftime("%Y-%m-%d %H:%M"),
        "todate": (candle_start + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"),
    }
    for wait in (2, 4, 8):                    # retry with growing back-off
        try:
            res = smart.getCandleData(params)
        except Exception as e:
            time.sleep(wait)
            continue
        finally:
            time.sleep(CALL_DELAY)
        if res and res.get("status") and res.get("data") is not None:
            for ts, o, h, l, c, v in res["data"]:
                if datetime.fromisoformat(ts).replace(tzinfo=None) == candle_start:
                    return dict(open=o, high=h, low=l, close=c, volume=v)
            return None                       # no trade in that candle
        time.sleep(wait)
    return None


def compare_pairs(df, min_total=6):
    """
    Compare EVERY CE strike with EVERY PE strike in df (same candle).
     ce_count = number of PE {O,H,L,C} lying inside [CE low, CE high]
     pe_count = number of CE {O,H,L,C} lying inside [PE low, PE high]
     total    = ce_count + pe_count   (max 8)
    """
    cols = ["open", "high", "low", "close"]
    ce_df = df[df["type"] == "CE"].dropna(subset=cols)
    pe_df = df[df["type"] == "PE"].dropna(subset=cols)

    results = []
    for c in ce_df.itertuples(index=False):
        c_vals = [c.open, c.high, c.low, c.close]
        for p in pe_df.itertuples(index=False):
            p_vals = [p.open, p.high, p.low, p.close]
            ce_cnt = sum(c.low <= v <= c.high for v in p_vals)   # PE values inside CE range
            pe_cnt = sum(p.low <= v <= p.high for v in c_vals)   # CE values inside PE range
            results.append(dict(
                ce_strike=c.strike, pe_strike=p.strike,
                ce_count=ce_cnt, pe_count=pe_cnt, total=ce_cnt + pe_cnt,
                higher_high=max(c.high, p.high), lower_low=min(c.low, p.low)))

    res = pd.DataFrame(results)
    if res.empty:
        st.warning("No complete CE/PE candle data to compare.")
        return res

    matched = res[res["total"] >= min_total].sort_values(
        ["total", "ce_strike", "pe_strike"], ascending=[False, True, True])
    
    st.subheader("CE x PE Pair Comparison Results")
    st.write(f"Pairs compared: {len(res)} | Pairs with total >= {min_total}: {len(matched)}")
    
    if matched.empty:
        st.info(f"No pair reached {min_total}. Best total = {res['total'].max()}")
    else:
        st.dataframe(matched, use_container_width=True)
    return res


# ------------------------- EXECUTION FLOW ------------------------------

if run_button:
    if API_KEY == "YOUR_API_KEY" or CLIENT_CODE == "YOUR_CLIENT_CODE" or PIN == "YOUR_PIN" or TOTP_SECRET == "YOUR_TOTP_SECRET":
        st.error("Please update your actual API credentials in the code under the FILL THESE IN section.")
    else:
        try:
            # Validate inputs
            hh, mm = map(int, t_text.split(":"))
            candle_start = datetime.combine(d_text, datetime.min.time()).replace(hour=hh, minute=mm)
            
            if centre % step != 0:
                st.error(f"{index_name} strikes move in steps of {step}. Centre strike must be a multiple of {step}.")
                st.stop()

            with st.spinner("Connecting to Angel One and fetching data..."):
                smart = login(API_KEY, CLIENT_CODE, PIN, TOTP_SECRET)
                st.success("Logged in successfully!")
                
                expiry, cmap = load_option_map(index_name, expiry_option)
                st.info(f"Using {index_name} expiry ({expiry_option}): {expiry}")

                strikes = [centre + i * step for i in range(-N_AROUND, N_AROUND + 1)]
                rows = []
                
                progress_bar = st.progress(0)
                total_steps = len(strikes) * 2
                current_step = 0

                for strike in strikes:
                    for otype in ("CE", "PE"):
                        current_step += 1
                        progress_bar.progress(current_step / total_steps)
                        
                        info = cmap.get((strike, otype))
                        if not info:
                            continue
                        token, symbol = info
                        c = fetch_candle(smart, INDICES[index_name]["opt_exch"], token, candle_start)
                        rows.append(dict(
                            candle_time=candle_start.strftime("%Y-%m-%d %H:%M"),
                            index=index_name, expiry=str(expiry), strike=int(strike), type=otype,
                            symbol=symbol,
                            open=c["open"] if c else None, high=c["high"] if c else None,
                            low=c["low"] if c else None, close=c["close"] if c else None,
                            volume=c["volume"] if c else None))

                df = pd.DataFrame(rows)
                if not df.empty:
                    df = df.sort_values(["strike", "type"]).reset_index(drop=True)
                    st.subheader("Candle Data Table")
                    st.dataframe(df, use_container_width=True)
                    
                    compare_pairs(df, min_total=min_total)
                else:
                    st.warning("No data returned.")

        except Exception as e:
            st.error(f"An error occurred: {e}")
