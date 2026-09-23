import os
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import xstore
from assets import LocalAssetStore
from common import QuerySpec


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

    def test_paged_cursor_expands_to_an_equivalent_disjunction(self):
        """行构造器比较展开为等价的 OR 形式，结果集与 openGauss 版本一致。

        展开式的两处 start_time 比较必须绑定同一个游标时刻，绑定错位会让中间页
        返回与真值不同的行集合，真值门禁随之失败。
        """
        with tempfile.TemporaryDirectory() as root:
            query = QuerySpec("list", {
                **WINDOW,
                "cursor_time": "2030-01-01T00:26:00.000Z",
                "cursor_id": "event-12345",
            })
            statement, values = adapter(root)._query_statement(query)
        self.assertIn("(start_time > %s OR (start_time = %s AND event_id > %s))", statement)
        self.assertEqual(
            values,
            (WINDOW["project_id"], WINDOW["start_time"], WINDOW["end_time"],
             "2030-01-01T00:26:00.000Z", "2030-01-01T00:26:00.000Z", "event-12345", 256),
        )

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


if __name__ == "__main__":
    unittest.main()
