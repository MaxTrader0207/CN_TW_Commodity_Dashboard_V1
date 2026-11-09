"""高信心族群與隔日預測卡（只用校準檔 calibration.json 已驗證的關係）。

- sector_confidence(cal)：每個類股的信心等級（同日＋隔日／僅同日／未驗證）
- predictive_stats(...)：校準時計算「陸期 D 日 → 台股下一交易日」的歷史命中率（全期與樣本外）
- sensitive_stocks(cal, ...)：各類股依實測敏感度排序的個股
- predict_cards(...)：組出今天的隔日預測卡（陸期收盤 → 台股下一交易日；國際隔夜 → 台股當天）
"""
import bisect
from datetime import date

T_VALID = 2.0     # 同日顯著
T_LAG1 = 2.5      # 隔日顯著（較嚴格：多重比較）
Z_MIN = 0.5       # 命中率只計算訊號夠明確的日子（|z| ≥ 0.5）
LEVELS = {"A": "同日＋隔日", "B": "僅同日"}
TIERS = [(2.0, "強"), (1.0, "中"), (Z_MIN, "弱")]     # 訊號強度分級（|z| 門檻）


def tier_of(z):
    if z is None:
        return None
    return next((name for th, name in TIERS if abs(z) >= th), None)


def _all_pairs(cal):
    return list(cal.get("pairs") or []) + list((cal.get("intl") or {}).get("pairs") or []) + list(cal.get("discovered") or [])


def is_predictive(p):
    """預測關係成立。
    陸期：隔日 t1 達門檻、前後兩段方向一致，且與同日方向相同（避免只是反轉雜訊）。
    國際：校準時已把美國 D 日對齊到下一個台股交易日，ρ0／t0 本身就是預測力（盤前已知）。"""
    if p.get("fdr_ok") is False:
        return False
    t0, t1 = p.get("t0") or 0, p.get("t1") or 0
    if p.get("ex") == "國際":
        return p.get("status") == "有效" and abs(t0) >= T_LAG1 and bool(p.get("stable"))
    return (abs(t1) >= T_LAG1 and p.get("stable1")
            and abs(t0) >= T_VALID and (t0 > 0) == (t1 > 0))


def sector_confidence(cal):
    """{類股: {level, label, links:[...], best_t0, best_t1}}；只列有實測有效關聯的類股"""
    out = {}
    for p in _all_pairs(cal):
        valid = p.get("status") == "有效" and p.get("fdr_ok") is not False   # 多重比較校正未過的不算
        pred = is_predictive(p)
        if not (valid or pred):
            continue
        s = out.setdefault(p["sector"], {"links": [], "level": "B"})
        s["links"].append({"code": p["code"], "name": p["name"], "t0": p.get("t0"), "t1": p.get("t1"),
                           "status": p.get("status"), "predictive": bool(pred), "intl": p.get("ex") == "國際",
                           "decay": p.get("decay")})
        if pred:
            s["level"] = "A"
    for s in out.values():
        s["links"].sort(key=lambda x: -abs(x["t0"] or 0))
        # 「數據新增」只有隔日關係、同日無手動對照：不足以單獨撐起信心等級
        if not any(x["status"] == "有效" for x in s["links"]) and not any(x["intl"] and x["predictive"] for x in s["links"]):
            s["level"] = None
        s["label"] = LEVELS.get(s["level"])
        s["best_t0"] = max((abs(x["t0"] or 0) for x in s["links"]), default=0)
        s["best_t1"] = max((abs(x["t1"] or 0) for x in s["links"] if not x["intl"]), default=0)
    return {k: v for k, v in out.items() if v["level"]}


def _next_tw(tw_dates, d):
    i = bisect.bisect_right(tw_dates, d)
    if i >= len(tw_dates):
        return None
    t = tw_dates[i]
    return t if (date.fromisoformat(t) - date.fromisoformat(d)).days <= 5 else None


def hit_stats(ret, sig, target, tw_dates, sign, d0=None, z_min=Z_MIN, shifted=False, z_max=None):
    """陸期 D 日（|z|≥z_min）的方向 × sign，對台股下一交易日 target 的命中率與平均報酬（預測方向）。
    shifted=True：ret 已對齊到要預測的台股日（國際隔夜），直接比對同一天。"""
    n = hit = 0
    tot = 0.0
    for d, r in ret.items():
        if d0 and d < d0:
            continue
        sd = sig.get(d)
        if not sd or abs(r / sd) < z_min or (z_max and abs(r / sd) >= z_max):
            continue
        t = d if shifted else _next_tw(tw_dates, d)
        y = target.get(t) if t else None
        if y is None or y == 0:
            continue
        dirn = (1 if r > 0 else -1) * sign
        n += 1
        hit += (y > 0) == (dirn > 0)
        tot += y * dirn
    return {"n": n, "hit": round(hit / n, 3) if n else None, "avg": round(tot / n, 3) if n else None}


