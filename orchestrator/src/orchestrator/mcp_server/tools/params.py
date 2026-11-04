"""The tool parameters more than one tool module takes, so a ref reads the same everywhere."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

__all__ = ["OptionalProjectArg", "ProjectArg", "TicketArg", "TicketProjectArg"]

TicketArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=300,
        description="owner/repo#123; or 123 together with `project`; or id:45, Factorial's own ID.",
    ),
]
ProjectArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="Project name, the owner/repo its issues are filed in, or id:N.",
    ),
]
OptionalProjectArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=200,
        description=(
            "Project name, owner/repo or id:N; omit for every project in this organization."
        ),
    ),
]
TicketProjectArg = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=200,
        description=(
            "Project name, owner/repo or id:N; needed only when `ticket` is a bare number."
        ),
    ),
]
