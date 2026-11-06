"""My Options Flow Terminal.  Run:  streamlit run terminal.py"""
import io
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import flow_core as fc

st.set_page_config(page_title="Flow Terminal", layout="wide")
st.title("Options Flow Terminal")

NASDAQ_LIST_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
YF_NASDAQ_CODES = {"NMS", "NGM", "NCM"}  # Yahoo exchange codes for the NASDAQ tiers


def parse_nasdaq_list(text):
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("File Creation")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str)
    df = df[df["Test Issue"] != "Y"]
    return set(df["Symbol"].str.strip().str.upper())


@st.cache_data(ttl=86400)
def nasdaq_symbols():
    """Official NASDAQ-listed symbol list (stocks and ETFs). Raises on failure so a failure is not cached."""
    with urllib.request.urlopen(NASDAQ_LIST_URL, timeout=15) as r:
        return parse_nasdaq_list(r.read().decode("utf-8", errors="ignore"))


@st.cache_data(ttl=86400)
def yahoo_says_nasdaq(sym):
    try:
        return str(yf.Ticker(sym).fast_info["exchange"]).upper() in YF_NASDAQ_CODES
    except Exception:
        return False


def is_nasdaq(sym):
    try:
        return sym in nasdaq_symbols()
    except Exception:  # official list unreachable: fall back to Yahoo's exchange code
        return yahoo_says_nasdaq(sym)


@st.cache_data(ttl=None)
def snapshot_time():
    """Timestamp of the current snapshot; the cache is cleared by 'Pull now', which renews it."""
    return datetime.now(ZoneInfo("America/New_York"))


with st.sidebar:
    raw = st.text_input("Tickers (NASDAQ-listed only, comma separated)", "", placeholder="Type NASDAQ tickers here")
    entered = list(dict.fromkeys(t.strip().upper() for t in raw.split(",") if t.strip()))
    tickers = [t for t in entered if is_nasdaq(t)]
    rejected = [t for t in entered if t not in tickers]
    if rejected:
        st.warning("Not NASDAQ-listed, skipped: " + ", ".join(rejected))
    ticker = st.selectbox("Ticker", tickers)
    min_vol = st.number_input("Min contract volume", 1, 10000, 1)
    rise = st.slider("High-IV threshold (IV rise away from the money)", 0.05, 0.60, 0.15, 0.01, format="%.2f")
    lvl_pct = st.slider("High-IV level (zone IV at or above this % counts as HIGH)", 10, 300, 50, 5)
    iv_src = st.radio("IV source", ["Own (from bid/ask mid)", "Yahoo"])
    rate = st.number_input("Risk-free rate", 0.0, 0.15, 0.04, 0.001, format="%.4f")
    win = st.slider("Strike window (+/- % of spot)", 2, 30, 10) / 100
    whole_chain = st.checkbox("Whole chain (ignore strike window)", False)
    max_spread = st.slider("Drop quotes wider than (% of mid)", 5, 200, 50) / 100
    bal_mode = st.radio("Premium balance compares", ["Any two Neutral zones (calls or puts)", "Whole smile (only if all four Neutral)", "Each pair (ITM/ITM, OTM/OTM)"])
    mids_only = st.checkbox("STO/STC check: midpoint strikes only", True)
    edge = st.slider("Bid/ask edge zone (0 = your definition)", 0, 40, 0) / 100
    only_today = st.checkbox("Only contracts whose last trade was today", True)
    st.divider()
    scan_on_pull = st.checkbox("Also scan all tickers when I pull", False)
    if st.button("Pull now", type="primary", use_container_width=True):
        st.cache_data.clear()
        if scan_on_pull:
            st.session_state["scan_now"] = True
    st.caption(f"Snapshot taken: {snapshot_time():%a %m/%d %H:%M:%S} ET (updates only when you press Pull now)")


if not tickers:
    st.info("Type one or more NASDAQ-listed tickers in the sidebar to begin.")
    st.stop()


@st.cache_data(ttl=None)  # frozen snapshot: only refreshes when you press "Pull now"
def load(sym, exp=None):
    t = yf.Ticker(sym)
    exps = list(t.options)
    exp = exp or exps[0]
    oc = t.option_chain(exp)
    try:
        spot = float(t.fast_info["last_price"])
    except Exception:
        spot = float(t.history(period="1d")["Close"].iloc[-1])
    return exps, exp, spot, oc.calls, oc.puts


