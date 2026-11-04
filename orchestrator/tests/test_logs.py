"""Service logging (tech-stack decision 81): the record's shape, the bound fields, redaction, and
what `configure_logging` does to a process.

The formatters are tested on records made here. `configure_logging` replaces the root logger's
handlers, so it runs in a child process, never in the test runner's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from orchestrator.logs import (
    FIELDS,
    JsonFormatter,
    TextFormatter,
    bind,
    keep,
    note,
    redact,
    revert_on_error,
)


def _record(msg: str, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise FileNotFoundError("state.json")
        except FileNotFoundError:
            exc_info = sys.exc_info()
    return logging.LogRecord("orchestrator.x", logging.WARNING, __file__, 1, msg, args, exc_info)


def _json(record: logging.LogRecord) -> dict[str, object]:
    line = JsonFormatter("worker").format(record)
    assert "\n" not in line
    return json.loads(line)


def test_a_record_is_one_json_object_with_its_traceback_inside() -> None:
    out = _json(_record("run %s: session log offset unread", 9312, exc=True))
    assert out["level"] == "WARNING"
    assert out["service"] == "worker"
    assert out["logger"] == "orchestrator.x"
    assert out["msg"] == "run 9312: session log offset unread"
    assert str(out["ts"]).endswith("Z")
    assert "FileNotFoundError: state.json" in str(out["exc"])


def test_bound_fields_reach_the_record_and_leave_with_the_block() -> None:
    with bind(ticket_id=481, step="build"), bind(step_run_id=9312, attempt=None):
        out = _json(_record("inside"))
    assert {k: out[k] for k in ("ticket_id", "step", "step_run_id")} == {
        "ticket_id": 481,
        "step": "build",
        "step_run_id": 9312,
    }
    assert "attempt" not in out
    assert not FIELDS & _json(_record("outside")).keys()


@pytest.mark.parametrize(
    "name", ["asyncio", "sqlalchemy.pool.impl.AsyncAdaptedQueuePool", "py.warnings"]
)
def test_records_written_outside_their_task_carry_no_ids(name: str) -> None:
    """'Task exception was never retrieved' is logged by a garbage collection, on whichever
    ticket's task happens to be running."""
    record = logging.LogRecord(name, logging.ERROR, __file__, 1, "collected", (), None)
    with bind(ticket_id=481):
        assert "ticket_id" not in _json(record)
        assert "ticket_id=481" not in TextFormatter().format(record)


def test_a_similar_name_keeps_its_ids() -> None:
    record = logging.LogRecord("asyncio_helpers", logging.ERROR, __file__, 1, "x", (), None)
    with bind(ticket_id=481):
        assert _json(record)["ticket_id"] == 481


def test_a_block_that_raises_still_unbinds() -> None:
    with pytest.raises(RuntimeError), bind(ticket_id=1):
        raise RuntimeError
    assert "ticket_id" not in _json(_record("after"))


def test_a_name_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(ValueError, match="not a log field: token"), bind(token="x"):
        pass
    with pytest.raises(ValueError, match="not a log field: token"):
        note(token="x")


def test_a_note_lasts_until_the_enclosing_block_ends() -> None:
    """The worker learns a run's id halfway through a ticket's block."""
    with bind(ticket_id=1):
        note(step_run_id=7)
        assert _json(_record("inside"))["step_run_id"] == 7
    assert not FIELDS & _json(_record("after")).keys()


def test_a_none_note_clears_the_field() -> None:
    with bind(ticket_id=1, step_run_id=7):
        note(step_run_id=None)
        out = _json(_record("inside"))
    assert out["ticket_id"] == 1
    assert "step_run_id" not in out


def test_bind_none_hides_an_outer_field_for_its_block() -> None:
    with bind(step_run_id=7):
        with bind(step_run_id=None):
            assert "step_run_id" not in _json(_record("inner"))
        assert _json(_record("outer"))["step_run_id"] == 7