def predictive_stats(cal_pairs, cal_stocks, cmd_ret, sigmas, excess, stock_res, split):
    """校準時呼叫：每個隔日關係（陸期 → 類股）與其實測個股的歷史命中率"""
    tw_dates = sorted(excess)
    out = []
    for p in cal_pairs:
        if not is_predictive(p) or p["code"] not in cmd_ret:
            continue
        code, sector = p["code"], p["sector"]
        sign = 1 if p["t1"] > 0 else -1
        target = {d: row[sector] for d, row in excess.items() if sector in row}
        row = {"code": code, "name": p["name"], "sector": sector, "t1": p["t1"], "status": p["status"],
               "kind": "cn", "sign": sign, "all": hit_stats(cmd_ret[code], sigmas[code], target, tw_dates, sign),
               "test": hit_stats(cmd_ret[code], sigmas[code], target, tw_dates, sign, d0=split),
               "tiers": tier_stats(cmd_ret[code], sigmas[code], target, tw_dates, sign), "stocks": []}
        st_dates = sorted({d for v in stock_res.values() for d in v})
        for s in cal_stocks.get(code, []):
            res = stock_res.get(s["code"])
            if not res or (s.get("t1") or 0) * sign < T_VALID:
                continue
            row["stocks"].append({"code": s["code"], "why": s["why"], "t1": s["t1"],
                                  "next_ok": (s.get("oos") or {}).get("next_ok"),
                                  "all": hit_stats(cmd_ret[code], sigmas[code], res, st_dates, sign),
                                  "test": hit_stats(cmd_ret[code], sigmas[code], res, st_dates, sign, d0=split)})
        row["stocks"].sort(key=lambda x: -(x["t1"] * sign))
        out.append(row)
    out.sort(key=lambda r: (r["sector"], -abs(r["t1"])))
    return out


def tier_stats(ret, sig, target, tw_dates, sign, shifted=False):
    """各強度區間（弱 0.5~1、中 1~2、強 ≥2）各自的命中率與平均報酬"""
    bands = {"弱": (Z_MIN, 1.0), "中": (1.0, 2.0), "強": (2.0, None)}
    return {k: hit_stats(ret, sig, target, tw_dates, sign, z_min=a, z_max=b, shifted=shifted) for k, (a, b) in bands.items()}


def predictive_stats_intl(intl_pairs, intl_series, sigmas, excess, stock_res, cal_stocks, intl_cn, split):
    """國際隔夜（已對齊到台股日）→ 台股當天：命中率、強度分級、對應個股"""
    tw_dates = sorted(excess)
    st_dates = sorted({d for v in stock_res.values() for d in v})
    out = []
    for p in intl_pairs:
        code, sector = p["code"], p["sector"]
        if not is_predictive(p) or code not in intl_series or code not in sigmas:
            continue
        sign = 1 if p["t0"] > 0 else -1
        target = {d: row[sector] for d, row in excess.items() if sector in row}
        ret, sg = intl_series[code], sigmas[code]
        row = {"code": code, "name": p["name"], "sector": sector, "t1": p["t0"], "status": p["status"], "kind": "intl",
               "sign": sign, "all": hit_stats(ret, sg, target, tw_dates, sign, shifted=True),
               "test": hit_stats(ret, sg, target, tw_dates, sign, d0=split, shifted=True),
               "tiers": tier_stats(ret, sg, target, tw_dates, sign, shifted=True), "stocks": []}
        seen = set()
        for cn in intl_cn.get(code, []):
            for s in cal_stocks.get(cn, []):
                res = stock_res.get(s["code"])
                if not res or s["code"] in seen or s.get("verdict") != "一致" or (s.get("t0") or 0) * sign < T_VALID:
                    continue
                seen.add(s["code"])
                a = hit_stats(ret, sg, res, st_dates, sign, shifted=True)
                row["stocks"].append({"code": s["code"], "why": s["why"], "t1": s["t0"], "next_ok": None,
                                      "all": a, "test": hit_stats(ret, sg, res, st_dates, sign, d0=split, shifted=True)})
        out.append(row)
    return out

def sensitive_stocks(cal, sector, stocks_today, industry, n=6):
    """類股內依實測敏感度排序的個股：只取校準過、屬於此類股（或對照表指定）且同日 t 值顯著者。
    分數 = |t0|（同日）＋ 隔日 t 值（同向時）；樣本外成立者加權。"""
    conf_codes = {}
    links_by = (cal.get("live_links") or {})
    items = {code for code, ls in links_by.items() for l in ls if l["sector"] == sector}
    for item in items:
        for s in (cal.get("stocks") or {}).get(item, []):
            t0, t1 = s.get("t0") or 0, s.get("t1") or 0
            if s.get("verdict") not in ("一致", "隔日一致") or abs(t0) < T_VALID and abs(t1) < T_VALID:
                continue
            sec = industry.get(s["code"])
            if sec and sec != sector:
                continue
            oos = s.get("oos") or {}
            k = abs(t0) + (abs(t1) if (t0 > 0) == (t1 > 0) else 0)
            k *= 1.3 if oos.get("same_ok") else 1.0
            k *= 1.2 if oos.get("next_ok") else 1.0
            prev = conf_codes.get(s["code"])
            if prev and prev["k"] >= k:
                continue
            st = stocks_today.get(s["code"]) or {}
            conf_codes[s["code"]] = {
                "code": s["code"], "name": st.get("name"), "chg_pct": st.get("chg_pct"), "lots": st.get("lots"),
                "item": item, "role": s.get("role"), "why": s.get("why"),
                "t0": t0, "t1": t1, "rho0": s.get("rho0"), "oos_same": oos.get("same_ok"), "oos_next": oos.get("next_ok"),
                "k": round(k, 2)}
    return sorted(conf_codes.values(), key=lambda x: -x["k"])[:n]


