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


def test_momentum_z():
    ds = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(6)]
    ret = dict(zip(ds, [1, 1, 1, 1, 1, 1]))
    sig = {d: 1.0 for d in ds}
    m = H.momentum_z(ret, sig, 5)
    assert ds[3] not in m and m[ds[4]] == pytest.approx(5 / 5 ** 0.5)
    assert H.combo_z(1.0, 2.0) == pytest.approx(2.0) and H.combo_z(1.0, None) == 1.0


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
    by = {s["name"]: s for s in p["sectors"]}
    for n in p["bull"] + p["bear"]:
        assert len(by[n]["strong"]) <= 5 and len(by[n]["weak"]) <= 5
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