def test_notes_in_a_block_that_raises_are_undone() -> None:
    """A tick's act phase notes a run its rollback then erases."""
    with bind(ticket_id=1):
        note(step="build")
        with pytest.raises(RuntimeError), revert_on_error():
            note(step="review", step_run_id=7)
            raise RuntimeError
        out = _json(_record("after"))
    assert out["step"] == "build"
    assert "step_run_id" not in out


def test_notes_in_a_block_that_succeeds_are_kept() -> None:
    with bind(ticket_id=1):
        note(step="build")
        with revert_on_error():
            note(step="review", step_run_id=7)
        out = _json(_record("after"))
    assert (out["step"], out["step_run_id"]) == ("review", 7)


def test_kept_fields_survive_a_revert() -> None:
    """A run the tick polled existed before the act phase, so its rollback keeps the id."""
    with bind(ticket_id=1):
        with pytest.raises(RuntimeError), revert_on_error():
            keep(step_run_id=7, attempt=1)
            note(step="review")
            raise RuntimeError
        out = _json(_record("after"))
    assert (out["step_run_id"], out["attempt"]) == (7, 1)
    assert "step" not in out


def test_a_note_over_a_kept_field_is_reverted_too() -> None:
    """A retry inserts attempt 2 over the attempt 1 it read; a failed spawn erases attempt 2."""
    with bind(ticket_id=1):
        with pytest.raises(RuntimeError), revert_on_error():
            keep(step_run_id=7, attempt=1)
            note(step_run_id=8, attempt=2)
            raise RuntimeError
        out = _json(_record("after"))
    assert not {"step_run_id", "attempt"} & out.keys()


async def test_concurrent_tickets_never_see_each_others_fields() -> None:
    """The worker reconciles tickets with `asyncio.gather`, one task each."""

    async def one(ticket_id: int) -> object:
        with bind(ticket_id=ticket_id):
            await asyncio.sleep(0)
            return _json(_record("tick"))["ticket_id"]

    assert await asyncio.gather(one(1), one(2), one(3)) == [1, 2, 3]


@pytest.mark.parametrize(
    ("secret", "masked"),
    [
        ("ghs_" + "a" * 36, "[redacted]"),
        ("ghp_" + "B" * 36, "[redacted]"),
        ("github_pat_" + "11ABC_" + "x" * 40, "[redacted]"),
        ("sk-ant-api03-" + "k" * 40, "[redacted]"),
        ("sk-proj-" + "z" * 40, "[redacted]"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJl", "[redacted]"),  # gitleaks:allow
        ("Authorization: Bearer abcdefghijklmnopqrstuvwx", "Authorization: Bearer [redacted]"),
        (
            "https://x-access-token:ghx-not-a-pattern@github.com/o/r.git",
            "https://x-access-token:[redacted]@github.com/o/r.git",
        ),
        (
            "postgresql+asyncpg://factory:pw@db.example/factory",
            "postgresql+asyncpg://factory:[redacted]@db.example/factory",
        ),
        (
            "bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29tLz9BY3Rpb249abc+def/ghi==",  # gitleaks:allow  # noqa: E501
            "[redacted]",
        ),
        ("glpat-" + "x" * 20, "[redacted]"),
        ("e2b_" + "0123456789abcdef" * 2 + "01234567", "[redacted]"),
    ],
)
def test_credentials_are_masked_in_the_message_and_the_traceback(secret: str, masked: str) -> None:
    assert redact(f"cloning {secret} failed") == f"cloning {masked} failed"
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "%s", (secret,), sys.exc_info())
    out = _json(record)
    assert out["msg"] == masked
    assert masked in str(out["exc"])
    assert secret not in json.dumps(out)
    assert secret not in TextFormatter().format(record)


@pytest.mark.parametrize(
    "plain",
    [
        "input_tokens=5120 output_tokens=88",
        "branch spec/42-sk-short",
        "http://localhost:8000/factorial/api/orgs/default",
        "tasks 3 of 4",
        "orchestrator.e2b_template",
        "AWS_BEARER_TOKEN_BEDROCK is unset",
        "glpat- prefix",
    ],
)
def test_ordinary_text_passes_unmasked(plain: str) -> None:
    assert redact(plain) == plain