def predict_cards(cal, klines, board, session, run_date, tw_next, stocks_today):
    """預測卡。
    陸期（kind=cn）：收盤版用今天陸期日盤收盤 → 台股下一交易日；盤前版用前一個陸期交易日收盤 → 台股今天，
                    另附夜盤目前漲跌（夜盤未經回測，只供參考）。
    國際（kind=intl）：只在盤前版，用最後一根已收盤的美國日線 → 台股今天。"""
    stats = (cal.get("focus") or {}).get("predictive") or []
    if not stats:
        return []
    by_code = {c["code"]: c for c in board}
    cards = {}
    for p in stats:
        kind = p.get("kind", "cn")
        c = by_code.get(p["code"]) or {}
        if kind == "intl":
            if session != "night" or c.get("chg_settled") is None:
                continue
            d, ret, z = c.get("settled_date"), c.get("chg_settled"), c.get("z")
        else:
            rows = klines.get(p["code"]) or []
            rows = [r for r in rows if r[2] is not None and (r[0] <= run_date if session == "day" else r[0] < run_date)]
            if not rows:
                continue
            d, _close, ret, z = rows[-1][:4]
        card = cards.setdefault((p["sector"], kind), {"sector": p["sector"], "kind": kind, "signal_date": d,
                                                      "target": tw_next, "session": session, "items": [], "stocks": {}})
        dirn = (1 if ret > 0 else -1) * p["sign"] if ret else 0
        tier = tier_of(z)
        ts = (p.get("tiers") or {}).get(tier) or {}
        card["items"].append({
            "code": p["code"], "name": p["name"], "status": p["status"], "ret": ret, "z": z,
            "strong": tier is not None, "tier": tier, "dir": dirn, "t1": p["t1"],
            "hit": p["all"]["hit"], "n": p["all"]["n"], "hit_test": p["test"]["hit"], "n_test": p["test"]["n"],
            "avg": p["all"]["avg"], "tier_hit": ts.get("hit"), "tier_n": ts.get("n"), "tier_avg": ts.get("avg"),
            "night": c.get("chg") if kind == "cn" and session == "night" and c.get("status") == "ok" else None})
        for s in p["stocks"]:
            st = stocks_today.get(s["code"]) or {}
            old = card["stocks"].get(s["code"])
            if old and old["t1"] >= s["t1"] * p["sign"]:
                continue
            card["stocks"][s["code"]] = {"code": s["code"], "name": st.get("name"), "why": s["why"], "via": p["name"],
                                         "t1": round(s["t1"] * p["sign"], 2), "next_ok": s["next_ok"],
                                         "dir": dirn if p["status"] == "有效" else 0,
                                         "hit": s["all"]["hit"], "n": s["all"]["n"], "hit_test": s["test"]["hit"],
                                         "last_chg": st.get("chg_pct")}
    out = []
    rank = {None: 0, "弱": 1, "中": 2, "強": 3}
    for card in cards.values():
        # 只有「有效」（已計入分數）的關係決定方向；數據新增的關係列出供參考
        main = [x for x in card["items"] if x["status"] == "有效" and x["strong"]]
        vote = sum(x["dir"] * abs(x["z"] or 0) for x in main)
        card["direction"] = 1 if vote > 0 else -1 if vote < 0 else 0
        agree = [x for x in main if x["dir"] == card["direction"]]
        card["tier"] = max((x["tier"] for x in agree), key=lambda t: rank[t], default=None) if card["direction"] else None
        card["items"].sort(key=lambda x: (x["status"] != "有效", -abs(x["t1"])))
        card["stocks"] = sorted(card["stocks"].values(), key=lambda x: -x["t1"])
        out.append(card)
    out.sort(key=lambda c: (c["kind"] != "intl", c["sector"]))
    return out


# ── 依類股可信度調整分數（第 4 項）：倍數只用訓練期的信心等級決定，回測過關才採用 ──
FACTOR_VARIANTS = {
    "conf_only": {"A": 1.0, "B": 1.0, None: 0.0},     # 只讓有實測支持的類股進排名
    "discount": {"A": 1.0, "B": 1.0, None: 0.5},      # 未驗證類股分數打五折
    "pred_boost": {"A": 1.5, "B": 1.0, None: 0.5},    # 可預測族群加權、未驗證打折
}


def sector_factor(conf, variant):
    m = FACTOR_VARIANTS[variant]
    f = {name: m[c["level"]] for name, c in conf.items()}
    f["_default"] = m[None]
    return f
