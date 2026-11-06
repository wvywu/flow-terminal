"""Core logic for the options flow terminal (no UI, no network). Mirrors the
Institutional Positioning Map spreadsheet."""
import itertools
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

ASK, MID, BID = "At/above ask", "Midpoint (between)", "At/below bid"
INVALID = "Invalid quote (excluded)"
ZONES = ["ITM CALLS", "OTM CALLS", "ITM PUTS", "OTM PUTS"]

def E(title, bias, text, src="sheet", sto=None, stc=None):
    """One playbook entry. sto/stc = (bias, text) branches for the STO-vs-STC ruling.
    src: 'sheet' = from your spreadsheet, 'user' = definition you wrote, 'added' = filled in by me."""
    return dict(title=title, bias=bias, text=text, src=src, sto=sto, stc=stc)


# (zone, fill, iv) -> entry. iv=None matches either High or Low IV.
PLAYBOOK = {
    # ---- ITM CALLS ----
    ("ITM CALLS", ASK, "HIGH"): E("Urgent institutional buying (BTO)", "Bullish",
        "A massive fund is aggressively sweeping deep ITM contracts as a synthetic stock replacement. (Long calls, bullish)"),
    ("ITM CALLS", ASK, "LOW"): E("Patient stock replacement (BTO)", "Bullish",
        "Deep ITM calls are lifted at the ask without paying a volatility premium. A fund is building long exposure through calls instead of shares, calmly and without urgency. (Long calls, patient bullish)", "added"),
    ("ITM CALLS", MID, None): E("Neutral institutional cross trade", "Neutral",
        "Two massive funds matched their orders privately halfway between the market maker's prices. (Neutral)"),
    ("ITM CALLS", BID, "LOW"): E("Urgent institutional liquidation or shorting (STO / STC)", "Check OI",
        "Funds are hitting the bid on deep ITM calls with size.",
        sto=("Bearish", "STO: fresh short premium farming. Market makers sell stock to hedge, building a hard resistance ceiling."),
        stc=("Bearish / exiting", "STC: funds are taking profits and unwinding long calls. Institutions exiting.")),
    ("ITM CALLS", BID, "HIGH"): E("Aggressive Institutional Volatility Farming or Rapid Exit (STO / STC)", "Check OI",
        "A massive fund is slamming the market maker's bid with heavily overvalued, deep-in-the-money premium.",
        sto=("Bearish pressure", "STO (Sell to Open): the fund is harvesting massive cash premium upfront to establish a highly aggressive synthetic short position or cap a hard resistance ceiling. Market makers are forced to sell the underlying stock to delta-hedge, applying immediate downward pressure."),
        stc=("Bearish / institutional exiting", "STC (Sell to Close): a large fund is urgently unwinding deep ITM calls and taking profits or cutting exposure before an expected high-volatility event (like an earnings report or macro data release) crushes the remaining extrinsic value."),
        src="user"),
    # ---- OTM CALLS ----
    ("OTM CALLS", ASK, "HIGH"): E("Aggressive breakout bet (BTO) or volatility arbitrage", "Bullish / Vol",
        "A directional bet or a multi-leg delta-neutral arbitrage. An institution may buy these calls while shorting the stock in dark pools, a trade that profits from volatility expansion rather than direction. (Long calls)"),
    ("OTM CALLS", ASK, "LOW"): E("Cheap upside positioning (BTO)", "Bullish",
        "OTM calls are bought at the ask while IV is low: upside convexity bought cheaply ahead of a possible move, with no panic premium being paid. (Long calls, moderate bullish)", "added"),
    ("OTM CALLS", MID, "LOW"): E("Efficient position building", "Neutral",
        "Funds are quietly accumulating long positions or scaling out at fair mid-prices. (Neutral)"),
    ("OTM CALLS", MID, "HIGH"): E("Rich-vol position building", "Neutral",
        "OTM calls change hands at the midpoint despite elevated IV: patient trading in an expensive part of the skew, possibly legs of a spread rather than a directional bet. (Neutral)", "added"),
    ("OTM CALLS", BID, "LOW"): E("Institutional selling / distribution (STO / STC)", "Check OI",
        "Calls are sold into the bid.",
        sto=("Capped upside", "STO (farming): a premium wall capping the stock. Short calls."),
        stc=("Bearish / distribution", "STC (closing): institutional distribution. Smart money is quietly walking away from the stock.")),
    ("OTM CALLS", BID, "HIGH"): E("Call premium harvesting into rich IV (STO / STC)", "Check OI",
        "OTM calls are sold into the bid while IV is elevated.",
        sto=("Capped upside", "STO: premium sold into rich IV builds a call wall. Market makers who bought the calls hedge by selling stock, which caps rallies."),
        stc=("Upside exhausted", "STC: holders of OTM calls are selling into elevated IV and taking profits, which suggests the upside move may be finished."),
        src="added"),
    # ---- ITM PUTS ----
    ("ITM PUTS", ASK, "HIGH"): E("Urgent institutional liquidation / crash hedge (BTO)", "Bearish",
        "A fund is desperately buying deep ITM puts or shorting the underlying stock with size. (Long puts, institutional entering)"),
    ("ITM PUTS", ASK, "LOW"): E("Patient bearish positioning (BTO)", "Bearish",
        "Deep ITM puts are lifted at the ask without a volatility premium: a calm, steady build of bearish exposure or a stock-substitute hedge. (Long puts, patient bearish)", "added"),
    ("ITM PUTS", MID, None): E("Structural risk swapping", "Neutral",
        "Funds are cleanly transferring blocks of bearish exposure back and forth privately. (Neutral)"),
    ("ITM PUTS", BID, "LOW"): E("Institutional bottom fishing (STC / STO)", "Check OI",
        "Deep ITM puts are sold into the bid.",
        sto=("Bullish", "STO: puts sold at the bid act as a synthetic long. Market makers who bought them hedge by buying stock, which supports price."),
        stc=("Bullish / bottom near", "STC: confirmed profit-taking exit on old shorts, signaling the institution thinks the bottom is near. (Long puts, institutional exiting)")),
    ("ITM PUTS", BID, "HIGH"): E("Rich-vol put selling or exit (STO / STC)", "Check OI",
        "Deep ITM puts are hit on the bid while IV is elevated.",
        sto=("Bullish", "STO: the fund sells expensive put premium to build a synthetic long. Dealers hedge by buying stock, supporting price."),
        stc=("Bearish unwind", "STC: holders of deep ITM puts are cashing out into elevated IV, removing bearish exposure."),
        src="added"),
    # ---- OTM PUTS ----
    ("OTM PUTS", ASK, "HIGH"): E("Black swan / panic hedging (BTO) or delta-neutral hedge", "Bearish / Hedge",
        "Is this a directional crash bet or part of a complex multi-leg volatility spread? Watch the public tape: if price cracks support on heavy volume after the print, the bearish direction is real. (Long puts)"),
    ("OTM PUTS", ASK, "LOW"): E("Cheap tail protection (BTO)", "Bearish / Hedge",
        "OTM puts are bought at the ask while IV is low: hedgers adding protection cheaply, defensive rather than panicked. (Long puts)", "added"),
    ("OTM PUTS", MID, "LOW"): E("Routine portfolio protection", "Neutral",
        "Major asset managers are placing normal, non-urgent defensive tail-hedges. (Neutral)"),
    ("OTM PUTS", MID, "HIGH"): E("Rich-vol hedging", "Neutral",
        "OTM puts trade at the midpoint while IV is elevated: protection traded patiently at fair value in an expensive part of the skew, often spread legs. (Neutral)", "added"),
    ("OTM PUTS", BID, "LOW"): E("The institutional floor (STO)", "Bullish",
        "Massive institutions are slamming the market maker's bid with short puts to farm premium and buy the bounce. (Short puts)"),
    ("OTM PUTS", BID, "HIGH"): E("Aggressive institutional floor squeezing (STO)", "Bullish",
        "A massive fund is slamming the market maker's bid with high-premium short puts. Dealers are forced to aggressively buy the underlying stock to hedge, locking in a hard intraday support floor. (Short puts, high-conviction bullish reversal)"),
}


