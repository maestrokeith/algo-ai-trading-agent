"""Custom SQLAlchemy type for TiDB VECTOR columns.

TiDB Serverless supports ``VECTOR(n)`` as a native column type with HNSW
indexing.  SQLite (used for CI tests) does not — so this type renders as
``TEXT`` when the dialect is SQLite, storing the vector as a
``[f1,f2,...]`` string identical to TiDB's wire format.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, UserDefinedType


class TiDBVector(TypeDecorator):
    """Store a list of floats as a TiDB VECTOR(dim) column.

    On TiDB the column DDL is ``VECTOR(dim)``.
    On SQLite  the column DDL is ``TEXT`` (JSON array string).
    """

    impl = Text
    cache_ok = True

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name in ("mysql", "tidb"):
            return dialect.type_descriptor(_TiDBVectorNative(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if dialect.name in ("mysql", "tidb"):
            # TiDB wire format: [1.0,2.0,...]
            return "[" + ",".join(str(float(v)) for v in value) + "]"
        return json.dumps(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        # Both TiDB and SQLite return a JSON-array-like string
        return json.loads(value)


class _TiDBVectorNative(UserDefinedType):
    """Raw DDL type that renders as ``VECTOR(dim)`` on TiDB/MySQL."""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:  # noqa: ANN401
        return f"VECTOR({self.dim})"
