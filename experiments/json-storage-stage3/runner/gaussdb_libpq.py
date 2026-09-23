"""GaussDB/XStore libpq ctypes wrapper providing psycopg-compatible connection interface.

GaussDB uses a custom SASL authentication mechanism not supported by standard
libpq (which psycopg bundles). This module wraps GaussDB's own libpq via ctypes
to provide a minimal connection/cursor API compatible with the OpenGauss adapter.
"""

import ctypes
import json
import os
from pathlib import Path

# GaussDB 的 libpq 位置随部署而变，由环境提供；导入本模块不加载动态库，
# 使不连接数据库的调用方可以正常导入。
_GAUSS_LIB_DIR = os.environ.get("GAUSSDB_LIB_DIR", "")
_GAUSS_SERVER_LIB_DIR = os.environ.get("GAUSSDB_SERVER_LIB_DIR", "")
_libpq = None


def _bind_signatures(_libpq):
    """绑定所用 libpq 函数的返回类型与参数类型。"""
    _libpq.PQputCopyData.restype = ctypes.c_int
    _libpq.PQputCopyData.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _libpq.PQputCopyEnd.restype = ctypes.c_int
    _libpq.PQputCopyEnd.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _libpq.PQgetCopyData.restype = ctypes.c_int
    _libpq.PQgetCopyData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]
    _libpq.PQexec.restype = ctypes.c_void_p
    _libpq.PQconnectdb.restype = ctypes.c_void_p
    _libpq.PQconnectdb.argtypes = [ctypes.c_char_p]
    _libpq.PQfinish.restype = None
    _libpq.PQfinish.argtypes = [ctypes.c_void_p]
    _libpq.PQstatus.restype = ctypes.c_int
    _libpq.PQstatus.argtypes = [ctypes.c_void_p]
    _libpq.PQerrorMessage.restype = ctypes.c_char_p
    _libpq.PQerrorMessage.argtypes = [ctypes.c_void_p]
    _libpq.PQexec.restype = ctypes.c_void_p
    _libpq.PQexec.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _libpq.PQexecParams.restype = ctypes.c_void_p
    _libpq.PQexecParams.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_int,
    ]
    _libpq.PQresultStatus.restype = ctypes.c_int
    _libpq.PQresultStatus.argtypes = [ctypes.c_void_p]
    _libpq.PQresultErrorMessage.restype = ctypes.c_char_p
    _libpq.PQresultErrorMessage.argtypes = [ctypes.c_void_p]
    _libpq.PQntuples.restype = ctypes.c_int
    _libpq.PQntuples.argtypes = [ctypes.c_void_p]
    _libpq.PQnfields.restype = ctypes.c_int
    _libpq.PQnfields.argtypes = [ctypes.c_void_p]
    _libpq.PQfname.restype = ctypes.c_char_p
    _libpq.PQfname.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpq.PQftype.restype = ctypes.c_uint
    _libpq.PQftype.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _libpq.PQgetvalue.restype = ctypes.c_char_p
    _libpq.PQgetvalue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _libpq.PQgetisnull.restype = ctypes.c_int
    _libpq.PQgetisnull.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _libpq.PQclear.restype = None
    _libpq.PQclear.argtypes = [ctypes.c_void_p]
    _libpq.PQcmdTuples.restype = ctypes.c_char_p
    _libpq.PQcmdTuples.argtypes = [ctypes.c_void_p]


def _load_libpq():
    """按 GAUSSDB_LIB_DIR 加载 libpq 并绑定签名，返回库句柄；缺失时给出明确原因。"""
    global _libpq
    if _libpq is not None:
        return _libpq
    if not _GAUSS_LIB_DIR:
        raise RuntimeError("GAUSSDB_LIB_DIR must point at the directory holding libpq.so.5")
    for directory in (_GAUSS_LIB_DIR, _GAUSS_SERVER_LIB_DIR):
        if directory and directory not in os.environ.get("LD_LIBRARY_PATH", ""):
            os.environ["LD_LIBRARY_PATH"] = directory + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    library_path = Path(_GAUSS_LIB_DIR) / "libpq.so.5"
    if not library_path.is_file():
        raise RuntimeError(f"GaussDB libpq is missing: {library_path}")
    library = ctypes.cdll.LoadLibrary(str(library_path))
    _bind_signatures(library)
    _libpq = library
    return _libpq

