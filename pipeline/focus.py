"""高信心族群與隔日預測卡（只用校準檔 calibration.json 已驗證的關係）。

- sector_confidence(cal)：每個類股的信心等級（同日＋隔日／僅同日／未驗證）
- predictive_stats(...)：校準時計算「陸期 D 日 → 台股下一交易日」的歷史命中率（全期與樣本外）
- sensitive_stocks(cal, ...)：各類股依實測敏感度排序的個股
- predict_cards(...)：組出今天的隔日預測卡
"""
import bisect
from datetime import date

T_VALID = 2.0     # 同日顯著
T_LAG1 = 2.5      # 隔日顯著（較嚴格：多重比較）
Z_MIN = 0.5       # 命中率只計算訊號夠明確的日子（|z| ≥ 0.5）
LEVELS = {"A": "同日＋隔日", "B": "僅同日"}


def _all_pairs(cal):
    return list(cal.get("pairs") or []) + list((cal.get("intl") or {}).get("pairs") or []) + list(cal.get("discovered") or [])


def is_predictive(p):
    """隔日關係成立：t1 達門檻、前後兩段方向一致，且與同日方向相同（避免只是反轉雜訊）"""
    t0, t1 = p.get("t0") or 0, p.get("t1") or 0
    return (p.get("ex") != "國際" and abs(t1) >= T_LAG1 and p.get("stable1")
            and abs(t0) >= T_VALID and (t0 > 0) == (t1 > 0))


def sector_confidence(cal):
    """{類股: {level, label, links:[...], best_t0, best_t1}}；只列有實測有效關聯的類股"""
    out = {}
    for p in _all_pairs(cal):
        valid = p.get("status") == "有效"
        pred = is_predictive(p)
        if not (valid or pred):
            continue
        s = out.setdefault(p["sector"], {"links": [], "level": "B"})
        s["links"].append({"code": p["code"], "name": p["name"], "t0": p.get("t0"), "t1": p.get("t1"),
                           "status": p.get("status"), "predictive": bool(pred), "intl": p.get("ex") == "國際"})
        if pred:
            s["level"] = "A"
    for s in out.values():
        s["links"].sort(key=lambda x: -abs(x["t0"] or 0))
        # 「數據新增」只有隔日關係、同日無手動對照：不足以單獨撐起信心等級
        if not any(x["status"] == "有效" for x in s["links"]):
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


def hit_stats(ret, sig, target, tw_dates, sign, d0=None, z_min=Z_MIN):
    """陸期 D 日（|z|≥z_min）的方向 × sign，對台股下一交易日 target 的命中率與平均報酬（預測方向）"""
    n = hit = 0
    tot = 0.0
    for d, r in ret.items():
        if d0 and d < d0:
            continue
        sd = sig.get(d)
        if not sd or abs(r / sd) < z_min:
            continue
        t = _next_tw(tw_dates, d)
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
               "sign": sign, "all": hit_stats(cmd_ret[code], sigmas[code], target, tw_dates, sign),
               "test": hit_stats(cmd_ret[code], sigmas[code], target, tw_dates, sign, d0=split), "stocks": []}
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
    """隔日預測卡。
    日盤版（收盤後）：用今天陸期日盤收盤 → 預測台股下一交易日（tw_next）。
    盤前版：用前一個陸期交易日的收盤 → 預測台股今天；另附夜盤目前漲跌（夜盤未經回測，只供參考）。"""
    stats = (cal.get("focus") or {}).get("predictive") or []
    if not stats:
        return []
    by_code = {c["code"]: c for c in board}
    cards = {}
    for p in stats:
        rows = klines.get(p["code"]) or []
        rows = [r for r in rows if r[2] is not None and (r[0] <= run_date if session == "day" else r[0] < run_date)]
        if not rows:
            continue
        d, _close, ret, z = rows[-1][:4]
        c = by_code.get(p["code"]) or {}
        card = cards.setdefault(p["sector"], {"sector": p["sector"], "signal_date": d, "target": tw_next,
                                              "session": session, "items": [], "stocks": {}})
        dirn = (1 if ret > 0 else -1) * p["sign"] if ret else 0
        card["items"].append({
            "code": p["code"], "name": p["name"], "status": p["status"], "ret": ret, "z": z,
            "strong": z is not None and abs(z) >= Z_MIN, "dir": dirn, "t1": p["t1"],
            "hit": p["all"]["hit"], "n": p["all"]["n"], "hit_test": p["test"]["hit"], "n_test": p["test"]["n"],
            "avg": p["all"]["avg"],
            "night": c.get("chg") if session == "night" and c.get("status") == "ok" else None})
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
    for card in cards.values():
        # 只有「有效」（已計入分數）的關係決定方向；數據新增的關係列出供參考
        main = [x for x in card["items"] if x["status"] == "有效" and x["strong"]]
        vote = sum(x["dir"] * abs(x["z"] or 0) for x in main)
        card["direction"] = 1 if vote > 0 else -1 if vote < 0 else 0
        card["items"].sort(key=lambda x: (x["status"] != "有效", -abs(x["t1"])))
        card["stocks"] = sorted(card["stocks"].values(), key=lambda x: -x["t1"])
        out.append(card)
    return out