def _raised(exc: BaseException) -> logging.LogRecord:
    try:
        raise exc
    except BaseException:
        return logging.LogRecord("x", logging.ERROR, __file__, 1, "tick", (), sys.exc_info())


def test_a_long_traceback_keeps_its_ends_and_fits_one_docker_line() -> None:
    """Docker splits a line over 16 KiB, and awslogs stores the parts as events of their own."""
    try:
        try:
            raise ValueError("é" * 20_000)
        except ValueError as cause:
            raise RuntimeError("the tick failed") from cause
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "tick", (), sys.exc_info())
    line = JsonFormatter("worker").format(record)
    assert len(line.encode()) <= 16_000
    exc = str(json.loads(line)["exc"])
    assert exc.startswith("Traceback (most recent call last)")
    assert "characters cut ...]" in exc
    assert exc.endswith("RuntimeError: the tick failed")


def test_a_secret_in_a_cut_traceback_stays_masked() -> None:
    """Redaction runs before the cut, so a cut through a token leaves no part of it."""
    tokens = " ".join(["ghs_" + "a" * 36] * 2000)
    line = JsonFormatter("worker").format(_raised(RuntimeError(tokens)))
    assert len(line.encode()) <= 16_000
    assert "ghs_" not in line
    assert "a" * 8 not in line


def test_a_long_message_leaves_room_for_the_traceback() -> None:
    """The worker puts an exception's text in the message too, so both can be long."""
    text = "x" * 20_000
    record = _raised(RuntimeError(text))
    record.msg, record.args = "evaluation failed (%s)", (text,)
    line = JsonFormatter("worker").format(record)
    assert len(line.encode()) <= 16_000
    exc = str(json.loads(line)["exc"])
    assert exc.startswith("Traceback (most recent call last)")
    assert "in _raised" in exc
    assert "RuntimeError: x" in exc


def test_a_long_message_is_cut_too() -> None:
    line = JsonFormatter("worker").format(_record("m" * 30_000))
    assert len(line.encode()) <= 16_000
    assert "characters cut" in str(json.loads(line)["msg"])


def test_text_keeps_the_old_format_and_adds_the_bound_fields() -> None:
    with bind(ticket_id=481, step_run_id=9312):
        line = TextFormatter().format(_record("run %s: unread", 9312))
    assert " WARNING  orchestrator.x: run 9312: unread [ticket_id=481 step_run_id=9312]" in line