try:
    exps, _, _, _, _ = load(ticker)
except Exception as e:
    st.error(f"Could not load {ticker} from Yahoo: {e}")
    st.stop()
exp = st.sidebar.selectbox("Expiration", exps)
more_exps = st.sidebar.multiselect("Add expirations to the premium chart", [e for e in exps if e != exp])
exps, exp, spot, calls, puts = load(ticker, exp)
raw_calls, raw_puts = calls, puts  # whole Yahoo chain, kept for walls and ratios

P = dict(mids_only=mids_only, min_vol=min_vol, rise=rise, level=lvl_pct / 100, own_iv=iv_src.startswith("Own"), rate=rate,
         win=1.0 if whole_chain else win, max_spread=max_spread, edge=edge, only_today=only_today)
# IV overrides typed in on the zone cards (read from widget state, so they apply on the rerun a click triggers)
IVO = {z: st.session_state.get(f"ivo_{ticker}_{exp}_{z}", "Auto") for z in fc.ZONES}
res = fc.analyze_chain(calls, puts, spot, exp, iv_override=IVO, **P)
calls, puts, c_df, p_df, summary, T = (res[k] for k in ["calls", "puts", "c_df", "p_df", "summary", "T"])
W = fc.walls(raw_calls, raw_puts, spot, P["win"])
S = fc.skew_stats(raw_calls, raw_puts, calls, puts, spot, T, rate)
n_bad = int((c_df["fill"] == fc.INVALID).sum() + (p_df["fill"] == fc.INVALID).sum())
if n_bad:
    st.warning(f"{n_bad} contracts have crossed/zero quotes (stale data) and are excluded from the totals.")

# ---- One-click scan of every ticker ----
if st.button("Scan all tickers (selected expiration if available, else nearest)") or st.session_state.pop("scan_now", False):
    rows = []
    bar = st.progress(0.0)
    for i, sym in enumerate(tickers):
        try:
            ex_list, ex, sp, cc, pp = load(sym, exp if exp in list(yf.Ticker(sym).options) else None)
            r = fc.analyze_chain(cc, pp, sp, ex, **P)
            row = {"Ticker": sym, "Exp": ex, "Spot": round(sp, 2)}
            for _, z in r["summary"].iterrows():
                row[z.zone] = f"{z.ruling_bias or z.bias}{(' [' + z.ruling + ']') if z.ruling else ''} ({z.iv_level} IV, {z.dominant}) ${z.total/1e6:,.2f}M"
            rows.append(row)
        except Exception as e:
            rows.append({"Ticker": sym, "Exp": "-", "Spot": None, "ITM CALLS": f"failed: {e}"})
        bar.progress((i + 1) / len(tickers))
    st.session_state["scan"] = pd.DataFrame(rows)
if "scan" in st.session_state:
    st.dataframe(st.session_state["scan"], use_container_width=True, hide_index=True)

st.subheader(f"{ticker}  |  spot {spot:,.2f}  |  exp {exp}")

# ---- Skew chart (Barchart style) ----
def clean(d):
    d = d[(d["impliedVolatility"] > 0.02) & fc.quoted(d)]
    return d.sort_values("strike")[["strike", "impliedVolatility"]]


if P["own_iv"]:
    # skew chart judges liquidity by trades, not by the quote filter: traded strikes with wide/stale quotes still plot
    cl = fc.skew_iv(raw_calls, spot, "call", T, rate, max_spread)
    pl = fc.skew_iv(raw_puts, spot, "put", T, rate, max_spread)
    cl, pl = (d[d["impliedVolatility"] > 0.02].sort_values("strike")[["strike", "impliedVolatility"]] for d in (cl, pl))
else:
    cl, pl = clean(calls), clean(puts)
# Every strike in the window gets a point. A strike with no tight quote AND no trade in the last session plots as 0,
# so the line runs flat along 0 until the next strike that has a valid quote.
_lo, _hi = spot * (1 - P["win"]), spot * (1 + P["win"])
grid = sorted({float(k) for k in pd.concat([pd.to_numeric(raw_calls["strike"]), pd.to_numeric(raw_puts["strike"])]).dropna()
               if _lo <= k <= _hi})


def on_grid(d):
    return d.set_index("strike")["impliedVolatility"].reindex(grid).fillna(0.0)