def lookup(zone, fill, iv):
    for key in ((zone, fill, iv), (zone, fill, None)):
        if key in PLAYBOOK:
            return PLAYBOOK[key]
    return E("IV level unavailable", "No entry",
             "Not enough valid quotes in this zone to measure the IV slope, so no playbook line can be picked. Widen the strike window or lower the spread filter.", "added")


def classify_fill(last, bid, ask, edge=0.0):
    """edge=0 is your definition (at/above ask, at/below bid, else midpoint). edge=0.25 counts
    the top/bottom 25% of the spread as ask/bid. Crossed, zero or missing quotes are invalid."""
    if not (bid > 0 and ask > 0) or bid > ask or np.isnan(bid) or np.isnan(ask):
        return INVALID
    w = ask - bid
    if last >= ask - edge * w:
        return ASK
    if last <= bid + edge * w:
        return BID
    return MID


def assign_zone(strike, spot, kind):
    if kind == "call":
        return "ITM CALLS" if strike < spot else "OTM CALLS"
    return "ITM PUTS" if strike > spot else "OTM PUTS"


def sla(last, bid, ask, volume, oi):
    """S_la = ((Last - Bid) / (Ask - Bid)) * (Volume / Static OI)."""
    if not (bid > 0) or ask <= bid or oi <= 0:
        return np.nan
    return ((last - bid) / (ask - bid)) * (volume / oi)


def sla_zone(x, vol_gt_oi):
    if np.isnan(x):
        return "n/a (no spread or OI=0)"
    if x <= 0:
        return "Zone 1: STO floor" if vol_gt_oi else "Zone 1*: at/below bid, vol < OI (ambiguous)"
    if 0.01 <= x <= 0.20:
        return "Zone 2: passive STC unwind"
    if x < 0.01:
        return "Between zones (0 to 0.01)"
    return "Zone 3* (my extension): volume > OI, opening-leaning" if vol_gt_oi else "Above Zone 2, volume < OI (ambiguous)"


