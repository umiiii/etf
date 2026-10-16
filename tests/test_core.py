"""核心逻辑测试。依赖 data/ 缓存的用例在缓存缺失时跳过。运行：uv run python -m unittest -v"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from etfprem import history, store
from etfprem.calendar import Calendar
from etfprem.config import DATA, get_fund
from etfprem.estimator import Estimator, snap_split
from etfprem.sources import szse_pcf

FUND = get_fund("159509")
PCF_FILE = DATA / "pcf" / "159509" / "20261009.txt"
HAS_DATA = PCF_FILE.exists() and (DATA / "us_prices" / "NVDA.json").exists()


class TestPCF(unittest.TestCase):
    @unittest.skipUnless(PCF_FILE.exists(), "缺少缓存的 PCF")
    def test_parse(self):
        p = szse_pcf.parse(PCF_FILE.read_bytes(), "159509")
        self.assertEqual(p.trade_date, date(2026, 10, 9))
        self.assertEqual(p.nav_date, date(2026, 9, 30))  # 清单里的净值是 T 的前两个交易日
        self.assertEqual(len(p.holdings), 47)
        self.assertEqual(p.holdings["AAPL"].qty, 139)
        self.assertAlmostEqual(p.holdings["AAPL"].sub_amount, 346784.05)
        self.assertAlmostEqual(p.nav, 2.38040466)
        self.assertEqual(p.nav4, 2.3804)
        self.assertAlmostEqual(p.cash_diff, 138.81)
        self.assertAlmostEqual(p.est_cash_diff, 32.68)
        self.assertAlmostEqual(p.cash_component, 2618409.13)


class TestSplit(unittest.TestCase):
    def test_snap(self):
        self.assertEqual(snap_split(1.004), 1.0)
        self.assertEqual(snap_split(3.9999), 4.0)
        self.assertEqual(snap_split(10.002), 10.0)
        self.assertEqual(snap_split(0.5001), 0.5)
        self.assertEqual(snap_split(1.5), 1.5)


class TestCalendar(unittest.TestCase):
    def test_holiday_alignment(self):
        cn = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8)]
        us = [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8)]
        cal = Calendar(cn, us)
        self.assertEqual(cal.prev_cn(date(2026, 10, 8)), date(2026, 9, 30))
        self.assertEqual(cal.next_cn(date(2026, 9, 30)), date(2026, 10, 8))
        self.assertEqual(cal.us_on_or_before(date(2026, 10, 7)), date(2026, 10, 5))
        self.assertEqual(
            cal.us_between(date(2026, 9, 30), date(2026, 10, 5)),
            [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)],
        )


@unittest.skipUnless(HAS_DATA, "缺少 data/ 缓存，先运行 etfprem update")
class TestHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = store.load(FUND)
        cls.rows = history.compute(cls.md, date(2025, 10, 12), date(2026, 10, 12))
        cls.summary = history.summarize(cls.rows)

    def test_accuracy(self):
        nav = self.summary["nav_err_bp"]
        self.assertLess(nav["mae"], 1.5)
        self.assertLess(nav["max"], 15)
        self.assertLess(abs(nav["mean"]), 0.5)

    def test_official_premium_matches_jisilu(self):
        self.assertGreater(self.summary["jisilu_days"], 0)
        self.assertLess(self.summary["jisilu_vs_official_pp"]["max"], 0.006)

    def test_no_lookahead(self):
        """删掉 T 日开盘后才知道的数据，估算结果必须完全相同。"""
        md, full = self.md, Estimator(self.md)
        for D in (date(2025, 12, 22), date(2026, 5, 6), date(2026, 9, 21), date(2026, 10, 8)):
            st = full.step(D)
            T = full.cal.next_cn(D)
            cut = replace(
                md,
                pcf={k: v for k, v in md.pcf.items() if k <= T},
                nav={k: v for k, v in md.nav.items() if k <= st.Dp},
                nav4={k: v for k, v in md.nav4.items() if k <= st.Dp},
                etf={k: v for k, v in md.etf.items() if k <= T},
                fx={k: v for k, v in md.fx.items() if k <= T},
                px={c: {k: v for k, v in s.items() if k <= st.s1} for c, s in md.px.items()},
                index={k: v for k, v in md.index.items() if k <= st.s1},
                jisilu={},
            )
            a, b = full.estimate(D), Estimator(cut).estimate(D)
            self.assertEqual(a.nav, b.nav, D)
            self.assertEqual(a.exposure, b.exposure, D)

    def test_split_days_handled(self):
        """KLAC 1 拆 10、CRWD 1 拆 4 前后，复权股数应连续。"""
        est = Estimator(self.md)
        for code, before, on, after in (
            ("KLAC", date(2026, 6, 11), date(2026, 6, 12), date(2026, 6, 15)),
            ("CRWD", date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)),
        ):
            q0, q1, q2 = (est.holdings(d).get(code) for d in (before, on, after))
            self.assertIsNotNone(q1, f"{code} 拆股当天应沿用前一份清单")
            self.assertAlmostEqual(q0, q1)
            self.assertLess(abs(q2 / q0 - 1), 0.3, f"{code} 拆股后复权股数应与拆股前接近")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 501225：定期报告持仓

from etfprem.estimator import ReportEstimator  # noqa: E402
from etfprem.sources import fund_reports  # noqa: E402

LOF = get_fund("501225")
Q2_PDF = DATA / "fund_reports" / "501225" / "AN202607201827154075.pdf"
HAS_LOF = (DATA / "fund_reports" / "501225" / "reports.json").exists() and (DATA / "cn_prices" / "159995.json").exists()


class TestFundReport(unittest.TestCase):
    def test_period_end(self):
        self.assertEqual(fund_reports.period_end("某基金2026年第2季度报告"), date(2026, 6, 30))
        self.assertEqual(fund_reports.period_end("某基金2026年中期报告"), date(2026, 6, 30))
        self.assertEqual(fund_reports.period_end("某基金2025年年度报告"), date(2025, 12, 31))
        self.assertIsNone(fund_reports.period_end("某基金招募说明书"))

    @unittest.skipUnless(Q2_PDF.exists(), "缺少 2026 年二季报 PDF")
    def test_parse_q2_holdings(self):
        pct, fv, unknown = fund_reports.parse_fund_holdings(fund_reports.pdf_text(Q2_PDF), LOF.assets)
        self.assertEqual(unknown, [])
        self.assertEqual(len(pct), 8)
        self.assertAlmostEqual(pct["PSI"], 0.1821)
        self.assertAlmostEqual(pct["159995"], 0.0774)
        self.assertAlmostEqual(pct["2644"], 0.0173)
        self.assertAlmostEqual(fv["SOXX"], 214581003.65)


@unittest.skipUnless(HAS_LOF, "缺少 501225 缓存，先运行 etfprem update --fund 501225")
class TestReportFund(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = store.load(LOF)
        cls.rows = history.compute(cls.md, date(2025, 10, 12), date(2026, 10, 12))
        cls.summary = history.summarize(cls.rows)

    def test_accuracy(self):
        nav = self.summary["nav_err_bp"]
        self.assertLess(nav["mae"], 4.0)
        self.assertLess(nav["max"], 25)

    def test_a_share_prices_adjusted(self):
        """三只 A 股 ETF 都做过 1 拆 2，复权后不应出现超过 ±21% 的单日跳变。"""
        for code in ("159995", "512760", "159560"):
            s = self.md.px[code]
            ks = sorted(s)
            worst = max(abs(s[b] / s[a] - 1) for a, b in zip(ks, ks[1:]))
            self.assertLess(worst, 0.21, code)

    def test_report_by_publish_date(self):
        est = ReportEstimator(self.md)
        self.assertEqual(est.report_for(date(2026, 7, 21)).period_end, date(2026, 3, 31))  # 二季报当天发布，当天不能用
        self.assertEqual(est.report_for(date(2026, 7, 22)).period_end, date(2026, 6, 30))

    def test_no_lookahead(self):
        md, full = self.md, ReportEstimator(self.md)
        for D in (date(2025, 11, 6), date(2026, 3, 27), date(2026, 7, 21), date(2026, 10, 8)):
            st = full.step(D)
            T = full.cal.next_cn(D)
            cut = replace(
                md,
                nav={k: v for k, v in md.nav.items() if k <= st.Dp},
                nav4={k: v for k, v in md.nav4.items() if k <= st.Dp},
                etf={k: v for k, v in md.etf.items() if k <= T},
                px={c: {k: v for k, v in s.items() if k <= D} for c, s in md.px.items()},
                fx_all={c: {k: v for k, v in s.items() if k <= T} for c, s in md.fx_all.items()},
                reports=[r for r in md.reports if r.publish_date < T],
                jisilu={},
            )
            a, b = full.estimate(D), ReportEstimator(cut).estimate(D)
            self.assertEqual(a.nav, b.nav, D)
            self.assertEqual(a.betas, b.betas, D)


# ---------------------------------------------------------------- 溢价率建模（需要 uv sync --extra analysis）

try:
    import numpy as np  # noqa: E402

    from etfprem import analysis  # noqa: E402

    HAS_ANALYSIS = (DATA / "index" / "NDXTMC.json").exists()
except ImportError:
    HAS_ANALYSIS = False


@unittest.skipUnless(HAS_ANALYSIS and HAS_DATA, "缺少可选依赖或长历史数据")
class TestPremiumModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = analysis.build_panel(FUND)

    def test_identity(self):
        """价格收益 = 净值收益 + 溢价变化，逐日精确成立。"""
        for h in (1, 5, 20):
            d = self.df[[f"rP{h}", f"rN{h}", f"dZ{h}"]].dropna()
            self.assertLess(float((d[f"rP{h}"] - d[f"rN{h}"] - d[f"dZ{h}"]).abs().max()), 1e-12)

    def test_excludes_halts_and_holiday_gaps(self):
        bad = self.df[(self.df["vol"] <= 0) | self.df["gap"]]
        self.assertTrue(bad["z"].isna().all())

    def test_structural_model(self):
        s = analysis.structural(self.df, oos_start=10_000)  # 只估参数，跳过样本外
        p = s["params"]
        self.assertTrue(0 < p["phi"] < 1)
        self.assertAlmostEqual(sum(s["dz_share"].values()), 1.0)
        f = {h: analysis.model_forecast(s["states"].T.to_numpy(), list(p.values()), h) for h in (1, 5)}
        for h, fc in f.items():  # 预测同样满足分解：价格 = 净值 + 溢价变化
            self.assertLess(float(np.nanmax(np.abs(fc["rP"] - fc["rN"] - fc["dZ"]))), 1e-12)
