"""Declarative base: one MetaData, deterministic constraint names, column conventions.

Autogenerate cannot infer these — bigint identity PKs, TEXT rather than VARCHAR, timestamptz
everywhere — so centralising them keeps hand-review of generated revisions small.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from sqlalchemy import BigInteger, DateTime, Identity, MetaData, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Autogenerate diffs *reflected* names against these, so anything left unnamed would surface
# as a spurious drop/create on every run.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB payloads are free-form by design (data-model.md "Project settings"); Pydantic
#: validates them at the application boundary, never the database.
JsonDict = dict[str, Any]

#: BIGINT GENERATED ALWAYS AS IDENTITY — no sequence grants, no client-side allocation.
IdPk = Annotated[int, mapped_column(BigInteger, Identity(always=True), primary_key=True)]

CreatedAt = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=text("now()"))
]
#: Mutable tables only, and only ever written by the set_updated_at() trigger — never the app.
UpdatedAt = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=text("now()"))
]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012  (SQLAlchemy declares this ClassVar on the base)
        str: Text(),
        datetime: DateTime(timezone=True),
        JsonDict: JSONB(),
    }