def _child(code: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter, from an empty directory so no `.env` is read. The child
    imports what this process imports, `PYTHONPATH` included."""
    inherited = {k: v for k, v in os.environ.items() if k == "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", **inherited, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_configure_logging_writes_json_to_stderr_and_takes_over_uvicorn(tmp_path: Path) -> None:
    done = _child(
        """
        import logging
        from orchestrator.logs import configure_logging
        logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler())
        logging.getLogger("uvicorn.access").propagate = False
        configure_logging("api")
        logging.getLogger("orchestrator.x").debug("hidden at INFO")
        logging.getLogger("orchestrator.x").info("hello")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
            "/factorial/pages/7?blob=ab&expires=1&signature=cd", "1.1", 200,
        )
        """,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    lines = [json.loads(line) for line in done.stderr.splitlines()]
    assert [line["msg"] for line in lines] == [
        "hello",
        '127.0.0.1:5 - "GET /factorial/pages/7 HTTP/1.1" 200',
    ]
    assert {line["service"] for line in lines} == {"api"}


def test_a_query_string_never_reaches_the_log(tmp_path: Path) -> None:
    """A signed link's signature is in its query, and a client may send a raw quote there."""
    done = _child(
        """
        import logging
        from orchestrator.logs import configure_logging
        configure_logging("api")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
            '/factorial/pages/7?a="x&signature=cd', "1.1", 200,
        )
        logging.getLogger("uvicorn.error").info(
            '%s - "WebSocket %s" [accepted]', "127.0.0.1:5", "/factorial/ws?ticket=abc"
        )
        """,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert [json.loads(line)["msg"] for line in done.stderr.splitlines()] == [
        '127.0.0.1:5 - "GET /factorial/pages/7 HTTP/1.1" 200',
        '127.0.0.1:5 - "WebSocket /factorial/ws" [accepted]',
    ]
    assert "signature" not in done.stderr
    assert "ticket=abc" not in done.stderr


def test_the_level_and_the_text_format_come_from_the_environment(tmp_path: Path) -> None:
    done = _child(
        """
        import logging
        from orchestrator.logs import configure_logging
        configure_logging("worker")
        logging.getLogger("x").info("hidden at WARNING")
        logging.getLogger("x").warning("shown")
        """,
        tmp_path,
        FACTORIAL_LOG_LEVEL="warning",
        FACTORIAL_LOG_FORMAT="TEXT",
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr.endswith(" WARNING  x: shown\n")
    assert "hidden" not in done.stderr


def test_an_unknown_format_stops_the_service(tmp_path: Path) -> None:
    done = _child(
        "from orchestrator.logs import configure_logging; configure_logging('worker')",
        tmp_path,
        FACTORIAL_LOG_FORMAT="yaml",
    )
    assert done.returncode != 0
    assert "FACTORIAL_LOG_FORMAT must be json or text, not 'yaml'" in done.stderr


def test_only_the_uvicorn_target_configures_the_api(tmp_path: Path) -> None:
    """Tests import `api/main.py`; configuring logging there would replace pytest's handlers."""
    done = _child(
        """
        import logging
        import orchestrator.api.main
        from orchestrator.logs import JsonFormatter
        def configured():
            return any(isinstance(h.formatter, JsonFormatter) for h in logging.getLogger().handlers)
        assert not configured(), "importing main configured logging"
        import orchestrator.api.serve
        assert configured(), "the uvicorn target did not configure logging"
        """,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr


def test_an_uncaught_error_is_one_json_record(tmp_path: Path) -> None:
    done = _child(
        """
        from orchestrator.logs import configure_logging
        configure_logging("worker")
        raise RuntimeError("boom")
        """,
        tmp_path,
    )
    assert done.returncode == 1
    (line,) = done.stderr.splitlines()
    out = json.loads(line)
    assert (out["level"], out["msg"]) == ("CRITICAL", "uncaught exception; the service stops")
    assert str(out["exc"]).endswith("RuntimeError: boom")


def test_ctrl_c_keeps_pythons_own_output(tmp_path: Path) -> None:
    done = _child(
        """
        from orchestrator.logs import configure_logging
        configure_logging("worker")
        raise KeyboardInterrupt
        """,
        tmp_path,
    )
    assert done.stderr.startswith("Traceback")
    assert "KeyboardInterrupt" in done.stderr


@pytest.mark.parametrize(
    "start",
    ["import orchestrator.api.serve", "from orchestrator.worker.__main__ import main; main()"],
)
def test_settings_that_fail_to_load_are_logged_as_json(start: str, tmp_path: Path) -> None:
    done = _child(start, tmp_path, FACTORIAL_WORKER__BATCH_SIZE="abc")
    assert done.returncode != 0
    lines = [json.loads(line) for line in done.stderr.splitlines()]
    assert "batch_size" in str(lines[-1]["exc"])


def test_a_warning_is_a_json_record(tmp_path: Path) -> None:
    done = _child(
        """
        import warnings
        from orchestrator.logs import configure_logging
        configure_logging("worker")
        warnings.warn("old", UserWarning)
        """,
        tmp_path,
    )
    assert done.returncode == 0, done.stderr
    (line,) = done.stderr.splitlines()
    out = json.loads(line)
    assert (out["logger"], out["level"]) == ("py.warnings", "WARNING")
