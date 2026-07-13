#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC gap scanner: Deribit options vs Polymarket.

Сравнивает risk-neutral вероятности из опционов Deribit с ценами
BTC-рынков Polymarket и показывает гэпы за вычетом taker-комиссии.

Типы рынков Polymarket:
  - touch    ("Will Bitcoin hit/reach $150k by <date>?") — барьерная вероятность
               P(max/min цены коснётся страйка до дедлайна). Резолюция обычно
               по 1-мин свече Binance BTC/USDT (high/low).
  - terminal ("Bitcoin above/below $X on <date>?") — терминальная вероятность
               P(S_T > K) = N(d2) из IV-поверхности Deribit.

Использование:
  python3 btc_gap_scanner.py                          # живой режим
  python3 btc_gap_scanner.py --min-edge 0.04 --bankroll 10000
  python3 btc_gap_scanner.py --save-cache cache/      # сохранить сырые данные
  python3 btc_gap_scanner.py --deribit-cache cache/deribit.json \
                             --poly-cache cache/poly.json      # оффлайн

Только stdlib. Не финансовый совет: модель упрощена (r=0, непрерывный
мониторинг барьера, lognormal), у Polymarket и Deribit разные
reference-цены и время экспирации — это базисный риск.
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

GAMMA = "https://gamma-api.polymarket.com"
DERIBIT = "https://www.deribit.com/api/v2"

DAY = 86400.0
YEAR_DAYS = 365.0

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ----------------------------- utils ---------------------------------------

def now_utc():
    override = os.environ.get("SCANNER_NOW")  # для тестов: 2026-07-13T00:00:00Z
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-gap-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


# ----------------------------- Deribit -------------------------------------

INSTR_RE = re.compile(r"^BTC-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$")


def parse_instrument(name):
    m = INSTR_RE.match(name)
    if not m:
        return None
    d, mon, yy, strike, cp = m.groups()
    if mon not in MONTHS:
        return None
    expiry = datetime(2000 + int(yy), MONTHS[mon], int(d), 8, 0,
                      tzinfo=timezone.utc)  # экспирация Deribit 08:00 UTC
    return expiry, float(strike), cp


def fetch_deribit(cache_path=None, save_path=None):
    if cache_path:
        with open(cache_path) as f:
            data = json.load(f)
    else:
        url = (DERIBIT + "/public/get_book_summary_by_currency"
               "?currency=BTC&kind=option")
        data = http_get_json(url)
        if save_path:
            with open(save_path, "w") as f:
                json.dump(data, f)
    return data.get("result", data if isinstance(data, list) else [])


def fetch_deribit_spot(cache_entries):
    try:
        data = http_get_json(DERIBIT + "/public/get_index_price?index_name=btc_usd")
        return data["result"]["index_price"]
    except Exception:
        return None  # оффлайн: возьмём underlying ближайшей экспирации


def build_surface(entries, now):
    """{expiry: {"F": forward, "smile": {K: iv}}}, iv в долях (0.40 = 40%)."""
    by_exp = {}
    for e in entries:
        parsed = parse_instrument(e.get("instrument_name", ""))
        if not parsed:
            continue
        expiry, strike, cp = parsed
        if expiry <= now:
            continue
        iv = e.get("mark_iv")
        und = e.get("underlying_price")
        if not iv or iv <= 0 or not und:
            continue
        d = by_exp.setdefault(expiry, {"unds": [], "smile_c": {}, "smile_p": {}})
        d["unds"].append(und)
        d["smile_c" if cp == "C" else "smile_p"][strike] = iv / 100.0

    surface = {}
    for expiry, d in by_exp.items():
        F = median(d["unds"])
        smile = {}
        strikes = set(d["smile_c"]) | set(d["smile_p"])
        for K in strikes:
            # предпочитаем OTM-сторону: call для K>=F, put для K<F
            if K >= F:
                iv = d["smile_c"].get(K, d["smile_p"].get(K))
            else:
                iv = d["smile_p"].get(K, d["smile_c"].get(K))
            if iv:
                smile[K] = iv
        if len(smile) >= 3:
            surface[expiry] = {"F": F, "smile": smile}
    return dict(sorted(surface.items()))


def iv_at_strike(smile, K):
    """Линейная интерполяция IV по ln(K), плоская экстраполяция за краями."""
    ks = sorted(smile)
    if K <= ks[0]:
        return smile[ks[0]], K < ks[0]
    if K >= ks[-1]:
        return smile[ks[-1]], K > ks[-1]
    for a, b in zip(ks, ks[1:]):
        if a <= K <= b:
            t = (math.log(K) - math.log(a)) / (math.log(b) - math.log(a))
            return smile[a] * (1 - t) + smile[b] * t, False
    return smile[ks[-1]], True


