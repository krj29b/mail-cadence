import contextlib
import io
import json
import logging
from collections.abc import Iterator
from typing import Literal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from mail_cadence.cli import JsonFormatter, setup_logging
from mail_cadence.models import Settings


def test_json_formatter_fields() -> None:
    formatter = JsonFormatter()
    logger = logging.getLogger("test_json_logger")
    logger.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    try:
        logger.info("Hello Zażółć world")
        output = stream.getvalue().strip()
        data = json.loads(output)

        assert set(data.keys()) == {"timestamp", "level", "logger", "message"}
        assert data["level"] == "INFO"
        assert data["logger"] == "test_json_logger"
        assert data["message"] == "Hello Zażółć world"
        assert "+00:00" in data["timestamp"] or data["timestamp"].endswith("Z")
        assert "exception" not in data
    finally:
        logger.removeHandler(handler)


def test_json_formatter_with_exception() -> None:
    formatter = JsonFormatter()
    logger = logging.getLogger("test_exc_logger")
    logger.setLevel(logging.DEBUG)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    try:
        try:
            raise ValueError("Something failed")
        except ValueError:
            logger.error("An error occurred", exc_info=True)

        output = stream.getvalue().strip()
        data = json.loads(output)

        assert "exception" in data
        assert "ValueError: Something failed" in data["exception"]
        assert data["message"] == "An error occurred"
    finally:
        logger.removeHandler(handler)


@contextlib.contextmanager
def bare_root_logger() -> Iterator[logging.Logger]:
    """The root logger without handlers, as in a fresh CLI process; restored afterwards.

    Used inside the test body: pytest adds its capture handlers to the root logger when
    the test call starts, after fixtures have run.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


@pytest.mark.parametrize("log_format", ["text", "json"])
def test_setup_logging_writes_the_chosen_format(
    capsys: pytest.CaptureFixture[str], log_format: Literal["text", "json"]
) -> None:
    with bare_root_logger():
        setup_logging(verbose=False, log_format=log_format)
        logging.getLogger("mail_cadence.test").info("Log message Zażółć")

    line = capsys.readouterr().out.strip()
    if log_format == "json":
        data = json.loads(line)
        assert data["message"] == "Log message Zażółć"
        assert data["level"] == "INFO"
    else:
        assert line.endswith("[INFO] mail_cadence.test: Log message Zażółć")


def test_setup_logging_keeps_the_handlers_of_an_embedding_application() -> None:
    with bare_root_logger() as root:
        host_handler = logging.StreamHandler(io.StringIO())
        root.addHandler(host_handler)

        setup_logging(log_format="json")

        assert root.handlers == [host_handler]


def test_settings_log_format_validation() -> None:
    s_default = Settings()
    assert s_default.log_format == "text"

    s_json = Settings(log_format="json")
    assert s_json.log_format == "json"

    s_json_caps = Settings(log_format="JSON")  # type: ignore[arg-type]
    assert s_json_caps.log_format == "json"

    s_text_caps = Settings(log_format="Text")  # type: ignore[arg-type]
    assert s_text_caps.log_format == "text"

    with pytest.raises(ValidationError):
        Settings(log_format="xml")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_dispatch_pipeline_loads_json_logging(tmp_path: object) -> None:
    from mail_cadence.cli import run_dispatch_pipeline

    db_file = str(tmp_path) + "/test_log.db"
    with (
        patch.dict("os.environ", {"LOG_FORMAT": "json"}),
        patch("mail_cadence.cli.setup_logging") as mock_setup,
    ):
        code = await run_dispatch_pipeline(db_path=db_file, dry_run=True)
        assert code == 0
        mock_setup.assert_called_once()
        _, kwargs = mock_setup.call_args
        assert kwargs.get("log_format") == "json"
