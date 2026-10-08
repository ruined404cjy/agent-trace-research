"""gaussdb_libpq 客户端取数开销的微基准，不连接数据库。

用法（GAUSSDB_LIB_DIR 指向含 libpq.so.5 的目录，或用 --libpq 指定任一 libpq）：

    python experiments/json-storage-stage3/tools/bench_libpq_fetch.py [--libpq <path>] [--repeat 200]

用 libpq 的 PQmakeEmptyPGresult、PQsetResultAttrs 与 PQsetvalue 在进程内构造结果集，
再经 GaussDBCursor.fetchall 取回 Python 值。测得的是每个单元的 ctypes 调用与类型转换开销，
不含网络往返与服务端执行。三个用例：

- list_first：256 行 x 11 列，列类型与 list:first 相同（文本、timestamptz、bigint、类型化 NULL）；
- detail_64k 与 detail_2m：1 行 x 11 列，payload 列分别为 64 KiB 与 2 MiB 文本。

每个用例输出一行：用例名、fetchall 的 p50 与 p95（毫秒），以及只调用 PQgetvalue 遍历全部单元的
p50（毫秒），后者是逐单元 ctypes 调用的下限。
"""

import argparse
import ctypes
import os
import statistics
import sys
import time
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
RUNNER_DIR = STAGE_DIR / "runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import gaussdb_libpq


TEXT, INT8, TIMESTAMPTZ = 25, 20, 1184
# list:first 的 11 列：LOGICAL_FIELDS 加 payload_value，preview 与 payload 投影为 NULL::text。
LIST_COLUMNS = (
    ("event_id", TEXT), ("trace_id", TEXT), ("project_id", TEXT), ("start_time", TIMESTAMPTZ),
    ("profile", TEXT), ("content_type", TEXT), ("encoding", TEXT), ("content_length", INT8),
    ("preview", TEXT), ("sha256", TEXT), ("payload_value", TEXT),
)


class PGresAttDesc(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char_p), ("tableid", ctypes.c_uint), ("columnid", ctypes.c_int),
                ("format", ctypes.c_int), ("typid", ctypes.c_uint), ("typlen", ctypes.c_int),
                ("atttypmod", ctypes.c_int)]


def list_first_case(rows=256):
    """返回 list:first 形态的 (列定义, 行)；单元为 bytes，None 表示 NULL。"""
    data = []
    for index in range(rows):
        data.append((
            f"evt-{index:08d}-4f3c-9a1e-5b7d2c8e{index:04d}".encode(), f"{index:032x}".encode(),
            b"Leoxx/whowhen_pro",
            f"2030-01-01 08:{index // 60 % 60:02d}:{index % 60:02d}.{index % 1000:03d}+08".encode(),
            b"text_64k", b"application/json", b"utf-8", str(65536 + index).encode(),
            None, f"{index:064x}".encode(), None,
        ))
    return LIST_COLUMNS, data


def detail_case(payload_bytes):
    """返回 1 行详情形态的 (列定义, 行)，payload_value 为指定字节数的文本。"""
    columns, rows = list_first_case(1)
    row = list(rows[0])
    row[8] = b"p" * 200
    row[10] = b'{"content":"' + b"x" * (payload_bytes - 14) + b'"}'
    return columns, [tuple(row)]


CASES = {
    "list_first": list_first_case,
    "detail_64k": lambda: detail_case(64 * 1024),
    "detail_2m": lambda: detail_case(2 * 1024 * 1024),
}


def load_library(path=None):
    """加载 libpq、绑定 gaussdb_libpq 所用签名与构造结果集的函数，并设为模块的 _libpq。"""
    if path is None:
        library = gaussdb_libpq._load_libpq()
    else:
        library = ctypes.cdll.LoadLibrary(str(path))
        gaussdb_libpq._bind_signatures(library)
        gaussdb_libpq._libpq = library
    library.PQmakeEmptyPGresult.restype = ctypes.c_void_p
    library.PQmakeEmptyPGresult.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.PQsetResultAttrs.restype = ctypes.c_int
    library.PQsetResultAttrs.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(PGresAttDesc)]
    library.PQsetvalue.restype = ctypes.c_int
    library.PQsetvalue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return library


def build_result(library, columns, rows):
    """在进程内构造文本格式的 PGresult 并返回句柄；调用方负责 PQclear。"""
    result = library.PQmakeEmptyPGresult(None, gaussdb_libpq.PGRES_TUPLES_OK)
    attributes = (PGresAttDesc * len(columns))(*(
        PGresAttDesc(name.encode(), 0, 0, 0, oid, -1, -1) for name, oid in columns
    ))
    if not library.PQsetResultAttrs(result, len(columns), attributes):
        raise RuntimeError("PQsetResultAttrs failed")
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            ok = (library.PQsetvalue(result, row_index, column, None, -1) if value is None
                  else library.PQsetvalue(result, row_index, column, value, len(value)))
            if not ok:
                raise RuntimeError(f"PQsetvalue failed at {row_index},{column}")
    return result


class _Connection:
    _closed = False


def measure(library, columns, rows, repeat):
    """对一个用例计时；返回 {fetchall_p50, fetchall_p95, getvalue_p50}（毫秒）与取回的行。"""
    clock = time.perf_counter
    result = build_result(library, columns, rows)
    cursor = gaussdb_libpq.GaussDBCursor(_Connection())
    cursor._result = result
    try:
        fetched = cursor.fetchall()
        fetch_times, floor_times = [], []
        getvalue, cells = library.PQgetvalue, [(i, j) for i in range(len(rows)) for j in range(len(columns))]
        for _ in range(repeat):
            start = clock()
            cursor.fetchall()
            fetch_times.append((clock() - start) * 1000)
            start = clock()
            for i, j in cells:
                getvalue(result, i, j)
            floor_times.append((clock() - start) * 1000)
    finally:
        cursor.close()
    fetch_times.sort()
    return {
        "fetchall_p50": statistics.median(fetch_times),
        "fetchall_p95": fetch_times[min(len(fetch_times) - 1, int(0.95 * len(fetch_times)))],
        "getvalue_p50": statistics.median(floor_times),
    }, fetched


def main(argv=None):
    """逐用例计时并打印一行结果；返回 0。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libpq", type=Path, help="libpq shared object; default GAUSSDB_LIB_DIR/libpq.so.5")
    parser.add_argument("--repeat", type=int, default=200)
    parser.add_argument("--cases", default=",".join(CASES))
    arguments = parser.parse_args(argv)
    library = load_library(arguments.libpq)
    print(f"python {sys.version.split()[0]} libpq {arguments.libpq or os.environ.get('GAUSSDB_LIB_DIR')}")
    for name in arguments.cases.split(","):
        columns, rows = CASES[name]()
        timing, _ = measure(library, columns, rows, arguments.repeat)
        print(f"{name} rows {len(rows)} cols {len(columns)} fetchall_p50_ms {timing['fetchall_p50']:.3f} "
              f"fetchall_p95_ms {timing['fetchall_p95']:.3f} getvalue_floor_p50_ms {timing['getvalue_p50']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