def model_probs(surface, spot, K, deadline, now):
    """Вероятности из поверхности Deribit на горизонт deadline.

    Возвращает dict: p_above (терминальная P(S_T>K)), p_touch_up, p_touch_down,
    sigma, F, warns[].
    """
    warns = []
    Tp = max((deadline - now).total_seconds() / DAY / YEAR_DAYS, 1e-4)
    exps = list(surface.items())
    Ts = [max((e - now).total_seconds() / DAY / YEAR_DAYS, 1e-6) for e, _ in exps]

    def w_and_extrap(i):
        iv, ex = iv_at_strike(exps[i][1]["smile"], K)
        return iv * iv * Ts[i], ex

    if Tp <= Ts[0]:
        w1, ex = w_and_extrap(0)
        w = w1 * Tp / Ts[0]  # плоская вола до первой экспирации
        F = spot * (exps[0][1]["F"] / spot) ** (Tp / Ts[0])
        if ex:
            warns.append("strike вне сетки Deribit (экстраполяция IV)")
    elif Tp >= Ts[-1]:
        wn, ex = w_and_extrap(len(Ts) - 1)
        w = wn * Tp / Ts[-1]
        F = exps[-1][1]["F"]
        warns.append("дедлайн дальше последней экспирации Deribit (экстраполяция)")
        if ex:
            warns.append("strike вне сетки Deribit (экстраполяция IV)")
    else:
        i = max(j for j in range(len(Ts)) if Ts[j] <= Tp)
        wa, exa = w_and_extrap(i)
        wb, exb = w_and_extrap(i + 1)
        t = (Tp - Ts[i]) / (Ts[i + 1] - Ts[i])
        w = wa * (1 - t) + wb * t  # линейная total variance по T
        lF = (math.log(exps[i][1]["F"]) * (1 - t)
              + math.log(exps[i + 1][1]["F"]) * t)
        F = math.exp(lF)
        if exa or exb:
            warns.append("strike вне сетки Deribit (экстраполяция IV)")
    if w <= 0:
        return None
    sw = math.sqrt(w)
    sigma = math.sqrt(w / Tp)

    # терминальная вероятность
    d2 = (math.log(F / K) - 0.5 * w) / sw
    p_above = norm_cdf(d2)

    # барьерная (first passage) для GBM, r=0: drift лога mu = -sigma^2/2
    S0 = spot
    mu = -0.5 * sigma * sigma
    b = math.log(K / S0)
    if abs(b) < 1e-9:
        p_up = p_dn = 1.0
    else:
        arg = 2.0 * mu * b / (sigma * sigma)
        arg = max(min(arg, 700), -700)
        if b > 0:   # верхний барьер
            p_up = (norm_cdf((-b + mu * Tp) / sw)
                    + math.exp(arg) * norm_cdf((-b - mu * Tp) / sw))
            p_dn = 1.0
        else:       # нижний барьер
            p_dn = (norm_cdf((b - mu * Tp) / sw)
                    + math.exp(arg) * norm_cdf((b + mu * Tp) / sw))
            p_up = 1.0
    return {"p_above": p_above,
            "p_touch_up": min(max(p_up, 0.0), 1.0),
            "p_touch_down": min(max(p_dn, 0.0), 1.0),
            "sigma": sigma, "F": F, "warns": warns}


# ----------------------------- Polymarket ----------------------------------

BTC_RE = re.compile(r"\b(bitcoin|btc)\b", re.I)
STRIKE_RE = re.compile(r"\$\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmM]?)")


def parse_strike(text):
    m = STRIKE_RE.search(text)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    suf = m.group(2).lower()
    if suf == "k":
        v *= 1e3
    elif suf == "m":
        v *= 1e6
    return v if v >= 1000 else None  # отсечь мусор типа "$5"


def classify(question):
    q = question.lower()
    if "up or down" in q or "between" in q:
        return None, None  # range/updown-рынки не поддерживаются
    if any(w in q for w in ("dip to", "drop to", "fall to", "dip below")):
        return "touch", "down"
    if any(w in q for w in ("hit", "reach", "touch")):
        return "touch", "up"
    if "above" in q or "close above" in q or "finish above" in q:
        return "terminal", "up"
    if "below" in q or "close below" in q or "finish below" in q:
        return "terminal", "down"
    return None, None


