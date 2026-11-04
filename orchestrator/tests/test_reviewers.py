"""`worker/reviewers.py` — who may decide a gate."""

from __future__ import annotations

from orchestrator.config import ApproverPermission
from orchestrator.github.state import RepoPermission
from orchestrator.worker.reviewers import (
    GateReviewers,
    Ignored,
    PermissionOf,
    Qualification,
    Weighed,
    qualify,
)

APP = "factorial[bot]"


def _reviewers(
    *named: str, bot_login: str = APP, floor: ApproverPermission | None = None
) -> GateReviewers:
    return GateReviewers(
        named=frozenset(login.casefold() for login in named),
        bot_login=bot_login.casefold(),
        floor=floor,
    )


def _lookup(levels: dict[str, RepoPermission], asked: list[str]) -> PermissionOf:
    async def permission_of(login: str) -> RepoPermission:
        asked.append(login)
        return levels.get(login, "none")

    return permission_of


async def _never(login: str) -> RepoPermission:
    raise AssertionError(f"looked up {login}")


async def test_an_open_gate_counts_any_person() -> None:
    assert await qualify(_reviewers(), ["operator"], bots=(), permission_of=_never) == Weighed(
        counted={"operator": Qualification("open")}
    )


async def test_a_named_gate_counts_only_its_names_whatever_the_case() -> None:
    assert await qualify(
        _reviewers("Operator"), ["operator", "teammate"], bots=(), permission_of=_never
    ) == Weighed(
        counted={"operator": Qualification("named")}, ignored={"teammate": Ignored("not_named")}
    )


async def test_the_apps_own_review_never_counts_even_when_named() -> None:
    app_ignored = Weighed(ignored={APP: Ignored("app")})
    assert await qualify(_reviewers(APP), [APP], bots=(APP,), permission_of=_never) == app_ignored
    assert await qualify(_reviewers(), [APP], bots=(), permission_of=_never) == app_ignored


async def test_the_apps_bot_is_kept_out_by_its_type_when_its_login_is_unconfigured() -> None:
    assert await qualify(
        _reviewers(bot_login=""), [APP], bots=(APP,), permission_of=_never
    ) == Weighed(ignored={APP: Ignored("bot")})


async def test_another_bot_counts_only_when_named() -> None:
    bot = "dependabot[bot]"
    assert await qualify(_reviewers(), [bot], bots=(bot,), permission_of=_never) == Weighed(
        ignored={bot: Ignored("bot")}
    )
    assert await qualify(_reviewers(bot), [bot], bots=(bot,), permission_of=_never) == Weighed(
        counted={bot: Qualification("named")}
    )


async def test_order_is_kept_and_a_repeated_login_counts_once() -> None:
    weighed = await qualify(
        _reviewers(), ["teammate", "operator", "teammate"], bots=(), permission_of=_never
    )
    assert list(weighed.counted) == ["teammate", "operator"]


async def test_a_floor_admits_its_role_and_every_stronger_one() -> None:
    asked: list[str] = []
    levels: dict[str, RepoPermission] = {
        "operator": "admin",
        "teammate": "write",
        "teammate-2": "triage",
    }
    weighed = await qualify(
        _reviewers(floor="write"),
        ["operator", "teammate", "teammate-2"],
        bots=(),
        permission_of=_lookup(levels, asked),
    )
    assert weighed == Weighed(
        counted={
            "operator": Qualification("permission", "admin"),
            "teammate": Qualification("permission", "write"),
        },
        ignored={"teammate-2": Ignored("below_permission", "triage")},
    )


async def test_a_floor_with_nobody_named_is_not_an_open_gate() -> None:
    """The risky spot: an empty `named` under a floor means nobody is named, never anyone."""
    asked: list[str] = []
    assert await qualify(
        _reviewers(floor="write"), ["reader"], bots=(), permission_of=_lookup({}, asked)
    ) == Weighed(ignored={"reader": Ignored("below_permission", "none")})


async def test_a_named_reviewer_a_bot_and_the_app_cost_no_lookup() -> None:
    asked: list[str] = []
    levels: dict[str, RepoPermission] = {APP: "admin", "ci[bot]": "write"}
    weighed = await qualify(
        _reviewers("operator", floor="write"),
        ["operator", "ci[bot]", APP],
        bots=("ci[bot]",),
        permission_of=_lookup(levels, asked),
    )
    assert weighed == Weighed(
        counted={"operator": Qualification("named")},
        ignored={"ci[bot]": Ignored("bot"), APP: Ignored("app")},
    )
    assert asked == []