# Constants
PGRES_EMPTY_QUERY = 0
PGRES_COMMAND_OK = 1
PGRES_TUPLES_OK = 2
PGRES_COPY_IN = 4
PGRES_FATAL_ERROR = 7

_CONNECTION_OK = 0

# Function signatures
# COPY support

# Transaction control

# Autocommit via PQexec — no direct API, use "SET AUTOCOMMIT" or BEGIN/COMMIT


class GaussDBError(Exception):
    """GaussDB connection or query error."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _to_bytes(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


def _from_bytes(value):
    if value is None:
        return None
    return value.decode("utf-8")


def _parse_timestamp(value):
    """Parse a PostgreSQL text timestamp into a UTC timezone-aware datetime.

    GaussDB returns timestamps in the server's local timezone (e.g. +08:00).
    We convert to UTC so the adapter's _timestamp() produces the expected 'Z' suffix.
    """
    from datetime import datetime, timezone
    s = value.strip()
    # Try to parse with timezone
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            # If no timezone, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            # Convert to UTC so isoformat produces "+00:00" → replaced to "Z"
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    # Fallback: try with space replaced by T for ISO format
    iso_s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(iso_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Last resort: return the string as-is
    return value


def _parse_date(value):
    """Parse a PostgreSQL text date into a date object."""
    from datetime import datetime
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return value


def _format_param(value):
    """Convert a Python value to its PostgreSQL text representation."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    # datetime/date objects
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class GaussDBCursor:
    """Minimal cursor compatible with psycopg's cursor interface."""

    def __init__(self, connection):
        self._conn = connection
        self._result = None
        self._rowcount = -1
        self._closed = False

    def _check_open(self):
        if self._closed:
            raise GaussDBError("cursor is closed")
        if self._conn._closed:
            raise GaussDBError("connection is closed")

    @property
    def rowcount(self):
        return self._rowcount

    def execute(self, statement, params=None):
        """Execute a SQL statement with optional parameter binding."""
        self._check_open()
        if self._result is not None:
            _libpq.PQclear(self._result)
            self._result = None

        if params is not None:
            # GaussDB's PQexecParams does not handle NULL paramValues pointers
            # correctly (causes "invalid null pointer input for text_to_cstring()").
            # Inline NULL parameters as SQL NULL literals and only bind non-NULL
            # values via PQexecParams.
            converted = ""
            param_idx = 1
            non_null_params = []
            parts = statement.split("%s")
            for part_idx, part in enumerate(parts):
                converted += part
                if part_idx < len(params):
                    value = params[part_idx]
                    if value is None:
                        converted += "NULL"
                    else:
                        converted += f"${param_idx}"
                        param_idx += 1
                        non_null_params.append(value)
            if non_null_params:
                param_values = [_to_bytes(_format_param(p)) for p in non_null_params]
                n_params = len(param_values)
                param_arr = (ctypes.c_char_p * n_params)(*param_values)
                result = _libpq.PQexecParams(
                    self._conn._pg_conn,
                    _to_bytes(converted),
                    n_params,
                    None,  # paramTypes: let server infer
                    param_arr,
                    None,  # paramLengths: text mode, null-terminated
                    None,  # paramFormats: text
                    0,  # resultFormat: text
                )
            else:
                # All parameters were NULL — no binding needed
                result = _libpq.PQexec(self._conn._pg_conn, _to_bytes(converted))
        else:
            result = _libpq.PQexec(self._conn._pg_conn, _to_bytes(statement))

        if not result:
            raise GaussDBError(_from_bytes(_libpq.PQerrorMessage(self._conn._pg_conn)))

        status = _libpq.PQresultStatus(result)
        if status == PGRES_FATAL_ERROR:
            msg = _from_bytes(_libpq.PQresultErrorMessage(result))
            _libpq.PQclear(result)
            raise GaussDBError(msg)

        self._result = result
        if status == PGRES_TUPLES_OK:
            self._rowcount = _libpq.PQntuples(result)
        elif status == PGRES_COMMAND_OK:
            cmd_tuples = _from_bytes(_libpq.PQcmdTuples(result))
            try:
                self._rowcount = int(cmd_tuples) if cmd_tuples else -1
            except ValueError:
                self._rowcount = -1
        else:
            self._rowcount = -1

        return self

    def executemany(self, statement, params_list):
        """Execute a statement with multiple parameter sets."""
        for params in params_list:
            self.execute(statement, params)

    def fetchall(self):
        """Return all rows as tuples, converting typed columns to Python types."""
        self._check_open()
        if self._result is None:
            return []
        n_tuples = _libpq.PQntuples(self._result)
        n_fields = _libpq.PQnfields(self._result)
        # Determine column types
        bool_fields = set()
        ts_fields = set()
        date_fields = set()
        int_fields = set()
        float_fields = set()
        for j in range(n_fields):
            ftype = _libpq.PQftype(self._result, j)
            if ftype == 16:  # BOOLOID
                bool_fields.add(j)
            elif ftype in (1184, 1114):  # TIMESTAMPTZOID, TIMESTAMPOID
                ts_fields.add(j)
            elif ftype == 1082:  # DATEOID
                date_fields.add(j)
            elif ftype in (20, 21, 23):  # INT8OID, INT2OID, INT4OID
                int_fields.add(j)
            elif ftype in (700, 701):  # FLOAT4OID, FLOAT8OID
                float_fields.add(j)
        rows = []
        for i in range(n_tuples):
            row = []
            for j in range(n_fields):
                if _libpq.PQgetisnull(self._result, i, j):
                    row.append(None)
                elif j in bool_fields:
                    val = _from_bytes(_libpq.PQgetvalue(self._result, i, j))
                    row.append(val == "t")
                elif j in ts_fields:
                    val = _from_bytes(_libpq.PQgetvalue(self._result, i, j))
                    row.append(_parse_timestamp(val))
                elif j in date_fields:
                    val = _from_bytes(_libpq.PQgetvalue(self._result, i, j))
                    row.append(_parse_date(val))
                elif j in int_fields:
                    val = _from_bytes(_libpq.PQgetvalue(self._result, i, j))
                    row.append(int(val))
                elif j in float_fields:
                    val = _from_bytes(_libpq.PQgetvalue(self._result, i, j))
                    row.append(float(val))
                else:
                    row.append(_from_bytes(_libpq.PQgetvalue(self._result, i, j)))
            rows.append(tuple(row))
        return rows

    def fetchone(self):
        """Return a single row or None."""
        rows = self.fetchall()
        return rows[0] if rows else None

    def copy(self, statement):
        """Execute a COPY statement and return a copy context manager."""
        self._check_open()
        if self._result is not None:
            _libpq.PQclear(self._result)
            self._result = None

        result = _libpq.PQexec(self._conn._pg_conn, _to_bytes(statement))
        if not result:
            raise GaussDBError(_from_bytes(_libpq.PQerrorMessage(self._conn._pg_conn)))

        status = _libpq.PQresultStatus(result)
        if status != PGRES_COPY_IN:
            msg = _from_bytes(_libpq.PQresultErrorMessage(result))
            _libpq.PQclear(result)
            raise GaussDBError(f"COPY failed: {msg}")

        _libpq.PQclear(result)
        return _CopyContext(self._conn)

    def close(self):
        if self._result is not None:
            _libpq.PQclear(self._result)
            self._result = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class _CopyContext:
    """Context manager for COPY ... FROM STDIN operations."""

    def __init__(self, connection):
        self._conn = connection

    def write_row(self, values):
        """Write a single row as CSV (tab-separated)."""
        parts = []
        for v in values:
            if v is None:
                parts.append("\\N")
            elif isinstance(v, str) and v == "":
                # GaussDB treats empty strings as NULL; use \N explicitly
                parts.append("\\N")
            elif isinstance(v, bytes):
                text = v.decode("utf-8")
                if text == "":
                    parts.append("\\N")
                else:
                    parts.append(text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))
            elif isinstance(v, str):
                parts.append(v.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r"))
            elif isinstance(v, bool):
                parts.append("true" if v else "false")
            else:
                parts.append(str(v))
        line = "\t".join(parts) + "\n"
        data = line.encode("utf-8")
        ret = _libpq.PQputCopyData(self._conn._pg_conn, data, len(data))
        if ret == -1:
            raise GaussDBError(_from_bytes(_libpq.PQerrorMessage(self._conn._pg_conn)))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # Abort the COPY
            _libpq.PQputCopyEnd(self._conn._pg_conn, b"copy cancelled due to error")
        else:
            ret = _libpq.PQputCopyEnd(self._conn._pg_conn, None)
            if ret == -1:
                raise GaussDBError(_from_bytes(_libpq.PQerrorMessage(self._conn._pg_conn)))
        # After PQputCopyEnd, get the COPY result via PQgetResult
        _libpq.PQgetResult.restype = ctypes.c_void_p
        _libpq.PQgetResult.argtypes = [ctypes.c_void_p]
        result = _libpq.PQgetResult(self._conn._pg_conn)
        if result:
            status = _libpq.PQresultStatus(result)
            if status == PGRES_FATAL_ERROR:
                msg = _from_bytes(_libpq.PQresultErrorMessage(result))
                _libpq.PQclear(result)
                raise GaussDBError(msg)
            _libpq.PQclear(result)
        return False


class GaussDBConnection:
    """Minimal connection compatible with psycopg's connection interface."""

    def __init__(self, conninfo):
        self._pg_conn = _libpq.PQconnectdb(_to_bytes(conninfo))
        if not self._pg_conn:
            raise GaussDBError("PQconnectdb returned NULL")
        if _libpq.PQstatus(self._pg_conn) != _CONNECTION_OK:
            err = _from_bytes(_libpq.PQerrorMessage(self._pg_conn))
            _libpq.PQfinish(self._pg_conn)
            raise GaussDBError(f"connection failed: {err}")
        self._closed = False
        self.autocommit = False
        self._in_transaction = False

    @property
    def closed(self):
        return self._closed

    def cursor(self):
        if self._closed:
            raise GaussDBError("connection is closed")
        return GaussDBCursor(self)

    def execute(self, statement, params=None):
        """Shortcut: create cursor, execute, return cursor."""
        cursor = self.cursor()
        cursor.execute(statement, params)
        return cursor

    def commit(self):
        if self._closed or self.autocommit or not self._in_transaction:
            return
        result = _libpq.PQexec(self._pg_conn, b"COMMIT")
        if result:
            status = _libpq.PQresultStatus(result)
            _libpq.PQclear(result)
            if status == PGRES_FATAL_ERROR:
                raise GaussDBError(_from_bytes(_libpq.PQerrorMessage(self._pg_conn)))
        self._in_transaction = False

    def rollback(self):
        if self._closed or self.autocommit or not self._in_transaction:
            return
        result = _libpq.PQexec(self._pg_conn, b"ROLLBACK")
        if result:
            _libpq.PQclear(result)
        self._in_transaction = False

    def transaction(self):
        """Return a context manager for transaction control."""
        self._start_transaction()
        return _TransactionContext(self)

    def _start_transaction(self):
        if not self.autocommit and not self._in_transaction:
            result = _libpq.PQexec(self._pg_conn, b"BEGIN")
            if result:
                _libpq.PQclear(result)
            self._in_transaction = True

    def close(self):
        if self._closed:
            return
        if self._in_transaction:
            self.rollback()
        _libpq.PQfinish(self._pg_conn)
        self._pg_conn = None
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class _TransactionContext:
    """Context manager for database transactions."""

    def __init__(self, connection):
        self._conn = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._conn.rollback()
        else:
            self._conn.commit()
        return False


def connect(conninfo=None, **kwargs):
    """Create a GaussDB connection, compatible with psycopg.connect."""
    _load_libpq()
    if conninfo is None:
        parts = []
        for key, value in kwargs.items():
            parts.append(f"{key}={value}")
        conninfo = " ".join(parts)
    elif kwargs:
        # Merge conninfo string with keyword args
        parts = [conninfo]
        for key, value in kwargs.items():
            parts.append(f"{key}={value}")
        conninfo = " ".join(parts)
    return GaussDBConnection(conninfo)