cs, ps = on_grid(cl), on_grid(pl)
both_iv = ((cs + ps) / 2).where((cs > 0) & (ps > 0), 0.0)


def iv_trace(s, name, color, symbol, **kw):
    y = (s * 100).round(2)
    return go.Scatter(x=grid, y=y, name=name, mode="lines+markers", line=dict(color=color),
                      marker=dict(symbol=symbol, size=[6 if v > 0 else 0 for v in y]), **kw)
dte = (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now(ZoneInfo("America/New_York")).date()).days

fig = go.Figure()
fig.add_trace(iv_trace(both_iv, "Call & Put", "#9a9a9a", "circle", visible="legendonly"))
fig.add_trace(iv_trace(cs, "Call", "#2ca02c", "diamond"))
fig.add_trace(iv_trace(ps, "Put", "#d62728", "square"))
fig.add_vline(x=spot, line_width=1, line_color="#888", annotation_text=f"Last Price: {spot:,.2f}",
              annotation_font_color="#2ca02c", annotation_position="top right")
if W["call"]:
    fig.add_vline(x=W["call"]["strike"], line_width=1.5, line_dash="dash", line_color="#2ca02c",
                  annotation_text=f"Call wall {W['call']['strike']:g} (OI {W['call']['oi']:,.0f})",
                  annotation_font_color="#2ca02c", annotation_position="bottom right")
if W["put"]:
    fig.add_vline(x=W["put"]["strike"], line_width=1.5, line_dash="dash", line_color="#d62728",
                  annotation_text=f"Put wall {W['put']['strike']:g} (OI {W['put']['oi']:,.0f})",
                  annotation_font_color="#d62728", annotation_position="bottom left")
fig.update_layout(
    title=dict(text=f"{ticker} - Options Volatility Skew - {datetime.strptime(exp, '%Y-%m-%d'):%m/%d/%y} ({dte} DTE)", x=0.5),
    height=420, paper_bgcolor="#181818", plot_bgcolor="#181818", font=dict(color="#cccccc"),
    legend=dict(orientation="h", x=0.5, xanchor="center", y=1.0, yanchor="bottom"),
    yaxis=dict(title="Volatility", gridcolor="#444", rangemode="tozero", hoverformat=".2f"),
    xaxis=dict(gridcolor="#333", tickangle=-70, dtick=5, title=None), hovermode="x unified")
st.plotly_chart(fig, use_container_width=True)
st.caption("Click a legend item to toggle it. 'Call & Put' = average of call and put IV at the same strike. "
           "Walls = the call strike above spot and the put strike below spot with the most open interest. "
           "A flat line at 0 means that strike is illiquid (no valid two-sided quote within the spread filter).")


def na(v, fmt):
    return "N/A" if pd.isna(v) else fmt.format(v)


# ---- Skew & expected move ----
st.subheader("Skew & expected move")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Put/call (volume)", na(S["pc_vol"], "{:.2f}"), help="Total put volume / total call volume across the whole chain for this expiration.")
m2.metric("Put/call (open interest)", na(S["pc_oi"], "{:.2f}"), help="Total put open interest / total call open interest across the whole chain.")
m3.metric("25-delta risk reversal", na(S["rr25"] * 100, "{:+.1f} vol pts"),
          help="25-delta call IV minus 25-delta put IV, using OTM options. Negative = puts priced richer than calls. "
               "N/A means the strike window does not reach 25 delta: widen it or tick Whole chain.")
m4.metric("Straddle move", "N/A" if pd.isna(S["straddle"]) else f"±${S['straddle']:,.2f} (±{S['straddle_pct']:.1%})",
          help="ATM call mid + ATM put mid: roughly the market's expected absolute move by expiration.")
m5.metric("1-SD move (ATM IV)", "N/A" if pd.isna(S["one_sd"]) else f"±${S['one_sd']:,.2f} (±{S['one_sd_pct']:.1%})",
          help="Spot x ATM IV x sqrt(time to expiry), from the ATM call/put midpoints.")
if not pd.isna(S["straddle"]):
    m4.caption(f"{spot - S['straddle']:,.2f} to {spot + S['straddle']:,.2f}")
if not pd.isna(S["one_sd"]):
    m5.caption(f"{spot - S['one_sd']:,.2f} to {spot + S['one_sd']:,.2f}")

# ---- Highest premium by strike ----
SIGNAL_COLORS = {"STO": "#1f77b4", "STC": "#ff7f0e", "Opening": "#9467bd", "Unclear": "#888888"}


