"""單元測試：解析器、標準化、無偷看、對照表完整性。  python -m pytest -q tests"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import build as B  # noqa: E402
import calibrate as C  # noqa: E402
import history as H  # noqa: E402
from mapping import BENEFICIARIES, COMMODITIES, INDUSTRY_CODE, INTL, SPREADS  # noqa: E402

TPE = timezone(timedelta(hours=8))
SECTORS = set(INDUSTRY_CODE.values())


# ───────── 對照表 ─────────
def test_commodity_codes_unique():
    codes = [c[1] for c in COMMODITIES] + [s[0] for s in SPREADS]
    assert len(codes) == len(set(codes))


def test_links_use_known_sectors():
    for ex, code, name, limit, links in COMMODITIES:
        assert limit > 0, code
        for s, w, why in links:
            assert s in SECTORS, f"{code} → {s}"
            assert -1.5 <= w <= 1.5 and why
    for code, name, legs, links in SPREADS:
        for s, *_ in links:
            assert s in SECTORS, f"{code} → {s}"
    for code, sym, name, cn, links in INTL:
        for s, *_ in links:
            assert s in SECTORS, f"{code} → {s}"


def test_references_exist():
    known = {c[1] for c in COMMODITIES}
    for code, name, legs, links in SPREADS:
        for leg, coef in legs:
            assert leg in known, f"價差 {code} 的腿 {leg} 不在商品表"
    for code, sym, name, cn, links in INTL:
        assert set(cn) <= known, f"國際 {code} 對應 {cn}"
    for item, lst in BENEFICIARIES.items():
        assert item in known | {s[0] for s in SPREADS}, item
        for stock, role, why in lst:
            assert stock.isdigit() and role in ("產品", "成本")


# ───────── 解析器 ─────────
def test_parse_sina():
    f = ["乙二醇", "150000", "4400", "4500", "4380", "4410", "4480", "4481", "4490", "4470", "4400",
         "0", "0", "120000", "80000", "", "", "2026-10-29"]
    out = B.parse_sina(f'var hq_str_nf_EG0="{",".join(f)}";\n')
    q = out["EG"]
    assert q["base"] == 4400 and q["last"] == 4490
    assert q["chg"] == pytest.approx(2.05, abs=0.01)
    assert q["date"] == "2026-10-29" and q["oi"] == 120000


def test_parse_sina_skips_bad_rows():
    assert B.parse_sina('var hq_str_nf_XX0="";') == {}


def test_parse_shfe_params_takes_mode():
    j = {"ContractDailyTradeArgument": [
        {"INSTRUMENTID": "sc2611", "UPPER_VALUE": "0.2"}, {"INSTRUMENTID": "sc2612", "UPPER_VALUE": "0.16"},
        {"INSTRUMENTID": "sc2701", "UPPER_VALUE": "0.16"}, {"INSTRUMENTID": "cu2611", "UPPER_VALUE": "0.09"}]}
    assert H.parse_shfe_params(j) == {"SC": 16.0, "CU": 9.0}


def test_parse_zce_params():
    txt = ("合约代码|当日结算价|是否单边市|连续单边市天数|交易保证金率(%)|涨跌停板(%)|x\n"
           "AP611    |6,932.00   |N |0 |10 |±8 |5.00\nCF611 |14,955.00 |N |0 |10 |±6 |4.30\n")
    assert H.parse_zce_params(txt) == {"AP": 8.0, "CF": 6.0}


def test_parse_holidays_excludes_trading_days():
    rows = [{"Name": "中秋節", "Date": "1151006"}, {"Name": "市場無交易，僅辦理結算交割作業", "Date": "1150212"},
            {"Name": "開始交易", "Date": "1150102"}]
    h = H.parse_holidays(rows)
    assert "2026-10-06" in h and "2026-01-02" not in h


def test_parse_isin():
    html = ("<table><tr><td>6488　環球晶</td><td>TW0006488000</td><td>2015/09/25</td><td>上櫃</td>"
            "<td>半導體業</td><td>ESVUFR</td><td></td></tr></table>")
    assert B.parse_isin(html) == {"6488": "半導體"}


# ───────── 交易日 ─────────
def test_expected_trade_date():
    hol = {"2026-10-09": "國慶日補假"}
    fri_morning = datetime(2026, 10, 9, 10, 0, tzinfo=TPE)          # 休市日 → 前一個交易日
    assert B.expected_trade_date(fri_morning, hol) == "2026-10-08"
    mon_after = datetime(2026, 10, 12, 15, 0, tzinfo=TPE)            # 收盤後 → 當天
    assert B.expected_trade_date(mon_after, hol) == "2026-10-12"
    mon_before = datetime(2026, 10, 12, 9, 0, tzinfo=TPE)            # 盤中 → 上一個交易日（跳過休市與週末）
    assert B.expected_trade_date(mon_before, hol) == "2026-10-08"


# ───────── 標準化與報酬 ─────────
def test_robust_sigma_ignores_outlier():
    xs = [(-1) ** i * (0.2 + 0.1 * (i % 7)) for i in range(20)]
    assert H.robust_sigma(xs + [50]) == pytest.approx(H.robust_sigma(xs), rel=0.25)
    assert H.robust_sigma(xs + [50]) < 1, "單一極端值不能把波動度撐大"
    assert H.robust_sigma([1, 2]) is None


def test_rolling_sigma_no_lookahead():
    ret = {(date(2026, 1, 1) + timedelta(days=i)).isoformat(): (-1) ** i * (1 + i % 3) for i in range(40)}
    d = sorted(ret)[30]
    a = H.rolling_sigma(ret)[d]
    ret2 = dict(ret, **{d: 999.0})
    assert H.rolling_sigma(ret2)[d] == a, "當天的漲跌不能影響當天的波動度"


def test_z_signal():
    assert H.z_signal(1.0, 1.0) == pytest.approx(0.5)
    assert H.z_signal(10.0, 1.0) == 1.0
    assert H.z_signal(-7.0, 1.0, limit=7, bonus=True) == pytest.approx(-1.5)


def test_pairs_returns_anomaly_cap():
    out, dropped = H.pairs_returns({"2026-01-02": (110, 100), "2026-01-03": (160, 100), "2026-01-04": (90, 0)}, 7)
    assert out == {"2026-01-02": pytest.approx(10.0)} and dropped == 1


def test_span_alignment_after_china_holiday():
    """大陸長假：陸期 (p, d] 要對到台股同一段期間的累計超額報酬"""
    tw = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-08", "2026-10-09"]
    sret = {d: 1.0 for d in tw}
    assert C.span_sum(sret, tw, "2026-09-30", "2026-10-09") == pytest.approx(5.0)


def test_build_fixtures_end_to_end(tmp_path):
    """離線假資料完整跑一次 build：輸出結構正確、多空類股不重複"""
    import json
    import subprocess
    fx = tmp_path / "fx"
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_fixtures.py"), str(fx)], check=True, capture_output=True)
    import shutil
    work = tmp_path / "repo"
    for d in ("pipeline", "docs"):
        shutil.copytree(ROOT / d, work / d, ignore=shutil.ignore_patterns("__pycache__", "history"))
    r = subprocess.run([sys.executable, str(work / "pipeline" / "build.py"), "--fixtures", str(fx), "--session", "day"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    p = json.loads((work / "docs" / "data" / "latest.json").read_text(encoding="utf-8"))
    assert p["demo"] is True and len(p["commodities"]) >= len(COMMODITIES)
    assert len(p["bull"]) <= 3 and len(p["bear"]) <= 3 and not set(p["bull"]) & set(p["bear"])
    assert all("light" in s and "strong" not in s for s in p["sectors"]), "每個類股都有狀態燈欄位，不再輸出當日漲跌排行"
    assert "chips" not in p and "focus" in p and "lights" in p["focus"]
    st = json.loads((work / "docs" / "data" / "status.json").read_text(encoding="utf-8"))
    assert st["ok"] is True


def test_update_limits_rejects_bad_values_and_falls_back(tmp_path, monkeypatch):
    """交易所漲跌停：超出 1~30% 的數字不採用；鄭商所 https 不通時改用 http"""
    monkeypatch.setattr(H, "LIMITS_JSON", tmp_path / "limits.json")

    class R:
        def __init__(self, code, text=None, j=None):
            self.status_code, self.text, self._j, self.encoding = code, text, j, None

        def json(self):
            return self._j

    class S:
        def get(self, url, timeout=0):
            if "shfe" in url:
                return R(200, j={"ContractDailyTradeArgument": [
                    {"INSTRUMENTID": "cu2611", "UPPER_VALUE": "0.09"}, {"INSTRUMENTID": "al2611", "UPPER_VALUE": "0.95"}]})
            if "ine.cn" in url:
                return R(404)
            if url.startswith("https://www.czce"):
                raise ConnectionError("tls")
            return R(200, text="h\nAP611 |1|N|0|10|±8|5\n")

    cache, st = H.update_limits(S(), date(2026, 10, 30), {"CU", "AL", "AP"}, log=lambda *a: None)
    assert cache["CU"]["limit"] == 9.0 and "AL" not in cache and cache["AP"]["limit"] == 8.0
    assert "AL=95.0" in st["上期所"] and st["鄭商所"].startswith("ok")


def _wd():
    import importlib.util
    import os
    os.environ.setdefault("GITHUB_REPOSITORY", "x/y")
    sys.path.insert(0, str(ROOT / ".github" / "scripts"))
    spec = importlib.util.spec_from_file_location("watchdog", ROOT / ".github" / "scripts" / "watchdog.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_watchdog_weekend_is_not_stale_until_monday_slot_missed():
    W = _wd()
    fri = datetime(2026, 10, 30, 17, 15, tzinfo=TPE)
    assert not W.check(fri, datetime(2026, 11, 2, 7, 0, tzinfo=TPE), {})[0]     # 週一 07:00：盤前版還在緩衝內
    stale, age, missed = W.check(fri, datetime(2026, 11, 2, 8, 30, tzinfo=TPE), {})
    assert stale and age > 60 and [n for _, n in missed] == ["盤前版"]


def test_watchdog_weekday_threshold_30h():
    W = _wd()
    tue = datetime(2026, 11, 3, 17, 15, tzinfo=TPE)
    assert not W.check(tue, datetime(2026, 11, 4, 23, 0, tzinfo=TPE), {})[0]   # 29.75 小時：未達門檻
    stale, _, missed = W.check(tue, datetime(2026, 11, 4, 23, 30, tzinfo=TPE), {})
    assert stale and len(missed) == 2


def test_watchdog_holiday_skipped():
    W = _wd()
    hol = {"2026-11-02": "假日", "2026-11-03": "假日"}
    fri = datetime(2026, 10, 30, 17, 15, tzinfo=TPE)
    assert not W.check(fri, datetime(2026, 11, 4, 6, 0, tzinfo=TPE), hol)[0]   # 連假中沒有應跑的排程
    assert W.check(fri, datetime(2026, 11, 4, 9, 0, tzinfo=TPE), hol)[0]       # 連假後第一天盤前版沒跑


# ───────── 高信心族群／隔日預測 ─────────
def _cal_focus():
    return {"pairs": [
        {"ex": "上期所", "code": "CU", "name": "銅", "sector": "電器電纜", "t0": 6.3, "t1": 4.8, "stable1": True, "status": "有效"},
        {"ex": "上期所", "code": "RB", "name": "螺紋鋼", "sector": "鋼鐵", "t0": 3.9, "t1": 1.0, "stable1": True, "status": "有效"},
        {"ex": "大商所", "code": "M", "name": "豆粕", "sector": "食品", "t0": 1.2, "t1": 0.3, "stable1": False, "status": "不顯著"},
        {"ex": "上期所", "code": "X", "name": "反轉", "sector": "鋼鐵", "t0": 3.0, "t1": -3.0, "stable1": True, "status": "有效"}],
        "discovered": [{"ex": "上期所", "code": "SN", "name": "錫", "sector": "電器電纜", "t0": 6.6, "t1": 3.6, "stable1": True, "status": "數據新增"}]}


def test_sector_confidence_levels():
    import focus as F
    conf = F.sector_confidence(_cal_focus())
    assert conf["電器電纜"]["level"] == "A" and conf["鋼鐵"]["level"] == "B" and "食品" not in conf
    assert not any(x["predictive"] for x in conf["鋼鐵"]["links"]), "隔日方向與同日相反的不算預測力"


def test_hit_stats_counts_only_clear_signals():
    import focus as F
    ds = [(date(2026, 1, 5) + timedelta(days=i)).isoformat() for i in range(6)]
    ret = {ds[0]: 2.0, ds[1]: -2.0, ds[2]: 0.1, ds[3]: 2.0}
    sig = {d: 1.0 for d in ds}
    target = {ds[1]: 1.0, ds[2]: 1.0, ds[3]: -1.0, ds[4]: -1.0}   # 下一交易日
    st = F.hit_stats(ret, sig, target, ds, 1)
    assert st["n"] == 3 and st["hit"] == pytest.approx(1 / 3, abs=0.001)   # ds[2] 的 |z|<0.5 不計


def test_predict_cards_direction_uses_valid_links_only():
    import focus as F
    cal = {"focus": {"predictive": [
        {"code": "CU", "name": "銅", "sector": "電器電纜", "t1": 4.8, "status": "有效", "sign": 1,
         "all": {"n": 100, "hit": 0.55, "avg": 0.3}, "test": {"n": 40, "hit": 0.56, "avg": 0.2}, "stocks": []},
        {"code": "SN", "name": "錫", "sector": "電器電纜", "t1": 3.6, "status": "數據新增", "sign": 1,
         "all": {"n": 100, "hit": 0.52, "avg": 0.2}, "test": {"n": 40, "hit": 0.5, "avg": 0.1}, "stocks": []}]}}
    kl = {"CU": [["2026-11-05", 1, 0.2, 0.3], ["2026-11-06", 1, -1.9, -3.2], ["2026-11-09", 1, 0.5, 0.8]],
          "SN": [["2026-11-06", 1, 2.0, 1.5]]}
    night = F.predict_cards(cal, kl, [], "night", "2026-11-09", "2026-11-09", {})
    assert night[0]["signal_date"] == "2026-11-06" and night[0]["direction"] == -1, "盤前版用前一個陸期交易日；錫只觀察"
    day = F.predict_cards(cal, kl, [], "day", "2026-11-09", "2026-11-10", {})
    assert day[0]["signal_date"] == "2026-11-09" and day[0]["direction"] == 1
    # 收盤版：日K 還沒有今天這根時，改用今天的收盤報價
    kl2 = {"CU": kl["CU"][:2], "SN": kl["SN"]}
    board = [{"code": "CU", "status": "ok", "quote_date": "2026-11-09", "chg": 0.9, "z": 1.2}]
    day2 = F.predict_cards(cal, kl2, board, "day", "2026-11-09", "2026-11-10", {})
    assert day2[0]["signal_date"] == "2026-11-09" and day2[0]["items"][0]["tier"] == "中"


def test_breakdown_levels_and_cards():
    import track as T
    sig = {"bull": [{"name": "電器電纜", "score": 10}], "bear": [{"name": "食品", "score": -5}],
           "conf": {"電器電纜": "A", "食品": None}, "focus_bull": [{"name": "電器電纜", "score": 10}], "focus_bear": [],
           "cards": [{"sector": "電器電纜", "direction": -1}]}
    b = T.sector_breakdown([(sig, {"next_date": "2026-11-10"})], {"2026-11-10": {"電器電纜": 1.0, "食品": 0.5}}, {})
    assert b["by_level"]["A"]["avg"] == 1.0 and b["by_level"]["none"]["avg"] == -0.5
    assert b["cards"]["avg"] == -1.0 and b["backfilled"] == 0


def test_build_skip_day_if_done(tmp_path):
    """收盤版備援排程：今天已有 15:00 後的收盤版就略過"""
    import json
    import subprocess
    import shutil
    work = tmp_path / "repo"
    for d in ("pipeline", "docs"):
        shutil.copytree(ROOT / d, work / d, ignore=shutil.ignore_patterns("__pycache__"))
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    h = work / "docs" / "data" / "history"
    h.mkdir(parents=True, exist_ok=True)
    (h / f"{today}_day.json").write_text(json.dumps({"generated_at": f"{today}T17:10+08:00"}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(work / "pipeline" / "build.py"), "--session", "day", "--skip-if-done",
                        "--fixtures", str(tmp_path / "nofx")], capture_output=True, text=True)
    assert r.returncode == 0 and "略過" in r.stdout, r.stdout[-800:] + r.stderr[-800:]


def test_intl_card_premarket_only_and_tier():
    import focus as F
    cal = {"focus": {"predictive": [
        {"code": "BZ", "name": "Brent", "sector": "油電燃氣", "t1": 6.5, "status": "有效", "sign": 1, "kind": "intl",
         "all": {"n": 100, "hit": 0.55, "avg": 0.6}, "test": {"n": 40, "hit": 0.58, "avg": 1.2},
         "tiers": {"強": {"n": 50, "hit": 0.62, "avg": 2.1}}, "stocks": []}]}}
    board = [{"code": "BZ", "kind": "intl", "settled_date": "2026-11-06", "chg_settled": -3.0, "z": -2.4}]
    assert F.predict_cards(cal, {}, board, "day", "2026-11-09", "2026-11-10", {}) == []
    c = F.predict_cards(cal, {}, board, "night", "2026-11-09", "2026-11-09", {})[0]
    assert c["direction"] == -1 and c["tier"] == "強" and c["items"][0]["tier_hit"] == 0.62


def test_bh_pass_and_fdr_excludes_confidence():
    import focus as F
    ok = C.bh_pass([6.0, 2.05, 0.1, 0.2, 0.3, 0.1, 0.4, 0.2, 0.1, 0.3])
    assert 0 in ok and 1 not in ok, "t=2.05 在 10 組檢定中不應通過 BH"
    cal = _cal_focus()
    cal["pairs"][1]["fdr_ok"] = False                  # 螺紋鋼→鋼鐵 未通過校正
    assert "鋼鐵" in F.sector_confidence(cal)            # 仍有「反轉」那組有效關聯撐著
    cal["pairs"][3]["fdr_ok"] = False
    assert "鋼鐵" not in F.sector_confidence(cal)


def test_watchdog_token_days_left():
    W = _wd()
    assert W.token_days_left(date(2027, 9, 29), "2027-10-29\n") == 30
    assert W.token_days_left(date(2027, 11, 1), "2027-10-29") == -3
    assert W.token_days_left(date(2027, 1, 1), "壞掉") is None
    assert (ROOT / ".github" / "token-expiry.txt").read_text().strip() == "2027-10-29"


def test_decay_links_downweights_reversed_relation():
    ds = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(150)]
    import random
    rnd = random.Random(1)
    x = {d: rnd.gauss(0, 1) for d in ds}
    ex = {d: {"A": -x[d] + rnd.gauss(0, 0.3), "B": x[d] + rnd.gauss(0, 0.3)} for d in ds}
    live = {"X": [{"sector": "A", "w": 0.5}, {"sector": "B", "w": 0.5}]}
    out, hit = C.decay_links(live, {"X": x}, ex, ds, ds[-1], 0.0)
    assert hit == [("X", "A")] and out["X"][0]["w"] == 0.0 and out["X"][1]["w"] == 0.5


# ───────── 2026-11 改版：只處理有實測支持的項目、狀態燈、實盤停損、迴歸式 ─────────
def test_verified_codes_and_spread_legs():
    import focus as F
    cal = {"pairs": [
        {"code": "CU", "sector": "電器電纜", "status": "有效", "fdr_ok": True},
        {"code": "M", "sector": "食品", "status": "不顯著", "fdr_ok": False},
        {"code": "PVC", "sector": "塑膠", "status": "有效", "fdr_ok": False},          # 多重比較沒過 → 不算
        {"code": "SPR_PR", "sector": "紡織纖維", "status": "有效", "fdr_ok": True},
        {"code": "NEW", "sector": "鋼鐵", "status": "樣本不足"}],
        "intl": {"pairs": [{"code": "BZ", "sector": "油電燃氣", "status": "有效", "fdr_ok": True},
                           {"code": "GC", "sector": "金融保險", "status": "不顯著"}]},
        "focus": {"predictive": [{"code": "SN", "kind": "cn"}, {"code": "BZ", "kind": "intl"}]}}
    v = F.verified_codes(cal, [("SPR_PR", "瓶片加工費", [("PR", 1), ("TA", -0.855), ("EG", -0.335)], [])])
    assert v["cn"] == {"CU", "SPR_PR", "NEW"} and v["intl"] == {"BZ"}
    assert v["need"] == {"CU", "NEW", "PR", "TA", "EG", "SN"}, "價差的腿與預測卡的觀察商品也要抓日K"
    assert F.verified_codes({}) is None, "沒有校準檔時全部都處理"


def test_apply_lights_uses_own_history_scale():
    import focus as F
    cal = {"lights": {"z": 1.0, "z_strong": 2.0,
                      "day": {"sd": {"油電燃氣": 100.0, "水泥": 25.0}, "stats": {"水泥": {"lag0": {"n": 40, "hit": 0.4, "avg": -0.1}}}},
                      "pre": {"sd": {"油電燃氣": 50.0}, "stats": {}}}}
    secs = [{"name": "油電燃氣", "score": 90.0, "confidence": {"level": "A"}},
            {"name": "水泥", "score": -30.0, "confidence": {"level": "B"}},
            {"name": "橡膠", "score": 80.0, "confidence": None}]
    order = F.apply_lights(cal, secs, "day")
    by = {s["name"]: s for s in secs}
    assert by["油電燃氣"]["light"]["state"] == 0, "90 分對油電燃氣只是 0.9 倍，不亮"
    assert by["水泥"]["light"]["state"] == -1 and by["水泥"]["light"]["weak_oos"], "−30 分對水泥是 1.2 倍，亮偏空；樣本外不到五成要標示"
    assert by["橡膠"]["light"] is None and order == ["水泥", "油電燃氣"]
    F.apply_lights(cal, secs, "night")
    assert by["油電燃氣"]["light"]["state"] == 1 and not by["油電燃氣"]["light"]["strong"]


def test_gate_states():
    import track as T
    assert T.gate([0.5, -0.2])["state"] == "collecting"
    assert T.gate([-0.5] * 30 + [0.2] * 15)["state"] == "demoted"            # 45 次、命中 33%、平均為負
    assert T.gate([0.5] * 30 + [-0.2] * 15)["state"] == "ok"
    g = T.gate([-0.5] * 50 + [0.5] * 60)                                      # 只看最近 60 次：全對 → 恢復
    assert g["state"] == "ok" and g["n"] == 60 and g["total"] == 110
    outs = T.light_outcomes({"lights": [{"name": "水泥", "state": -1, "z": -1.2}, {"name": "鋼鐵", "state": 0, "z": 0.3}]},
                            {"水泥": -0.4}, {"水泥": 0.3})
    assert outs == [{"name": "水泥", "state": -1, "z": -1.2, "same": 0.4, "next": -0.3}]


def test_light_stats_lit_vs_unlit():
    by = {"X": {f"2026-01-{i:02d}": [(50.0 if i % 2 else 5.0), 1.0 if i % 2 else -1.0, 0.0] for i in range(1, 29)}}
    per, pool = C.light_stats(by, {"X": 20.0}, "2026-01-01")
    assert per["X"]["lag0"] == {"n": 14, "hit": 1.0, "avg": 1.0}
    assert pool["lit"]["lag0"]["hit"] == 1.0 and pool["unlit"]["lag0"]["hit"] == 0.0 and pool["strong"]["lag0"]["n"] == 14


def test_ridge_shares_weight_between_collinear_features():
    import random
    rnd = random.Random(3)
    X, y = [], []
    for _ in range(200):
        f = rnd.gauss(0, 1)
        X.append([f, f + rnd.gauss(0, 0.01)])          # 兩個幾乎一樣的商品
        y.append(1.0 * f + rnd.gauss(0, 0.1))
    b = C.ridge_fit(X, y, 1.0)
    assert abs(b[0] - b[1]) < 0.2 and b[0] + b[1] == pytest.approx(1.0, abs=0.05), "共線的兩個特徵平分權重，合計約 1"
    assert C._solve([[2.0, 0.0], [0.0, 4.0]], [2.0, 8.0]) == [1.0, 2.0]


def test_build_links_scores_only_valid_direction():
    """方向與產業邏輯相反的關聯（即使統計顯著）不計分"""
    import random
    rnd = random.Random(5)
    ds = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(260)]
    x = {d: rnd.gauss(0, 1) for d in ds}
    ex = {d: {"甲": x[d] + rnd.gauss(0, 0.5), "乙": -x[d] + rnd.gauss(0, 0.5)} for d in ds}
    items = [("測", "X", "商品", 5.0, [("甲", 0.5, "邏輯同向"), ("乙", 0.5, "邏輯同向但資料相反")], True)]
    pairs, _disc, live = C.build_links(items, {"X": x}, ex, ds)
    st = {p["sector"]: p["status"] for p in pairs}
    assert st == {"甲": "有效", "乙": "方向相反"} and [l["sector"] for l in live["X"]] == ["甲"]


def test_alert_deploy_only_classification():
    import importlib.util
    import os
    os.environ.setdefault("GITHUB_REPOSITORY", "x/y")
    os.environ.setdefault("RUN_ID", "1")
    os.environ.setdefault("GH_TOKEN", "t")
    spec = importlib.util.spec_from_file_location("alert_t", ROOT / ".github" / "scripts" / "alert.py")
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    ok, bad = {"conclusion": "success"}, {"conclusion": "failure"}
    assert A.deploy_only({}, [{"name": "build", **ok}, {"name": "deploy / deploy", **bad}])
    assert not A.deploy_only({}, [{"name": "build", **bad}, {"name": "deploy / deploy", "conclusion": "skipped"}])
    assert not A.deploy_only({}, [{"name": "build", **ok}])


# ───────── 盤中／夜盤資料蒐集 ─────────
def test_intraday_parsers():
    import intraday as ID
    txt = 'var _CU0_30=([{"d":"2026-11-12 10:30:00","o":"1","h":"1","l":"1","c":"86550.000","v":"1","p":"1"},{"d":"bad"}]);'
    assert ID.parse_minline(txt) == {"2026-11-12 10:30": 86550.0}
    j = {"stat": "OK", "fields": ["時間", "發行量加權股價指數", "水泥類指數", "未含金融保險股指數"],
         "data": [["09:00:00", "23,000.50", "150.2", "1"], ["09:00:05", "23,001", "150.3", "1"], ["13:30:00", "23,100.00", "151.0", "1"]]}
    assert ID.parse_tw5(j) == {"09:00": {TAIEX_N: 23000.5, "水泥": 150.2}, "13:30": {TAIEX_N: 23100.0, "水泥": 151.0}}
    assert ID.parse_tw5({"stat": "很抱歉，沒有符合條件的資料!"}) == {}


TAIEX_N = H.TAIEX


def test_intraday_moves_and_study():
    import intraday as ID
    days = [(date(2026, 3, 2) + timedelta(days=i)).isoformat() for i in range(70)]
    cn, tw = {"X": {}}, {}
    for i, d in enumerate(days):
        up = i % 2 == 0
        cn["X"][f"{d} 15:00"] = 100.0                       # 日盤收盤
        cn["X"][f"{d} 23:00"] = 101.0 if up else 99.0        # 夜盤（屬於隔天的資訊）
        if i:
            pu = (i - 1) % 2 == 0                            # 前一晚夜盤的方向 → 今天早盤延續、台股午盤跟上
            cn["X"][f"{d} 10:30"] = 102.0 if pu else 98.0
            tw[d] = {"10:30": {TAIEX_N: 100.0, "甲": 100.0}, "13:30": {TAIEX_N: 100.0, "甲": 101.0 if pu else 99.0}}
    mv = ID.cn_moves(cn, sorted(tw))
    d2 = days[2]
    assert mv["night"]["X"][d2] == pytest.approx(-1.0) and mv["10:30"]["X"][d2] == pytest.approx(-2.0)
    assert d2 not in mv["12:00"].get("X", {}) or mv["12:00"]["X"][d2] == pytest.approx(-2.0)
    st = ID.study(cn, tw, {"X": [{"sector": "甲", "w": 0.5}]}, {"甲": 1.0})
    assert st["ready"] and st["tests"]["10:30"]["hit"] == 1.0 and st["tests"]["10:30"]["days"] == 69
    assert ID.study(cn, dict(list(tw.items())[:10]), {}, {})["ready"] is False


def test_intraday_night_spans_weekend():
    import intraday as ID
    cn = {"X": {"2026-11-13 15:00": 100.0, "2026-11-13 23:00": 101.0, "2026-11-14 01:00": 102.0, "2026-11-16 10:30": 103.0}}
    mv = ID.cn_moves(cn, ["2026-11-16"])
    assert mv["night"]["X"]["2026-11-16"] == pytest.approx(2.0), "週五夜盤（含週六凌晨）對應週一"
    assert mv["10:30"]["X"]["2026-11-16"] == pytest.approx(3.0)


def test_premarket_scope_decision():
    import calibrate as C
    a = lambda t, hit=0.5, avg=0.1: {"n": 300, "hit": hit, "avg": avg, "t": t}  # noqa: E731
    def row(cn_t, intl_t, days=120):
        return {"days": days, "from": "2026-03-04", "sectors_intl": ["油電燃氣", "電器電纜"], "sectors_cn": ["鋼鐵"],
                "cn_only": {"all": a(cn_t), "lit": a(cn_t)}, "intl_only": {"all": a(intl_t), "lit": a(intl_t)},
                "intl_combo": {"all": a(2.0), "lit": a(2.0)}}
    ok = C.premarket_scope_decision({"0.6": row(0.5, 3.0), "0.7": row(1.2, 2.5), "0.8": row(-0.3, 2.1)})
    assert ok["adopt"] and ok["mode"] == "intl_only" and ok["pre_sectors"] == ["油電燃氣", "電器電纜"]
    # 任何一個切點：陸期夜盤有預測力，或國際不夠顯著 → 不切換
    assert not C.premarket_scope_decision({"0.6": row(0.5, 3.0), "0.7": row(2.3, 2.5), "0.8": row(0.1, 2.1)})["adopt"]
    assert not C.premarket_scope_decision({"0.6": row(0.5, 3.0), "0.7": row(1.2, 1.5), "0.8": row(0.1, 2.1)})["adopt"]
    # 資料不夠、切點不足 → 不切換
    assert not C.premarket_scope_decision({"0.6": row(0.5, 3.0), "0.7": row(1.2, 2.5), "0.8": row(0.1, 2.1, days=40)})["adopt"]
    assert not C.premarket_scope_decision({})["adopt"]
    # 逐一檢查：只有自己過關的類股才在盤前亮燈
    rows = {k: {**row(0.5, 3.0), "qualified": ["油電燃氣"]} for k in ("0.6", "0.7", "0.8")}
    assert C.premarket_scope_decision(rows)["pre_sectors"] == ["油電燃氣"]
    rows = {k: {**row(0.5, 3.0), "qualified": []} for k in ("0.6", "0.7", "0.8")}
    assert not C.premarket_scope_decision(rows)["adopt"]


def test_apply_lights_pre_scope():
    import focus as F
    cal = {"lights": {"z": 1.0, "z_strong": 2.0, "pre": {"sd": {"鋼鐵": 10, "油電燃氣": 10}, "stats": {}},
                      "day": {"sd": {"鋼鐵": 10, "油電燃氣": 10}, "stats": {}}},
           "premarket_scope": {"adopt": True, "pre_sectors": ["油電燃氣"]}}
    mk = lambda: [{"name": "鋼鐵", "score": 30, "confidence": {"level": "B"}},  # noqa: E731
                  {"name": "油電燃氣", "score": -15, "confidence": {"level": "A"}}]
    s = mk(); order = F.apply_lights(cal, s, "night")
    assert s[0]["light"]["pre_off"] and s[0]["light"]["state"] == 0 and not s[0]["light"]["strong"]
    assert s[1]["light"]["state"] == -1 and not s[1]["light"].get("pre_off") and order == ["油電燃氣", "鋼鐵"]
    s = mk(); F.apply_lights(cal, s, "day")           # 收盤版不受影響
    assert s[0]["light"]["state"] == 1 and not s[0]["light"].get("pre_off")
    cal["premarket_scope"]["adopt"] = False
    s = mk(); F.apply_lights(cal, s, "night")         # 沒過關：照常亮燈
    assert s[0]["light"]["state"] == 1


def test_apply_lights_intl_basis_and_segments():
    import focus as F
    import intraday as ID
    cal = {"lights": {"z": 1.0, "z_strong": 2.0, "pre": {"sd": {"油電燃氣": 10}, "stats": {}},
                      "pre_intl": {"sd": {"油電燃氣": 20}, "stats": {"油電燃氣": {"lag0": {"n": 50, "hit": 0.6, "avg": 0.5}, "open": {"n": 40, "hit": 0.6, "avg": 0.2}}}}},
           "premarket_scope": {"adopt": True, "pre_sectors": ["油電燃氣"], "score": "intl"}}
    s = [{"name": "油電燃氣", "score": 30, "score_intl": -25, "confidence": {"level": "A"}}]
    F.apply_lights(cal, s, "night")      # 合併分數偏多，但只用國際是偏空 → 以國際為準
    L = s[0]["light"]
    assert L["basis"] == "intl" and L["state"] == -1 and L["score"] == -25 and L["z"] == -1.25 and L["stats"]["open"]["n"] == 40
    cal["premarket_scope"]["score"] = "combo"
    s[0].pop("light"); F.apply_lights(cal, s, "night")
    assert s[0]["light"]["state"] == 1 and "basis" not in s[0]["light"]
    # 當天報酬拆兩段：09:00 那筆是前一天收盤
    bars = {"2026-11-12": {"09:00": {"加權指數": 100, "鋼鐵": 100}, "09:30": {"加權指數": 101, "鋼鐵": 103},
                           "13:30": {"加權指數": 101, "鋼鐵": 104.03}}}
    sg = ID.tw_segments(bars, {"鋼鐵": 1.0}, "09:30")
    assert abs(sg["gap"]["鋼鐵"]["2026-11-12"] - 2.0) < 1e-6 and abs(sg["after"]["鋼鐵"]["2026-11-12"] - 1.0) < 1e-6


def test_parse_tw5_keeps_open_extra():
    import intraday as ID
    j = {"stat": "OK", "fields": ["時間", "發行量加權股價指數"],
         "data": [["09:00:00", "100"], ["09:00:05", "100.5"], ["09:05:00", "101"], ["09:07:00", "101.5"], ["09:15:00", "102"],
                  ["09:30:00", "103"], ["13:30:00", "104"]]}
    assert sorted(ID.parse_tw5(j)) == ["09:00", "09:05", "09:15", "09:30", "13:30"]


def test_basket_study_and_open_close():
    import calibrate as C
    by = {"油電燃氣": {"2026-05-01": [50, 0, 0], "2026-05-04": [-60, 0, 0], "2026-05-05": [5, 0, 0], "2026-01-02": [80, 0, 0]}}
    ind = {"6505": "油電燃氣", "9937": "油電燃氣", "2603": "航運"}
    cc = {"6505": {"2026-05-01": 2.0, "2026-05-04": -1.0}, "9937": {"2026-05-01": 1.0}, "2603": {"2026-05-01": 9}}
    oc = {"6505": {"2026-05-01": 1.0, "2026-05-04": 0.5}, "9937": {"2026-05-01": -1.0}, "2603": {"2026-05-01": 9}}
    b = C.basket_study(by, {"油電燃氣": 40}, "2026-03-01", ["油電燃氣"], ind, cc, oc)["油電燃氣"]
    assert b["lit_days"] == 2 and set(b["stocks"]) == {"6505", "9937"}          # 沒亮燈、切點前、別的類股都不算
    assert b["stocks"]["6505"]["day"]["avg"] == 1.5 and b["stocks"]["6505"]["oc"]["avg"] == 0.25    # 偏空日：跌才算對
    assert b["all"]["oc"]["n"] == 2 and b["all"]["oc"]["avg"] == -0.25          # 5/1 兩檔平均 0，5/4 只有一檔 −0.5
    res = {"timestamp": [1778000000, 1778086400], "indicators": {"quote": [{"open": [100, 50], "close": [102, None]}]}}
    assert list(C._open_close(res).values()) == [2.0]