def poly_fee_rate(m):
    """Пиковая ставка taker-fee из feeSchedule (fee = rate*p*(1-p))."""
    if not m.get("feesEnabled", False):
        return 0.0
    fs = m.get("feeSchedule")
    if isinstance(fs, str):
        try:
            fs = json.loads(fs)
        except Exception:
            fs = None
    if isinstance(fs, dict) and "rate" in fs:
        return float(fs["rate"])
    return 0.07  # дефолт для crypto-рынков


def fetch_polymarket(cache_path=None, save_path=None, max_pages=4):
    if cache_path:
        with open(cache_path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("markets", [])
    out, limit = [], 500
    for page in range(max_pages):
        params = urllib.parse.urlencode({
            "closed": "false", "active": "true", "limit": limit,
            "offset": page * limit, "order": "volume24hr", "ascending": "false"})
        batch = http_get_json(GAMMA + "/markets?" + params)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < limit:
            break
    if save_path:
        with open(save_path, "w") as f:
            json.dump(out, f)
    return out


def extract_btc_markets(raw, now, min_liquidity, min_hours):
    """Фильтрует BTC-рынки со страйком и дедлайном."""
    res, skipped = [], 0
    for m in raw:
        q = m.get("question", "") or ""
        if not BTC_RE.search(q):
            continue
        if not m.get("endDate"):
            continue
        kind, direction = classify(q)
        strike = parse_strike(q)
        if not kind or not strike:
            skipped += 1
            continue
        deadline = parse_iso(m["endDate"])
        if deadline <= now + timedelta(hours=min_hours):
            continue
        liq = float(m.get("liquidityNum") or 0)
        if liq < min_liquidity:
            continue
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            p_yes_mid = float(prices[0])
        except Exception:
            continue
        res.append({
            "question": q, "slug": m.get("slug", ""),
            "kind": kind, "direction": direction, "strike": strike,
            "deadline": deadline, "p_mid": p_yes_mid,
            "best_bid": m.get("bestBid"), "best_ask": m.get("bestAsk"),
            "liquidity": liq, "vol24": float(m.get("volume24hr") or 0),
            "fee_rate": poly_fee_rate(m),
        })
    return res, skipped


# ----------------------------- edges ---------------------------------------

def taker_fee(p, rate):
    return rate * p * (1.0 - p)


def kelly_quarter(p_model, cost):
    """Четверть-Келли для покупки бинарного исхода по цене cost."""
    if cost <= 0 or cost >= 1 or p_model <= cost:
        return 0.0
    f = (p_model - cost) / (1.0 - cost)
    return 0.25 * f


def evaluate(pm, probs, bankroll):
    if pm["kind"] == "terminal":
        p_model = probs["p_above"] if pm["direction"] == "up" else 1 - probs["p_above"]
    else:
        p_model = (probs["p_touch_up"] if pm["direction"] == "up"
                   else probs["p_touch_down"])

    bid, ask, rate = pm["best_bid"], pm["best_ask"], pm["fee_rate"]
    rows = []
    if ask is not None and 0 < ask < 1:
        cost = ask + taker_fee(ask, rate)
        rows.append(("BUY YES", p_model - cost, cost, p_model))
    if bid is not None and 0 < bid < 1:
        cost = (1 - bid) + taker_fee(1 - bid, rate)
        rows.append(("BUY NO", (1 - p_model) - cost, cost, 1 - p_model))
    best = max(rows, key=lambda r: r[1]) if rows else None
    stake = 0.0
    if best and best[1] > 0:
        stake = bankroll * kelly_quarter(best[3], best[2])
        stake = round(min(stake, 0.20 * bankroll), 0)  # cap 20% банкролла
    return p_model, best, stake


# ----------------------------- main ----------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Deribit vs Polymarket BTC gap scanner")
    ap.add_argument("--min-edge", type=float, default=0.04,
                    help="порог чистого edge для сигнала (default 0.04)")
    ap.add_argument("--min-liquidity", type=float, default=1000,
                    help="мин. ликвидность рынка Polymarket, $ (default 1000)")
    ap.add_argument("--min-hours", type=float, default=6,
                    help="мин. часов до дедлайна (default 6)")
    ap.add_argument("--bankroll", type=float, default=10000)
    ap.add_argument("--deribit-cache", help="файл с сохранённым ответом Deribit")
    ap.add_argument("--poly-cache", help="файл с сохранённым ответом Polymarket")
    ap.add_argument("--save-cache", help="папка для сохранения сырых данных")
    ap.add_argument("--json", dest="json_out", help="сохранить результат в JSON")
    ap.add_argument("--history", dest="history_out",
                    help="дописать компактную строку прогона в JSONL-файл")
    args = ap.parse_args()

    now = now_utc()
    sd = sp = None
    if args.save_cache:
        os.makedirs(args.save_cache, exist_ok=True)
        sd = os.path.join(args.save_cache, "deribit.json")
        sp = os.path.join(args.save_cache, "poly.json")

    entries = fetch_deribit(args.deribit_cache, sd)
    surface = build_surface(entries, now)
    if not surface:
        sys.exit("Нет пригодных опционов Deribit (проверьте данные).")

    spot = None if args.deribit_cache else fetch_deribit_spot(entries)
    if spot is None:
        spot = min(surface.items())[1]["F"]  # underlying ближайшей экспирации

    raw = fetch_polymarket(args.poly_cache, sp)
    markets, skipped = extract_btc_markets(raw, now, args.min_liquidity,
                                           args.min_hours)

    print(f"Время: {now:%Y-%m-%d %H:%M UTC} | BTC spot(index): {spot:,.0f}")
    print(f"Deribit: {len(surface)} экспираций "
          f"({min(surface):%d%b%y}..{max(surface):%d%b%y}); "
          f"Polymarket: {len(markets)} BTC-рынков (пропущено без страйка: {skipped})")
    print("-" * 118)
    hdr = (f"{'рынок':<44} {'тип':<9} {'strike':>8} {'дней':>5} "
           f"{'P_poly':>7} {'P_der':>7} {'gap':>7} {'edge':>7} {'сторона':<8} "
           f"{'~$':>6} {'ликв.$':>9}")
    print(hdr)
    print("-" * 118)

    results = []
    for pm in sorted(markets, key=lambda x: x["deadline"]):
        probs = model_probs(surface, spot, pm["strike"], pm["deadline"], now)
        if not probs:
            continue
        p_model, best, stake = evaluate(pm, probs, args.bankroll)
        gap = p_model - pm["p_mid"]
        edge = best[1] if best else float("nan")
        side = best[0] if best else "-"
        days = (pm["deadline"] - now).total_seconds() / DAY
        flag = " <<< СИГНАЛ" if best and edge >= args.min_edge else ""
        warn = (" [!" + "; ".join(probs["warns"]) + "]") if probs["warns"] else ""
        print(f"{pm['question'][:44]:<44} {pm['kind'] + '/' + pm['direction']:<9} "
              f"{pm['strike']:>8,.0f} {days:>5.1f} {pm['p_mid']:>7.3f} "
              f"{p_model:>7.3f} {gap:>+7.3f} {edge:>+7.3f} {side:<8} "
              f"{stake:>6,.0f} {pm['liquidity']:>9,.0f}{flag}{warn}")
        results.append({**{k: (v.isoformat() if isinstance(v, datetime) else v)
                           for k, v in pm.items()},
                        "p_model": p_model, "gap": gap, "edge_net": edge,
                        "side": side, "stake_quarter_kelly": stake,
                        "sigma": probs["sigma"], "warns": probs["warns"]})

    print("-" * 118)
    print("P_der — вероятность из IV-поверхности Deribit (терминальная N(d2) или "
          "барьерная для touch-рынков).")
    print("edge — за вычетом taker-fee Polymarket (rate*p*(1-p)); лимитные ордера "
          "(maker) комиссией не облагаются.")
    print("~$ — четверть-Келли от банкролла. Помните о базисном риске: у площадок "
          "разные reference-цены и время резолюции. Не финансовый совет.")

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w") as f:
            json.dump({"now": now.isoformat(), "spot": spot,
                       "min_edge": args.min_edge, "results": results},
                      f, ensure_ascii=False, indent=1)
        print(f"JSON: {args.json_out}")

    if args.history_out:
        os.makedirs(os.path.dirname(args.history_out) or ".", exist_ok=True)
        line = {"t": now.isoformat(), "spot": spot,
                "markets": [{"slug": r["slug"], "q": r["question"][:60],
                             "kind": r["kind"], "dir": r["direction"],
                             "strike": r["strike"],
                             "deadline": r["deadline"],
                             "p_mid": round(r["p_mid"], 4),
                             "p_model": round(r["p_model"], 4),
                             "edge": (None if r["edge_net"] != r["edge_net"]
                                      else round(r["edge_net"], 4)),
                             "side": r["side"],
                             "liq": round(r["liquidity"], 0)}
                            for r in results]}
        with open(args.history_out, "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"History: {args.history_out}")


if __name__ == "__main__":
    main()