def money(v):
    v = abs(v)
    return "$0" if v == 0 else (f"${v / 1e6:,.1f}M" if v >= 1e6 else f"${v / 1e3:,.0f}K")


def premium_by_strike_chart(rows, top_n, title):
    g = rows.groupby(["strike", "type", "signal"], as_index=False)["premium"].sum()
    keep = g.groupby("strike")["premium"].sum().nlargest(top_n).index
    g = g[g["strike"].isin(keep)]
    strikes = np.sort(g["strike"].unique())
    width = (np.diff(strikes).min() if len(strikes) > 1 else max(spot * 0.01, 1.0)) * 0.8
    f = go.Figure()
    shown = set()
    for sig, col in SIGNAL_COLORS.items():
        for typ, sign in (("Call", 1), ("Put", -1)):
            d = g[(g["signal"] == sig) & (g["type"] == typ)]
            if d.empty:
                continue
            f.add_bar(x=d["strike"], y=sign * d["premium"], name=sig, legendgroup=sig, showlegend=sig not in shown,
                      marker_color=col, width=width, customdata=d["premium"],
                      hovertemplate=f"Strike %{{x:g}} | {typ} | {sig}<br>$%{{customdata:,.0f}}<extra></extra>")
            shown.add(sig)
    m = float(g.groupby(["strike", "type"])["premium"].sum().max())
    tv = np.linspace(-m, m, 7)
    f.add_vline(x=spot, line_width=1, line_color="#888", annotation_text=f"Last Price: {spot:,.2f}",
                annotation_font_color="#cccccc", annotation_position="top right")
    if W["call"]:
        f.add_vline(x=W["call"]["strike"], line_width=1.5, line_dash="dash", line_color="#2ca02c",
                    annotation_text="Call wall", annotation_font_color="#2ca02c", annotation_position="top right")
    if W["put"]:
        f.add_vline(x=W["put"]["strike"], line_width=1.5, line_dash="dash", line_color="#d62728",
                    annotation_text="Put wall", annotation_font_color="#d62728", annotation_position="top left")
    f.update_layout(barmode="relative", height=440, title=dict(text=title, x=0.5),
                    paper_bgcolor="#181818", plot_bgcolor="#181818", font=dict(color="#cccccc"),
                    legend=dict(orientation="h", x=0.5, xanchor="center", y=1.0, yanchor="bottom"),
                    yaxis=dict(title="Premium: calls above, puts below", gridcolor="#444", range=[-m * 1.1, m * 1.1],
                               tickvals=tv, ticktext=[money(v) for v in tv]),
                    xaxis=dict(title="Strike", gridcolor="#333"), hovermode="closest")
    return f


st.subheader("Highest premium by strike")
frames = [fc.contract_rows(c_df, p_df, exp)]
for e in more_exps:
    try:
        _, _, sp_e, cc_e, pp_e = load(ticker, e)
        r_e = fc.analyze_chain(cc_e, pp_e, sp_e, e, **P)
        frames.append(fc.contract_rows(r_e["c_df"], r_e["p_df"], e))
    except Exception as ex:
        st.warning(f"Could not load {e}: {ex}")
prem_rows = pd.concat(frames, ignore_index=True)
top_n = st.slider("Strikes to show (ranked by total premium)", 5, 60, 25)
if prem_rows.empty:
    st.info("No contracts pass the current filters (try lowering Min contract volume or untick 'Only contracts whose last trade was today').")
else:
    st.plotly_chart(premium_by_strike_chart(prem_rows, top_n, f"{ticker} - Premium by strike - " + ", ".join([exp] + more_exps)),
                    use_container_width=True)
    st.caption("Bar color = S_la signal for the contract (STO, STC, Opening, Unclear). Premium from several expirations stacks at the same strike.")

