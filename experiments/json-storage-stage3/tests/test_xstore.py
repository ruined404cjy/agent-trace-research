import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import xstore
from assets import LocalAssetStore
from common import LAYOUTS, QuerySpec
from opengauss import OpenGaussAdapter


WINDOW = {
    "project_id": "Leoxx/whowhen_pro",
    "start_time": "2030-01-01T00:00:00.000Z",
    "end_time": "2030-01-01T00:52:08.500Z",
    "page_size": 256,
}


def adapter(root, layout="same_table"):
    """构造不连接数据库的 XStore adapter，仅用于检查语句构造。"""
    return xstore.XStoreAdapter(
        "127.0.0.1", 29000, "", "jsons3_test", layout,
        Path(root), LocalAssetStore(Path(root) / "assets"), user="runner",
    )


class XStoreQueryConstructionTest(unittest.TestCase):
    """XStore 重写的查询必须保持索引可用的访问路径与 openGauss 的取值语义。"""

    def test_paged_cursor_leads_with_an_index_range_lower_bound(self):
        """非空游标在展开式之前带一条 start_time >= cursor_time 下界。

        XStore 不把 OR 展开式用作索引范围起点，中间页从窗口起点扫描并逐行过滤；
        蕴含下界进入 Index Cond 后扫描从游标处开始。三处 start_time 比较必须绑定同一个
        游标时刻，绑定错位会让中间页返回与真值不同的行集合。
        列表与预览共用该游标，四种布局都要覆盖。
        """
        cursor = {"cursor_time": "2030-01-01T00:26:00.000Z", "cursor_id": "event-12345"}
        with tempfile.TemporaryDirectory() as root:
            for layout in LAYOUTS:
                for kind in ("list", "preview"):
                    with self.subTest(layout=layout, kind=kind):
                        statement, values = adapter(root, layout)._query_statement(
                            QuerySpec(kind, {**WINDOW, **cursor}))
                        self.assertIn(
                            "AND start_time >= %s AND (start_time > %s OR (start_time = %s AND event_id > %s)) "
                            "ORDER BY", statement)
                        self.assertEqual(statement.count("%s"), len(values))
                        self.assertEqual(
                            values,
                            (WINDOW["project_id"], WINDOW["start_time"], WINDOW["end_time"],
                             cursor["cursor_time"], cursor["cursor_time"], cursor["cursor_time"],
                             cursor["cursor_id"], 256),
                        )

    def test_bounded_cursor_returns_the_row_constructor_result_set(self):
        """带下界的展开式与行构造器比较选出同一组行，含与游标同一时刻的并列行。

        用 SQLite 执行两种谓词；下界只是 OR 条件的蕴含式，结果集任何变化都会让
        中间页偏离真值。
        """
        cursor_time = "2030-01-01T00:26:00.000Z"
        rows = [
            ("p", time, event_id)
            for time in ("2030-01-01T00:25:59.999Z", cursor_time, "2030-01-01T00:26:00.001Z")
            for event_id in ("event-1", "event-12345", "event-9")
        ] + [("other", cursor_time, "event-9")]
        cursor = {"cursor_time": cursor_time, "cursor_id": "event-12345"}
        with tempfile.TemporaryDirectory() as root:
            bounded, bounded_values = adapter(root)._query_statement(QuerySpec("list", {**WINDOW, **cursor}))
            reference, reference_values = OpenGaussAdapter._query_statement(
                adapter(root), QuerySpec("list", {**WINDOW, **cursor}))
        database = sqlite3.connect(":memory:")
        database.execute("CREATE TABLE events (project_id TEXT, start_time TEXT, event_id TEXT)")
        database.executemany("INSERT INTO events VALUES (?,?,?)", rows)

        def selected(statement, values):
            predicate = statement[statement.index(" WHERE ") + 7:statement.index(" ORDER BY")]
            window_values = ("p",) + tuple(values[1:-1])
            return database.execute(
                "SELECT start_time, event_id FROM events WHERE " + predicate.replace("%s", "?")
                + " ORDER BY start_time, event_id", window_values).fetchall()

        expected = selected(reference, reference_values)
        self.assertEqual(len(expected), 4, "游标之后应有同刻 event-9 与下一时刻三行")
        self.assertEqual(selected(bounded, bounded_values), expected)

    def test_first_page_omits_the_tautological_cursor_condition(self):
        """首页游标是窗口起点之前的哨兵值，展开后恒真，直接省略。"""
        with tempfile.TemporaryDirectory() as root:
            query = QuerySpec("list", {**WINDOW, "cursor_time": "1900-01-01T00:00:00.000Z", "cursor_id": ""})
            statement, values = adapter(root)._query_statement(query)
        self.assertNotIn("OR (", statement)
        self.assertEqual(len(values), 4, "首页只绑定项目、窗口两端与分页大小")

    def test_detail_and_trace_predicates_match_the_shared_contract(self):
        """详情与 Trace 的谓词列与顺序与 openGauss 一致，重写只作用于分页游标。"""
        with tempfile.TemporaryDirectory() as root:
            live = adapter(root)
            detail, detail_values = live._query_statement(QuerySpec("detail", {
                "project_id": "p", "trace_id": "t",
                "start_time": WINDOW["start_time"], "event_id": "e",
            }))
            trace, trace_values = live._query_statement(QuerySpec("trace", {
                "project_id": "p", "trace_id": "t",
                "start_time": WINDOW["start_time"], "end_time": WINDOW["end_time"],
            }))
        self.assertIn("WHERE project_id=%s AND trace_id=%s AND start_time=%s AND event_id=%s", detail)
        self.assertEqual(detail_values, ("p", "t", WINDOW["start_time"], "e"))
        self.assertIn("ORDER BY start_time,event_id", trace)
        self.assertEqual(trace_values, ("p", "t", WINDOW["start_time"], WINDOW["end_time"]))


class XStoreIdentityTest(unittest.TestCase):
    """运行账号来自环境，实现偏离随运行清单落盘。"""

    def test_missing_run_account_is_rejected(self):
        """缺少运行账号时拒绝构造，不落到内置默认值。"""
        with tempfile.TemporaryDirectory() as root:
            saved = os.environ.pop("XSTORE_USER", None)
            try:
                with self.assertRaises(ValueError):
                    xstore.XStoreAdapter(
                        "127.0.0.1", 29000, "", "jsons3_test", "same_table",
                        Path(root), LocalAssetStore(Path(root) / "assets"),
                    )
            finally:
                if saved is not None:
                    os.environ["XSTORE_USER"] = saved

    def test_declared_deviations_cover_every_rewritten_behaviour(self):
        """四项相对 openGauss 的偏离都要有登记，否则清单无法解释结果差异。"""
        self.assertEqual(
            set(xstore.XSTORE_DEVIATIONS),
            {"framework_nullable", "keyset_predicate", "watermark_aggregate", "connection"},
        )
        for reason in xstore.XSTORE_DEVIATIONS.values():
            self.assertTrue(reason.strip(), "偏离必须写明原因")

    def test_keyset_deviation_describes_the_emitted_lower_bound(self):
        """清单中的游标偏离必须写明实际发出的下界，读清单的人据此解释中间页计划。"""
        self.assertIn("start_time >= cursor_time", xstore.XSTORE_DEVIATIONS["keyset_predicate"])


if __name__ == "__main__":
    unittest.main()