def prepare(chain, spot, kind, min_volume=1, edge=0.0, require_today=False):
    """chain: DataFrame with strike,bid,ask,lastPrice,volume,openInterest,impliedVolatility."""
    df = chain.copy()
    for c in ["strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["strike", "lastPrice", "volume"])
    df = df[(df["volume"] >= min_volume) & (df["lastPrice"] > 0)].copy()
    df["openInterest"] = df["openInterest"].fillna(0)
    df["zone"] = [assign_zone(s, spot, kind) for s in df["strike"]]
    if "lastTradeDate" in df.columns:
        ts = pd.to_datetime(df["lastTradeDate"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
        df["last_trade_et"] = ts.dt.strftime("%m/%d %H:%M")
        # "today" = newest trading session in this chain (not the calendar day), so overnight,
        # weekend and pre-market views keep showing the last session's positions
        df["traded_today"] = ts.dt.date == ts.dt.date.max()
        if require_today:
            df = df[df["traded_today"]].copy()
    df["fill"] = [classify_fill(l, b, a, edge) for l, b, a in zip(df["lastPrice"], df["bid"], df["ask"])]
    df["premium"] = df["volume"] * df["lastPrice"] * 100
    df["sla"] = [sla(l, b, a, v, o) for l, b, a, v, o in
                 zip(df["lastPrice"], df["bid"], df["ask"], df["volume"], df["openInterest"])]
    df["vol_gt_oi"] = df["volume"] > df["openInterest"]
    df["sla_zone"] = [sla_zone(s, g) for s, g in zip(df["sla"], df["vol_gt_oi"])]
    return df.sort_values("strike").reset_index(drop=True)


def iv_level(full_chain, spot, zone, kind, rise_threshold=0.15, level_threshold=None):
    """HIGH if IV is at a big number (median IV of the zone >= level_threshold, when given) OR
    IV rises moving away from the money across the zone (your 'upward slope'); else LOW.
    Returns (label, slope_change, median_iv). Uses the full chain with junk quotes removed."""
    df = full_chain.copy()
    for c in ["strike", "bid", "ask", "impliedVolatility"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[quoted(df) & (df["impliedVolatility"] > 0.02)]
    df = df[[assign_zone(s, spot, kind) == zone for s in df["strike"]]]
    if len(df) < 4:
        return "N/A", np.nan, np.nan
    df = df.assign(dist=(df["strike"] - spot).abs()).sort_values("dist")
    half = len(df) // 2
    near, far = df["impliedVolatility"].iloc[:half].median(), df["impliedVolatility"].iloc[half:].median()
    change = far / near - 1
    level = df["impliedVolatility"].median()
    high = change >= rise_threshold or (level_threshold is not None and level >= level_threshold)
    return ("HIGH" if high else "LOW"), change, level


def _sla_class(x, vol_gt_oi):
    if np.isnan(x):
        return "amb"
    if x <= 0:
        return "sto" if vol_gt_oi else "amb"          # Zone 1
    if 0.01 <= x <= 0.20:
        return "stc"                                   # Zone 2
    if x < 0.01:
        return "amb"
    return "open" if vol_gt_oi else "amb"             # Zone 3 (extension)


def sto_stc_ruling(d, mids_only=True):
    """Rule a whole zone STO or STC from S_la across its strikes.
    mids_only=True checks midpoint-filled strikes (as you asked); False also includes at/below-bid prints."""
    sub = d[d["fill"] == MID] if mids_only else d[d["fill"].isin([MID, BID])]
    cls = [_sla_class(x, g) for x, g in zip(sub["sla"], sub["vol_gt_oi"])]
    prem = sub["premium"].values
    p = {k: float(sum(pr for c, pr in zip(cls, prem) if c == k)) for k in ("sto", "stc", "open", "amb")}
    sto_side, stc_side = p["sto"] + p["open"], p["stc"]
    if sto_side + stc_side == 0:
        verdict = "UNDETERMINED"
    else:
        verdict = "STO" if sto_side > stc_side else "STC"
    return dict(ruling=verdict, sto_prem=p["sto"], open_prem=p["open"], stc_prem=p["stc"],
                amb_prem=p["amb"], n_checked=len(sub))


def summarize(calls_df, puts_df, calls_full, puts_full, spot, rise_threshold=0.15, mids_only=True, level_threshold=None,
              iv_override=None):
    """iv_override: optional {zone: 'HIGH' or 'LOW'} typed in by the user; it replaces the measured IV label
    for that zone (even when the measurement was N/A) and the playbook line is picked from it."""
    rows = []
    for zone in ZONES:
        kind = "call" if "CALL" in zone else "put"
        d = (calls_df if kind == "call" else puts_df)
        d = d[d["zone"] == zone]
        full = calls_full if kind == "call" else puts_full
        iv, chg, lvl = iv_level(full, spot, zone, kind, rise_threshold, level_threshold)
        iv_measured = iv
        manual = (iv_override or {}).get(zone)
        manual = manual if manual in ("HIGH", "LOW") else None
        if manual:
            iv = manual
        by = d.groupby("fill")["premium"].sum()
        ask, mid, bid = (by.get(ASK, 0.0), by.get(MID, 0.0), by.get(BID, 0.0))
        tot = ask + mid + bid
        row = dict(zone=zone, iv_level=iv, iv_change=chg, iv_abs=lvl, iv_measured=iv_measured, iv_manual=bool(manual), ask=ask, mid=mid, bid=bid, total=tot,
                   dominant="-", dominant_share=0.0, bias="-", title="-", read="No volume in this zone.",
                   src="sheet", ruling="", ruling_bias="", ruling_text="", sto_prem=0.0, open_prem=0.0,
                   stc_prem=0.0, amb_prem=0.0, n_checked=0)
        if tot > 0:
            dom = max([(ask, ASK), (mid, MID), (bid, BID)])[1]
            e = lookup(zone, dom, iv)
            row.update(dominant=dom, dominant_share=by.get(dom, 0.0) / tot, bias=e["bias"],
                       title=e["title"], read=e["text"], src=e["src"])
            if e["sto"]:
                r = sto_stc_ruling(d, mids_only)
                row.update(r)
                if r["ruling"] == "STO":
                    row.update(ruling_bias=e["sto"][0], ruling_text=e["sto"][1])
                elif r["ruling"] == "STC":
                    row.update(ruling_bias=e["stc"][0], ruling_text=e["stc"][1])
        rows.append(row)
    return pd.DataFrame(rows)


# ---------- Own IV (Black-Scholes inversion from the bid/ask midpoint) ----------
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, r, sigma, kind):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if kind == "call" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, r, kind):
    """Bisection. Returns NaN if the price is below intrinsic or outside [1%, 1000%] IV."""
    if price <= 0 or T <= 0:
        return np.nan
    lo, hi = 0.01, 10.0
    if price < bs_price(S, K, T, r, lo, kind) or price > bs_price(S, K, T, r, hi, kind):
        return np.nan
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, r, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def quoted(d):
    """True where a contract has a usable IV: a live two-sided quote, or an IV rebuilt from the last trade."""
    two_sided = (pd.to_numeric(d["bid"], errors="coerce") > 0) & (pd.to_numeric(d["ask"], errors="coerce") > 0)
    return two_sided | d["iv_from_last"] if "iv_from_last" in d.columns else two_sided


def add_own_iv(chain, spot, kind, T_years, r=0.04):
    """Replace impliedVolatility with our own, from the midpoint when bid>0 and ask>0.
    When the market is closed Yahoo zeroes the quotes; then, for strikes that traded in the last
    session, fall back to the LAST TRADE price (flagged iv_from_last). Anything else gives NaN."""
    df = chain.copy()
    bid = pd.to_numeric(df["bid"], errors="coerce")
    ask = pd.to_numeric(df["ask"], errors="coerce")
    mids = (bid + ask) / 2
    ok = (bid > 0) & (ask > 0)
    last = pd.to_numeric(df["lastPrice"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    recent = pd.Series(True, index=df.index)
    if "lastTradeDate" in df.columns:
        ts = pd.to_datetime(df["lastTradeDate"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
        recent = ts.dt.date == ts.dt.date.max()
    fallback = ~ok & (last > 0) & (vol > 0) & recent
    px = mids.where(ok, last)
    df["impliedVolatility"] = [implied_vol(p, spot, k, T_years, r, kind) if (o or fb) else np.nan
                               for p, k, o, fb in zip(px, df["strike"], ok, fallback)]
    df["iv_from_last"] = (fallback & df["impliedVolatility"].notna()).to_numpy()
    return df


def skew_iv(chain, spot, kind, T_years, r=0.04, max_spread=None):
    """IV per strike for the skew CHART only. Uses the bid/ask midpoint when the quote is tight; when the
    quote is missing or wide, uses the last trade price if that strike actually traded in the last session
    (so thin but traded strikes are plotted instead of shown as 0). Untraded strikes with bad quotes stay NaN."""
    df = chain.copy()
    bid = pd.to_numeric(df["bid"], errors="coerce")
    ask = pd.to_numeric(df["ask"], errors="coerce")
    mid = (bid + ask) / 2
    ok = (bid > 0) & (ask > 0)
    wide = pd.Series(False, index=df.index)
    if max_spread is not None:
        wide = ok & (((ask - bid) / mid.replace(0, np.nan)) > max_spread)
    last = pd.to_numeric(df["lastPrice"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    recent = pd.Series(True, index=df.index)
    if "lastTradeDate" in df.columns:
        ts = pd.to_datetime(df["lastTradeDate"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
        recent = ts.dt.date == ts.dt.date.max()
    use_last = (~ok | wide) & (last > 0) & (vol > 0) & recent
    use_mid = ok & ~wide
    px = pd.Series(np.where(use_last, last, np.where(use_mid, mid, np.nan)), index=df.index)
    iv = [implied_vol(p, spot, k, T_years, r, kind) if pd.notna(p) else np.nan for p, k in zip(px, df["strike"])]
    return pd.DataFrame({"strike": pd.to_numeric(df["strike"], errors="coerce"), "impliedVolatility": iv,
                         "from_last": use_last.to_numpy()}).dropna(subset=["impliedVolatility"])


def strike_window(chain, spot, pct, max_spread_pct=None):
    """Keep strikes within +/-pct of spot; optionally drop wide, illiquid quotes."""
    df = chain[(chain["strike"] >= spot * (1 - pct)) & (chain["strike"] <= spot * (1 + pct))].copy()
    if max_spread_pct is not None and ((df["bid"] > 0) & (df["ask"] > 0)).any():
        # skip the spread filter when Yahoo has zeroed the quotes (market closed)
        mid = (df["bid"] + df["ask"]) / 2
        wide = (df["ask"] - df["bid"]) / mid.replace(0, np.nan) > max_spread_pct
        df = df[~wide.fillna(True)]
    return df


# ---------- Premium balance (neutral zones only) ----------
def bs_delta(S, K, T, r, sigma, kind):
    if T <= 0 or sigma <= 0 or np.isnan(sigma):
        return np.nan
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if kind == "call" else _ncdf(d1) - 1


def add_delta(df, spot, kind, T, r=0.04):
    df = df.copy()
    df["delta"] = [bs_delta(spot, k, T, r, iv, kind) for k, iv in zip(df["strike"], df["impliedVolatility"])]
    return df


def _scope_result(label, zones, summary, c_df, p_df, spot):
    s = summary.set_index("zone")
    not_neutral = [f"{z} reads '{s.loc[z, 'bias']}'" for z in zones if s.loc[z, "bias"] != "Neutral"]
    if not_neutral:
        return dict(scope=label, status="skipped", reason="; ".join(not_neutral))
    call_zones = [z for z in zones if "CALL" in z]
    put_zones = [z for z in zones if "PUT" in z]
    cp = float(s.loc[call_zones, "total"].sum())
    pp = float(s.loc[put_zones, "total"].sum())
    diff = cp - pp
    cd = c_df[c_df["zone"].isin(call_zones)].dropna(subset=["delta"])
    pdl = p_df[p_df["zone"].isin(put_zones)].dropna(subset=["delta"])
    call_delta_sh = float((cd["volume"] * 100 * cd["delta"]).sum())
    put_delta_sh = float((pdl["volume"] * 100 * pdl["delta"].abs()).sum())
    ddiff = call_delta_sh - put_delta_sh
    return dict(scope=label, status="compared", call_prem=cp, put_prem=pp, diff=diff,
                action="SHORT" if diff > 0 else "LONG",
                shares_dollar=abs(diff) / spot,
                call_delta_sh=call_delta_sh, put_delta_sh=put_delta_sh,
                delta_action="SHORT" if ddiff > 0 else "LONG", shares_delta=abs(ddiff),
                call_zones=call_zones, put_zones=put_zones)


def _zone_delta_shares(zone, c_df, p_df):
    d = c_df if "CALL" in zone else p_df
    d = d[d["zone"] == zone].dropna(subset=["delta"])
    return float((d["volume"] * 100 * d["delta"].abs()).sum())


def _pair_result(za, zb, summary, c_df, p_df, spot):
    """Compare ANY two zones (call/put or same type). Both must read Neutral. The smaller zone is treated as the
    matched leg, so the excess premium (or delta) of the bigger zone is what is left unmatched, and the offsetting
    share trade is the opposite of that zone's direction: excess calls = long delta -> SHORT, excess puts -> LONG.
    For a call zone vs a put zone this reproduces the original calls-minus-puts result exactly."""
    za, zb = sorted([za, zb], key=ZONES.index)          # calls before puts, ITM before OTM
    s = summary.set_index("zone")
    not_neutral = [f"{z} reads '{s.loc[z, 'bias']}'" for z in (za, zb) if s.loc[z, "bias"] != "Neutral"]
    if not_neutral:
        return dict(scope=f"{za} vs {zb}", status="skipped", reason="; ".join(not_neutral))
    pa, pb = float(s.loc[za, "total"]), float(s.loc[zb, "total"])
    diff = pa - pb
    da, db = _zone_delta_shares(za, c_df, p_df), _zone_delta_shares(zb, c_df, p_df)
    ddiff = da - db
    big_p, big_d = (za if diff > 0 else zb), (za if ddiff > 0 else zb)
    return dict(scope=f"{za} vs {zb}", status="compared", a_zone=za, b_zone=zb,
                call_prem=pa, put_prem=pb, diff=diff,                      # first / second zone premium
                action="SHORT" if "CALL" in big_p else "LONG", shares_dollar=abs(diff) / spot,
                call_delta_sh=da, put_delta_sh=db,                          # first / second zone delta shares
                net_delta=(1.0 if "CALL" in big_d else -1.0) * abs(ddiff),  # signed unmatched delta
                delta_action="SHORT" if "CALL" in big_d else "LONG", shares_delta=abs(ddiff),
                call_zones=[z for z in (za, zb) if "CALL" in z], put_zones=[z for z in (za, zb) if "PUT" in z])


def _share_exchange(zone, c_df, p_df, spot):
    """A Neutral zone with no opposite Neutral options zone: express the midpoint-filled flow in shares."""
    is_call = "CALL" in zone
    d = c_df if is_call else p_df
    d = d[(d["zone"] == zone) & (d["fill"] == MID)].dropna(subset=["delta"])
    prem = float(d["premium"].sum())
    return dict(scope=f"{zone}: option-to-share exchange", status="exchange", zone=zone, prem=prem,
                delta_shares=float((d["volume"] * 100 * d["delta"].abs()).sum()), dollar_shares=prem / spot,
                exposure="long" if is_call else "short", action="SHORT" if is_call else "LONG")


def premium_balance(summary, c_df, p_df, spot, mode="neutral"):
    if mode == "neutral":
        nz = [z for z in ZONES if summary.set_index("zone").loc[z, "bias"] == "Neutral"]
        if len(nz) >= 2:  # any two Neutral zones are compared, calls or puts, every pair
            return [_pair_result(a, b, summary, c_df, p_df, spot) for a, b in itertools.combinations(nz, 2)]
        if nz:  # a lone Neutral zone has nothing to match against: read it as an option-to-share exchange
            return [_share_exchange(z, c_df, p_df, spot) for z in nz]
        return [dict(scope="Neutral zones: none", status="skipped", reason="no zone reads Neutral")]
    """mode='whole': compare calls vs puts only if ALL four zones read Neutral.
    mode='pairs': compare ITM calls vs ITM puts and OTM calls vs OTM puts, each only if both read Neutral."""
    if mode == "whole":
        return [_scope_result("Whole smile (all calls vs all puts)", ZONES, summary, c_df, p_df, spot)]
    return [_scope_result("ITM calls vs ITM puts", ["ITM CALLS", "ITM PUTS"], summary, c_df, p_df, spot),
            _scope_result("OTM calls vs OTM puts", ["OTM CALLS", "OTM PUTS"], summary, c_df, p_df, spot)]


def time_to_expiry(exp):
    """Years from now (or from 4:00 PM ET today, if after the close) to 4:00 PM ET on exp."""
    ny = ZoneInfo("America/New_York")
    exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(hour=16, tzinfo=ny)
    now = datetime.now(ny)
    asof = min(now, now.replace(hour=16, minute=0, second=0, microsecond=0))
    return max((exp_dt - asof).total_seconds(), 60) / (365 * 24 * 3600)


def analyze_chain(calls, puts, spot, exp, min_vol=1, rise=0.15, own_iv=True, rate=0.04,
                  level=None, win=0.10, max_spread=0.5, edge=0.0, only_today=True, mids_only=True, iv_override=None):
    """Full pipeline for one ticker/expiration. Used by the detail view and the scan-all button."""
    T = time_to_expiry(exp)
    if own_iv:
        calls = add_own_iv(calls, spot, "call", T, rate)
        puts = add_own_iv(puts, spot, "put", T, rate)
    calls = strike_window(calls, spot, win, max_spread)
    puts = strike_window(puts, spot, win, max_spread)
    c_df = add_delta(prepare(calls, spot, "call", min_vol, edge, only_today), spot, "call", T, rate)
    p_df = add_delta(prepare(puts, spot, "put", min_vol, edge, only_today), spot, "put", T, rate)
    summary = summarize(c_df, p_df, calls, puts, spot, rise, mids_only, level, iv_override)
    return dict(calls=calls, puts=puts, c_df=c_df, p_df=p_df, summary=summary, T=T)


# ---------- Signals per contract, walls, skew and expected-move stats ----------
SIGNAL = {"sto": "STO", "stc": "STC", "open": "Opening", "amb": "Unclear"}


def signal(x, vol_gt_oi):
    """S_la class as a plain label: STO, STC, Opening (Zone 3 extension) or Unclear."""
    return SIGNAL[_sla_class(x, vol_gt_oi)]


def contract_rows(c_df, p_df, exp_label):
    """One row per valid contract: expiration, type, strike, volume, premium, fill, signal."""
    a = pd.concat([c_df.assign(type="Call"), p_df.assign(type="Put")], ignore_index=True)
    a = a[a["fill"] != INVALID].copy()
    a["signal"] = [signal(x, g) for x, g in zip(a["sla"], a["vol_gt_oi"])]
    a["exp"] = exp_label
    return a[["exp", "type", "strike", "volume", "premium", "fill", "signal"]]


def walls(calls_raw, puts_raw, spot, win):
    """Call wall = call strike at/above spot with the most open interest; put wall = put strike
    at/below spot with the most open interest. Looks only inside +/-win of spot."""
    def pick(df, above):
        d = df[["strike", "openInterest"]].apply(pd.to_numeric, errors="coerce").dropna()
        d = d[(d["strike"] >= spot * (1 - win)) & (d["strike"] <= spot * (1 + win))]
        d = d[d["strike"] >= spot] if above else d[d["strike"] <= spot]
        d = d[d["openInterest"] > 0]
        if d.empty:
            return None
        r = d.loc[d["openInterest"].idxmax()]
        return dict(strike=float(r["strike"]), oi=float(r["openInterest"]))
    return dict(call=pick(calls_raw, True), put=pick(puts_raw, False))


def _iv_at_delta(df, spot, kind, T, r, target=0.25):
    """IV at a given absolute delta, interpolated across the OTM side only (cleaner quotes)."""
    d = df.copy()
    for c in ("strike", "bid", "ask", "impliedVolatility"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[quoted(d) & (d["impliedVolatility"] > 0.02)]
    d = d[d["strike"] >= spot] if kind == "call" else d[d["strike"] <= spot]
    d = add_delta(d, spot, kind, T, r).dropna(subset=["delta"])
    d = d.assign(ad=d["delta"].abs()).sort_values("ad")
    if len(d) < 3 or not (d["ad"].min() <= target <= d["ad"].max()):
        return np.nan
    return float(np.interp(target, d["ad"], d["impliedVolatility"]))


def skew_stats(calls_raw, puts_raw, calls, puts, spot, T, r=0.04):
    """Put/call ratios, 25-delta risk reversal and straddle-implied move for one expiration.
    calls_raw/puts_raw = the whole Yahoo chain; calls/puts = after IV, window and spread filters."""
    def tot(df, col):
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())
    cv, pv = tot(calls_raw, "volume"), tot(puts_raw, "volume")
    co, po = tot(calls_raw, "openInterest"), tot(puts_raw, "openInterest")
    c25 = _iv_at_delta(calls, spot, "call", T, r)
    p25 = _iv_at_delta(puts, spot, "put", T, r)
    out = dict(pc_vol=pv / cv if cv > 0 else np.nan, pc_oi=po / co if co > 0 else np.nan,
               iv_c25=c25, iv_p25=p25, rr25=c25 - p25,
               atm_strike=np.nan, straddle=np.nan, straddle_pct=np.nan,
               atm_iv=np.nan, one_sd=np.nan, one_sd_pct=np.nan)

    def quotes(df):
        d = df[["strike", "bid", "ask"]].apply(pd.to_numeric, errors="coerce")
        d = d[(d["bid"] > 0) & (d["ask"] > 0) & (d["bid"] <= d["ask"])].copy()
        d["mid"] = (d["bid"] + d["ask"]) / 2
        return d[["strike", "mid"]]

    m = quotes(calls_raw).merge(quotes(puts_raw), on="strike", suffixes=("_c", "_p"))
    if not m.empty:
        row = m.iloc[int((m["strike"] - spot).abs().to_numpy().argmin())]
        k = float(row["strike"])
        if abs(k - spot) / spot <= 0.05:
            st_ = float(row["mid_c"] + row["mid_p"])
            ivs = [v for v in (implied_vol(float(row["mid_c"]), spot, k, T, r, "call"),
                               implied_vol(float(row["mid_p"]), spot, k, T, r, "put")) if not np.isnan(v)]
            out.update(atm_strike=k, straddle=st_, straddle_pct=st_ / spot)
            if ivs:
                iv = float(np.mean(ivs))
                sd = spot * iv * math.sqrt(T)
                out.update(atm_iv=iv, one_sd=sd, one_sd_pct=sd / spot)
    return out


# ---------- Overnight gap check: Net Portfolio Delta = Initial Delta + Gamma x (gap) ----------
def bs_gamma(S, K, T, r, sigma):
    """Black-Scholes gamma per share: change in delta for a $1 move in the stock."""
    if T <= 0 or S <= 0 or K <= 0 or sigma <= 0 or np.isnan(sigma):
        return np.nan
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (S * sigma * math.sqrt(T))


def gamma_gap(res, c_df, p_df, spot, T, rate, gap_pct):
    """Gamma of the compared call/put flow (in shares per $1) and what a gap does to a delta-hedged book.
    res = one 'compared' result from premium_balance. The sign of gamma (long or short the options)
    cannot be read from Yahoo data, so callers show both cases."""
    cd = c_df[c_df["zone"].isin(res["call_zones"])]
    pdf = p_df[p_df["zone"].isin(res["put_zones"])]

    def shares_gamma(d):
        g = np.array([bs_gamma(spot, k, T, rate, iv) for k, iv in zip(d["strike"], d["impliedVolatility"])], dtype=float)
        return float(np.nansum(d["volume"].to_numpy(dtype=float) * 100 * g)) if len(d) else 0.0

    gc, gp = shares_gamma(cd), shares_gamma(pdf)
    by = pd.concat([cd, pdf]).groupby("fill")["premium"].sum()
    return dict(gamma_c=gc, gamma_p=gp, G=gc + gp,
                D0=res.get("net_delta", res["call_delta_sh"] - res["put_delta_sh"]),
                dS=spot * gap_pct / 100.0,
                lev=res["shares_delta"] / res["shares_dollar"] if res["shares_dollar"] > 0 else np.nan,
                ask=float(by.get(ASK, 0.0)), mid=float(by.get(MID, 0.0)), bid=float(by.get(BID, 0.0)))


def gamma_side(res, c_df, p_df, lean=0.6):
    """Which side of gamma the compared flow most likely sits on today.
    BTO (long gamma)  = contracts filled at/above the ask (your sheet's BTO definition).
    STO (short gamma) = contracts whose S_la lands in Zone 1 (at/below bid with volume > OI).
    Midpoint, STC and unclear prints do not vote. Premium-weighted; `lean` = share needed to call a side."""
    cd = c_df[c_df["zone"].isin(res["call_zones"])]
    pdf = p_df[p_df["zone"].isin(res["put_zones"])]
    d = pd.concat([cd, pdf], ignore_index=True)
    d = d[d["fill"] != INVALID]
    is_long = (d["fill"] == ASK).to_numpy()
    is_short = np.array([signal(x, g) == "STO" for x, g in zip(d["sla"], d["vol_gt_oi"])], dtype=bool)
    long_p = float(d.loc[is_long, "premium"].sum())
    short_p = float(d.loc[is_short, "premium"].sum())
    total, flow = long_p + short_p, float(d["premium"].sum())
    share = long_p / total if total > 0 else np.nan
    if total <= 0:
        side = "UNDETERMINED"
    elif share >= lean:
        side = "LONG"
    elif share <= 1 - lean:
        side = "SHORT"
    else:
        side = "MIXED"
    return dict(side=side, long_prem=long_p, short_prem=short_p, long_share=share, flow_prem=flow,
                coverage=total / flow if flow > 0 else np.nan, n_long=int(is_long.sum()), n_short=int(is_short.sum()))


# ---------- 3D gamma surface (gamma exposure by spot and expiration) ----------
def _zero_cross(x, y, near):
    """x where y changes sign, linearly interpolated; the crossing closest to `near`, else NaN."""
    s = np.sign(y)
    idx = np.where(s[:-1] * s[1:] < 0)[0]
    if idx.size == 0:
        return np.nan
    xs = x[idx] - y[idx] * (x[idx + 1] - x[idx]) / (y[idx + 1] - y[idx])
    return float(xs[np.argmin(np.abs(xs - near))])


def gamma_surface(chains, spot, rate=0.04, weight="openInterest", span=0.12, n=61, own_iv=False, min_days=1.0):
    """Net dealer gamma exposure ($ per 1% move in the stock) at hypothetical spots, one row per expiration.
    chains = [(exp_label, dte_days, T_years, calls_df, puts_df), ...]
    GEX(S) = sum over contracts of  sign x weight x 100 x gamma(S) x S^2 x 1%,  calls +, puts - (the usual
    assumption that customers are long calls / short puts, so dealers hold the opposite). weight = 'openInterest'
    or 'volume'. Strikes with no usable IV get the interpolated smile IV of their expiration; T is floored at
    `min_days` so a same-day expiry does not blow the surface up."""
    S = np.linspace(spot * (1 - span), spot * (1 + span), n)
    Z, dtes, labels = [], [], []
    for exp, dte, T, calls, puts in chains:
        T_eff = max(float(T), min_days / 365.0)
        z, used = np.zeros(n), False
        for kind, df, sgn in (("call", calls, 1.0), ("put", puts, -1.0)):
            d = add_own_iv(df, spot, kind, T, rate) if own_iv else df
            K = pd.to_numeric(d["strike"], errors="coerce").to_numpy(float)
            iv = pd.to_numeric(d["impliedVolatility"], errors="coerce").to_numpy(float)
            w = pd.to_numeric(d[weight], errors="coerce").fillna(0).to_numpy(float)
            fin = np.isfinite(K)
            K, iv, w = K[fin], iv[fin], w[fin]
            good = np.isfinite(iv) & (iv > 0.03) & (iv < 4.0)
            if good.sum() < 3:
                continue
            o = np.argsort(K[good])
            iv = np.interp(K, K[good][o], iv[good][o])      # strikes without a usable IV take the smile value
            keep = w > 0
            if not keep.any():
                continue
            K, iv, w = K[keep], iv[keep], w[keep]
            sr = iv * np.sqrt(T_eff)
            d1 = (np.log(S[:, None] / K[None, :]) + (rate + 0.5 * iv ** 2) * T_eff) / sr
            gam = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi) / (S[:, None] * sr)
            z += sgn * (gam * w[None, :] * 100.0).sum(axis=1) * S ** 2 * 0.01
            used = True
        if used:
            Z.append(z); dtes.append(dte); labels.append(exp)
    if not Z:
        return None
    Z = np.array(Z)
    total = Z.sum(axis=0)
    return dict(S=S, dte=np.array(dtes, float), labels=labels, Z=Z, total=total,
                flip_total=_zero_cross(S, total, spot), flip_each=[_zero_cross(S, z, spot) for z in Z],
                at_spot=float(np.interp(spot, S, total)))