# ---- Zone summary cards ----
st.subheader("Zone diagnosis")
D = "\\$"  # escaped dollar sign so Streamlit does not treat "$...$" as math
for _, r in summary.iterrows():
    with st.container(border=True):
        a, b = st.columns([1, 2])
        _meas = (f"{r.iv_measured}, {r.iv_change:+.0%} away from money, level {r.iv_abs:.0%}" if r.iv_measured != "N/A" else "N/A")
        if r.iv_manual:
            a.markdown(f"**{r.zone}**  \nIV: **{r.iv_level}** (manual override; measured: {_meas})")
        else:
            a.markdown(f"**{r.zone}**  \nIV: **{r.iv_level}**" + (f" ({r.iv_change:+.0%} away from money, level {r.iv_abs:.0%})" if r.iv_level != "N/A" else ""))
        a.radio("IV override", ["Auto", "HIGH", "LOW"], horizontal=True, key=f"ivo_{ticker}_{exp}_{r.zone}",
                help="Auto uses the measured label. Pick HIGH or LOW to replace it; the playbook line, the premium balance and the rest of the page follow it. Kept per ticker and expiration.")
        a.markdown(f"Dominant: **{r.dominant}** ({r.dominant_share:.0%} of {D}{r.total:,.0f})")
        b.markdown(f"Ask {D}{r.ask:,.0f} | Mid {D}{r.mid:,.0f} | Bid {D}{r.bid:,.0f}")
        tag = " *(your definition)*" if r.src == "user" else ""
        b.markdown(f"**{r.title}**{tag}")
        b.markdown(f"**{r.bias}**: {r.read}")
        if r.ruling:
            if r.ruling == "UNDETERMINED":
                b.markdown("**Undetermined**")
            else:
                b.markdown(f"**{r.ruling}**")
                b.markdown(f"**{r.ruling_bias}**: {r.ruling_text}")
                b.caption("Confirm tomorrow morning: OI up = STO, OI down = STC.")

bars = go.Figure()
for fill, col in [(fc.ASK, "#2ca02c"), (fc.MID, "#aaaaaa"), (fc.BID, "#d62728")]:
    bars.add_bar(x=summary["zone"], y=summary[{fc.ASK: "ask", fc.MID: "mid", fc.BID: "bid"}[fill]], name=fill, marker_color=col)
bars.update_layout(barmode="group", height=320, template="plotly_dark", yaxis_title="Premium ($)")
st.plotly_chart(bars, use_container_width=True)

