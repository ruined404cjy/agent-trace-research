import glob
import importlib.util
import os
import random
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "tools"))

import bench_libpq_fetch as bench
import gaussdb_libpq


# libpq 类型 OID，与 fetchall 的类型分派一致。
BOOL, INT8, INT2, INT4, TEXT, FLOAT4, FLOAT8, DATE, TIMESTAMP, TIMESTAMPTZ, NUMERIC = (
    16, 20, 21, 23, 25, 700, 701, 1082, 1114, 1184, 1700,
)


class FakeLibpq:
    """按 libpq 文本协议语义模拟结果集：NULL 单元 PQgetvalue 返回空串，PQgetisnull 为 1。

    结果以整数句柄表示；execute 的语句与绑定值记录在 calls 中。
    """

    def __init__(self):
        self.results = {}
        self.calls = []
        self.next_result = None

    def add_result(self, oids, rows):
        handle = len(self.results) + 1
        self.results[handle] = (tuple(oids), [tuple(row) for row in rows])
        return handle

    def PQexecParams(self, connection, statement, count, types, values, lengths, formats, result_format):
        self.calls.append((statement, [values[index] for index in range(count)]))
        return self.next_result

    def PQexec(self, connection, statement):
        self.calls.append((statement, []))
        return self.next_result

    def PQresultStatus(self, result):
        return gaussdb_libpq.PGRES_TUPLES_OK

    def PQntuples(self, result):
        return len(self.results[result][1])

    def PQnfields(self, result):
        return len(self.results[result][0])

    def PQftype(self, result, column):
        return self.results[result][0][column]

    def PQgetisnull(self, result, row, column):
        return int(self.results[result][1][row][column] is None)

    def PQgetvalue(self, result, row, column):
        value = self.results[result][1][row][column]
        return b"" if value is None else value

    def PQgetlength(self, result, row, column):
        return len(self.PQgetvalue(result, row, column))

    def PQclear(self, result):
        pass


def assert_rows(case, actual, expected):
    """逐单元比较值与类型；失败信息截断，避免对 MiB 级文本做逐字符 diff。"""
    case.assertEqual(len(actual), len(expected), "row count differs")
    for i, (got_row, want_row) in enumerate(zip(actual, expected)):
        case.assertIs(type(got_row), tuple)
        case.assertEqual(len(got_row), len(want_row), f"row {i} width differs")
        for j, (got, want) in enumerate(zip(got_row, want_row)):
            if type(got) is not type(want) or got != want:
                case.fail(f"cell {i},{j}: {type(got).__name__} {got!r:.120} != {type(want).__name__} {want!r:.120}")


class FakeConnection:
    _closed = False
    _pg_conn = 1


class LibpqCase(unittest.TestCase):
    """把模块级 _libpq 换成 FakeLibpq，经 execute 与 fetchall 取回 Python 值。"""

    def setUp(self):
        self.fake = FakeLibpq()
        saved = gaussdb_libpq._libpq
        gaussdb_libpq._libpq = self.fake
        self.addCleanup(setattr, gaussdb_libpq, "_libpq", saved)

    def fetch(self, oids, rows, statement="SELECT x", params=None):
        self.fake.next_result = self.fake.add_result(oids, rows)
        cursor = gaussdb_libpq.GaussDBCursor(FakeConnection())
        return cursor.execute(statement, params).fetchall()

    def assertTyped(self, actual, expected):
        """逐值比较并要求类型完全相同，区分 bool 与 int、None 与空串。"""
        assert_rows(self, actual, expected)