# ---- Premium balance ----
st.subheader("Premium balance (Neutral zones only)")
gap_pct = st.number_input("Overnight gap for the gap check (% of spot, + up / - down)", -25.0, 25.0, 2.0, 0.01, format="%g")
for r in fc.premium_balance(summary, c_df, p_df, spot, "neutral" if bal_mode.startswith("Any") else ("whole" if bal_mode.startswith("Whole") else "pairs")):
    with st.container(border=True):
        st.markdown(f"**{r['scope']}**")
        if r["status"] == "exchange":
            a, b, c = st.columns(3)
            a.metric("Midpoint-filled premium", f"${r['prem']:,.0f}")
            b.metric("Share-equivalent (delta)", f"{r['delta_shares']:,.0f} shares")
            c.metric("Share-equivalent (dollar)", f"{r['dollar_shares']:,.0f} shares")
            st.markdown(f"Nothing opposite is Neutral to match it against, so this neutral flow is read as an option-to-share exchange: "
                        f"the swapped options carry about **{r['delta_shares']:,.0f} shares of {r['exposure']} delta**. "
                        f"The offsetting share trade is **{r['action']} ~{r['delta_shares']:,.0f} shares**.")
            continue
        if r["status"] == "skipped":
            st.info(f"Not compared. {r['reason']}.")
            continue
        a, b, c = st.columns(3)
        za, zb = r.get("a_zone", "Calls"), r.get("b_zone", "Puts")
        a.metric(f"{za} premium", f"${r['call_prem']:,.0f}")
        b.metric(f"{zb} premium", f"${r['put_prem']:,.0f}")
        c.metric("Imbalance", f"${abs(r['diff']):,.0f}", f"more {(za if r['diff'] > 0 else zb).lower()}")
        st.markdown(f"To balance: **{r['action']} ~{r['shares_dollar']:,.0f} shares** (dollar-equivalent: imbalance / spot)")
        st.markdown(f"Delta view: {za.lower()} {r['call_delta_sh']:,.0f} vs {zb.lower()} {r['put_delta_sh']:,.0f} share-equivalents, "
                    f"so **{r['delta_action']} ~{r['shares_delta']:,.0f} shares** to flatten delta")

        # ---- Overnight gap check: Net Portfolio Delta = Initial Delta + Gamma x gap ----
        g = fc.gamma_gap(r, c_df, p_df, spot, T, rate, gap_pct)
        st.markdown("**Overnight gap check**")
        a, b, c = st.columns(3)
        a.metric("Flow gamma", f"{g['G']:,.0f} shares per $1",
                 help=f"Calls {g['gamma_c']:,.0f} + puts {g['gamma_p']:,.0f}. How many shares of delta this flow adds for each $1 the stock moves.")
        b.metric("Gap", f"{g['dS']:+,.2f} ({gap_pct:+g}%)")
        c.metric("Leverage ratio", "N/A" if pd.isna(g["lev"]) else f"{g['lev']:,.0f}x",
                 help="Delta-view shares divided by To-balance shares: how much exposure each dollar of premium buys. "
                      "It does not show whether the position is long or short gamma.")
        drift = g["G"] * g["dS"]
        pnl = 0.5 * g["G"] * g["dS"] ** 2
        rows_g = []
        for name, sgn, verdict in (("Long these options (long gamma)", 1, "HOLD"), ("Short these options (short gamma)", -1, "FORCE LIQUIDATE")):
            net = sgn * drift
            rows_g.append({"If the fund is": name,
                           "Options delta after gap": f"{g['D0'] + net:+,.0f} sh",
                           "Offsetting shares": f"{-g['D0']:+,.0f} sh",
                           "Net portfolio delta": f"{net:+,.0f} sh",
                           "Re-hedge at open": ("BUY " if net < 0 else "SELL ") + f"{abs(net):,.0f} sh",
                           "Gamma P&L (approx.)": f"${sgn * pnl:+,.0f}",
                           "Rule": verdict})
        gs = fc.gamma_side(r, c_df, p_df)
        on_rule = {"LONG": "HOLD", "SHORT": "FORCE LIQUIDATE"}.get(gs["side"])

        def _hl(row):
            if on_rule is None or row["Rule"] != on_rule:
                return [""] * len(row)
            col = "#1e4d2b" if on_rule == "HOLD" else "#5a1e1e"
            return [f"background-color: {col}; font-weight: bold"] * len(row)

        st.dataframe(pd.DataFrame(rows_g).style.apply(_hl, axis=1), use_container_width=True, hide_index=True)

        evid = (f"{D}{gs['long_prem']:,.0f} bought at/above ask (BTO, {gs['n_long']} contracts) vs "
                f"{D}{gs['short_prem']:,.0f} STO by S_la Zone 1 ({gs['n_short']} contracts)")
        if gs["side"] == "LONG":
            st.success(f"**LONG GAMMA today: focus on the HOLD row.** Funds were net option buyers. {evid} "
                       f"({gs['long_share']:.0%} long).")
        elif gs["side"] == "SHORT":
            st.error(f"**SHORT GAMMA today: focus on the FORCE LIQUIDATE row.** Funds were net option sellers. {evid} "
                     f"({1 - gs['long_share']:.0%} short).")
        elif gs["side"] == "MIXED":
            st.warning(f"**MIXED today: buying and selling are close to even, so neither row is favored.** {evid}.")
        else:
            st.warning("**CAN'T TELL today: no at/above-ask prints and no S_la Zone 1 (STO) prints in these zones**, "
                       "so the flow is mostly midpoint crosses. Both rows stay possible.")
        cov = "" if pd.isna(gs["coverage"]) else f" Only {gs['coverage']:.0%} of the compared premium is classifiable (ask fills + Zone 1 STO)."
        st.caption("Call rule: BTO = filled at/above ask = long gamma; STO = S_la Zone 1 = short gamma; midpoint, STC and unclear prints do not vote; "
                   "a side needs 60% of the classified premium." + cov +
                   " Assumes the whole flow sits on one side, delta-hedged with shares, and ignores time decay and IV changes.")

# ---- STO vs STC (S_la) ----
st.subheader("STO vs STC")
allc = pd.concat([c_df.assign(type="call"), p_df.assign(type="put")])
allc["sla_zone"] = [fc.signal(x, g) for x, g in zip(allc["sla"], allc["vol_gt_oi"])]
cols = ["type", "zone", "strike", "bid", "ask", "lastPrice", "volume", "openInterest", "premium", "fill", "sla", "sla_zone"] + (["last_trade_et"] if "last_trade_et" in allc.columns else [])
st.dataframe(allc[cols].sort_values("premium", ascending=False), use_container_width=True, hide_index=True)
st.download_button("Download CSV", allc[cols].to_csv(index=False), f"{ticker}_{exp}_flow.csv")