class FetchallValueTest(LibpqCase):
    """fetchall 的取值与类型是 truth 门禁的输入，任何偏差都会让查询被判为不一致。"""

    def test_scalar_types_convert_to_their_python_types(self):
        rows = self.fetch(
            (BOOL, BOOL, INT8, INT2, INT4, FLOAT4, FLOAT8, DATE, NUMERIC),
            [(b"t", b"f", b"9223372036854775807", b"-32768", b"0", b"1.5", b"-0.25", b"2030-01-02", b"12.50")],
        )
        self.assertTyped(rows, [(True, False, 9223372036854775807, -32768, 0, 1.5, -0.25, date(2030, 1, 2), "12.50")])

    def test_float_special_values_follow_python_float_parsing(self):
        rows = self.fetch((FLOAT8, FLOAT8, FLOAT8), [(b"Infinity", b"-Infinity", b"-0")])
        self.assertEqual(rows[0][:2], (float("inf"), float("-inf")))
        self.assertEqual(str(rows[0][2]), "-0.0")
        nan = self.fetch((FLOAT8,), [(b"NaN",)])[0][0]
        self.assertNotEqual(nan, nan)

    def test_null_is_none_and_empty_text_stays_an_empty_string(self):
        """libpq 对 NULL 与空串都返回空值，只有 PQgetisnull 能区分两者。"""
        rows = self.fetch((TEXT, TEXT, INT8, TIMESTAMPTZ, BOOL), [
            (None, b"", None, None, None),
            (b"", None, b"7", None, b"t"),
        ])
        self.assertTyped(rows, [(None, "", None, None, None), ("", None, 7, None, True)])

    def test_text_decodes_multibyte_utf8_and_large_values_intact(self):
        large = ("载荷-" * 700000).encode("utf-8")[: 2 * 1024 * 1024 - 1] + b"x"
        large = large.decode("utf-8", errors="ignore").encode("utf-8")
        rows = self.fetch((TEXT, TEXT), [("中文 payload ✓ 😀".encode("utf-8"), large)])
        self.assertTyped(rows, [("中文 payload ✓ 😀", large.decode("utf-8"))])

    def test_timestamptz_converts_to_an_aware_utc_datetime(self):
        """服务端按会话时区输出，转换为 UTC 后 adapter 的 _timestamp 才能写出 Z 后缀。"""
        rows = self.fetch((TIMESTAMPTZ,) * 6, [(
            b"2030-01-01 08:26:00.123+08",
            b"2030-01-01 08:26:00+08",
            b"2030-01-01 00:26:00.123456+00",
            b"2030-01-01 00:26:00.12+05:30",
            b"2030-01-01 00:26:00.123-03",
            b"1900-01-01 08:05:43+08:05:43",
        )])
        self.assertTyped(rows, [(
            datetime(2030, 1, 1, 0, 26, 0, 123000, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 0, 26, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 0, 26, 0, 123456, tzinfo=timezone.utc),
            datetime(2029, 12, 31, 18, 56, 0, 120000, tzinfo=timezone.utc),
            datetime(2030, 1, 1, 3, 26, 0, 123000, tzinfo=timezone.utc),
            datetime(1900, 1, 1, 0, 0, tzinfo=timezone.utc),
        )])
        for value in rows[0]:
            self.assertIs(value.tzinfo, timezone.utc)

    def test_timestamp_without_zone_is_taken_as_utc(self):
        rows = self.fetch((TIMESTAMP, TIMESTAMP), [(b"2030-01-01 00:26:00", b"2030-01-01 00:26:00.5")])
        self.assertTyped(rows, [(datetime(2030, 1, 1, 0, 26, tzinfo=timezone.utc),
                                 datetime(2030, 1, 1, 0, 26, 0, 500000, tzinfo=timezone.utc))])
        self.assertIs(rows[0][0].tzinfo, timezone.utc)

    def test_non_canonical_timestamps_keep_the_general_parser_results(self):
        """非规范文本仍按通用解析规则处理：去空白、截断第七位小数、无法解析时原样返回。"""
        rows = self.fetch((TIMESTAMPTZ,) * 4 + (DATE,), [(
            b" 2030-01-01 00:26:00+08 ",
            b"2030-01-01 00:26:00.1234567+08",
            b"infinity",
            b"2030-01-01 00:26:00+0530",
            b"infinity",
        )])
        self.assertTyped(rows, [(
            datetime(2029, 12, 31, 16, 26, tzinfo=timezone.utc),
            datetime(2029, 12, 31, 16, 26, 0, 123456, tzinfo=timezone.utc),
            "infinity",
            datetime(2029, 12, 31, 18, 56, tzinfo=timezone.utc),
            "infinity",
        )])

    def test_timestamp_outside_the_datetime_range_raises(self):
        """转换到 UTC 越过 datetime 下界时报错，不静默返回错误时刻。"""
        with self.assertRaises(OverflowError):
            self.fetch((TIMESTAMPTZ,), [(b"0001-01-01 00:00:00+08",)])

    def test_list_page_shape_round_trips_every_cell(self):
        """list:first 形态：11 列文本、时间与整数混合，preview 与 payload 为类型化 NULL。"""
        oids = (TEXT, TEXT, TEXT, TIMESTAMPTZ, TEXT, TEXT, TEXT, INT8, TEXT, TEXT, TEXT)
        raw, expected = [], []
        for index in range(256):
            raw.append((f"event-{index:05d}".encode(), b"trace-a", "项目/a".encode(),
                        f"2030-01-01 08:{index // 60:02d}:{index % 60:02d}.{index:03d}+08".encode(),
                        b"text_64k", b"application/json", b"utf-8", str(65536 + index).encode(),
                        None, b"ab" * 32, None))
            expected.append((f"event-{index:05d}", "trace-a", "项目/a",
                             datetime(2030, 1, 1, 0, index // 60, index % 60, index * 1000, tzinfo=timezone.utc),
                             "text_64k", "application/json", "utf-8", 65536 + index, None, "ab" * 32, None))
        self.assertTyped(self.fetch(oids, raw), expected)

    def test_empty_result_and_fetchone(self):
        self.assertEqual(self.fetch((TEXT,), []), [])
        self.fake.next_result = self.fake.add_result((INT4,), [(b"1",), (b"2",)])
        cursor = gaussdb_libpq.GaussDBCursor(FakeConnection())
        self.assertEqual(cursor.execute("SELECT 1").fetchone(), (1,))


class TimestampFastPathTest(unittest.TestCase):
    """规范文本的快速路径必须与通用解析给出相同的值与时区对象，否则中间页游标与真值错位。"""

    def test_fast_path_agrees_with_the_general_parser(self):
        generator = random.Random(20260924)
        samples = ["2030-13-01 00:00:00+08", "2030-02-30 00:00:00", "2030-01-01 25:00:00+08",
                   "2030-01-01 00:00:60+08", "1900-01-01 00:00:00+08:05:43", "2030-01-01 00:26:00+14"]
        for _ in range(2000):
            fraction = "".join(generator.choice("0123456789") for _ in range(generator.randint(0, 6)))
            offset = generator.choice(["", "+00", "+08", "-03", "+05:30", "-09:30", "+08:05:43"])
            samples.append(
                f"{generator.randint(1900, 2100):04d}-{generator.randint(1, 12):02d}-{generator.randint(1, 28):02d} "
                f"{generator.randint(0, 23):02d}:{generator.randint(0, 59):02d}:{generator.randint(0, 59):02d}"
                + (f".{fraction}" if fraction else "") + offset)
        for sample in samples:
            with self.subTest(sample=sample):
                fast = gaussdb_libpq._parse_timestamp(sample)
                general = gaussdb_libpq._parse_timestamp_general(sample)
                self.assertEqual((type(fast), fast), (type(general), general))
                if isinstance(fast, datetime):
                    self.assertIs(fast.tzinfo, general.tzinfo)


class ExecuteParameterTest(LibpqCase):
    """参数以文本绑定；NULL 参数内联为 SQL NULL，其余按出现顺序编号。"""

    def test_parameters_are_numbered_in_order_and_nulls_are_inlined(self):
        when = datetime(2030, 1, 1, 0, 26, tzinfo=timezone.utc)
        self.fetch((INT4,), [], "SELECT %s, %s, %s, %s, %s, %s, %s", ("中文", None, 7, 1.5, True, when, b"raw"))
        statement, values = self.fake.calls[-1]
        self.assertEqual(statement, b"SELECT $1, NULL, $2, $3, $4, $5, $6")
        self.assertEqual(values, ["中文".encode(), b"7", b"1.5", b"true", b"2030-01-01T00:26:00+00:00", b"raw"])

    def test_all_null_parameters_use_plain_exec(self):
        self.fetch((INT4,), [], "SELECT %s", (None,))
        self.assertEqual(self.fake.calls[-1], (b"SELECT NULL", []))


def _local_libpq():
    """返回可用于构造结果集的 libpq：GAUSSDB_LIB_DIR 优先，其次 psycopg-binary 自带的 libpq。"""
    gauss = Path(os.environ.get("GAUSSDB_LIB_DIR", "")) / "libpq.so.5"
    if os.environ.get("GAUSSDB_LIB_DIR") and gauss.is_file():
        return gauss
    spec = importlib.util.find_spec("psycopg_binary")
    if spec is None or not spec.origin:
        return None
    found = glob.glob(str(Path(spec.origin).parent.parent / "psycopg_binary.libs" / "libpq-*.so*"))
    return Path(found[0]) if found else None


class BenchmarkCaseTest(LibpqCase):
    """基准用例必须保持 list:first 与详情的结果形态，否则前后对比测的不是同一负载。"""

    def test_list_first_case_matches_the_list_projection(self):
        columns, rows = bench.list_first_case()
        self.assertEqual([name for name, _ in columns][:10], list(__import__("opengauss").LOGICAL_FIELDS))
        self.assertEqual(len(columns), 11)
        self.assertEqual(len(rows), 256)
        self.assertEqual({row[8] for row in rows} | {row[10] for row in rows}, {None})
        self.assertEqual(dict(columns)["start_time"], TIMESTAMPTZ)
        self.assertEqual(dict(columns)["content_length"], INT8)

    def test_detail_cases_carry_the_named_payload_size(self):
        for name, size in (("detail_64k", 64 * 1024), ("detail_2m", 2 * 1024 * 1024)):
            with self.subTest(name=name):
                columns, rows = bench.CASES[name]()
                self.assertEqual(len(rows), 1)
                self.assertEqual(len(rows[0][10]), size)

    def test_fake_rows_decode_like_the_case_definition(self):
        """FakeLibpq 取回的值是真实 libpq 对照的基准，先确认其与用例定义一致。"""
        columns, rows = bench.list_first_case(3)
        fetched = self.fetch([oid for _, oid in columns], rows)
        self.assertEqual(fetched[2][3], datetime(2030, 1, 1, 0, 0, 2, 2000, tzinfo=timezone.utc))
        self.assertEqual(fetched[2][7], 65538)


@unittest.skipUnless(_local_libpq(), "no libpq available to build in-process results")
class RealLibpqTest(unittest.TestCase):
    """用真实 libpq 构造的结果集校验 ctypes 取值路径，并确认 FakeLibpq 的 NULL 语义与之相同。"""

    def setUp(self):
        saved = gaussdb_libpq._libpq
        self.addCleanup(setattr, gaussdb_libpq, "_libpq", saved)
        self.library = bench.load_library(_local_libpq())

    def fake_rows(self, columns, rows):
        fake = FakeLibpq()
        gaussdb_libpq._libpq = fake
        try:
            fake.next_result = fake.add_result([oid for _, oid in columns], rows)
            return gaussdb_libpq.GaussDBCursor(FakeConnection()).execute("SELECT x").fetchall()
        finally:
            gaussdb_libpq._libpq = self.library

    def test_real_and_fake_libpq_return_identical_rows(self):
        for name, build in bench.CASES.items():
            with self.subTest(case=name):
                columns, rows = build()
                timing, fetched = bench.measure(self.library, columns, rows, repeat=1)
                self.assertEqual(set(timing), {"fetchall_p50", "fetchall_p95", "getvalue_p50"})
                assert_rows(self, fetched, self.fake_rows(columns, rows))

    def test_real_libpq_distinguishes_null_from_empty_text(self):
        columns = (("a", TEXT), ("b", TEXT), ("c", INT8))
        rows = [(b"", None, None), ("多字节".encode(), b"", b"-1")]
        result = bench.build_result(self.library, columns, rows)
        cursor = gaussdb_libpq.GaussDBCursor(bench._Connection())
        cursor._result = result
        try:
            self.assertEqual(cursor.fetchall(), [("", None, None), ("多字节", "", -1)])
        finally:
            cursor.close()


if __name__ == "__main__":
    unittest.main()
