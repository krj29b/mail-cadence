"""Item 4: limit on failed web panel logins."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mail_cadence.login_throttle import LoginThrottle
from mail_cadence.models import Settings
from mail_cadence.web import create_app

USER, PASSWORD = "admin", "correct-horse"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- LoginThrottle ------------------------------------------------------------------------


def test_blocks_after_max_failures_and_reports_retry_after() -> None:
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=clock)
    assert [throttle.record_failure("1.2.3.4") for _ in range(3)] == [False, False, True]
    assert throttle.retry_after("1.2.3.4") == 60
    clock.now += 45
    assert throttle.retry_after("1.2.3.4") == 15
    assert throttle.retry_after("5.6.7.8") is None  # other clients are not affected


def test_sliding_window_allows_one_attempt_per_expired_failure() -> None:
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=clock)
    throttle.record_failure("c")
    clock.now += 30
    throttle.record_failure("c")
    assert throttle.retry_after("c") == 30
    clock.now += 30  # first failure leaves the window
    assert throttle.retry_after("c") is None
    throttle.record_failure("c")
    assert throttle.retry_after("c") == 30


def test_reset_forgets_failures() -> None:
    throttle = LoginThrottle(max_failures=1, window_seconds=60, clock=FakeClock())
    throttle.record_failure("c")
    assert throttle.retry_after("c") is not None
    throttle.reset("c")
    assert throttle.retry_after("c") is None


def test_tracked_clients_are_bounded() -> None:
    throttle = LoginThrottle(max_failures=1, window_seconds=60, max_clients=2, clock=FakeClock())
    for client in ("a", "b", "c"):
        throttle.record_failure(client)
    assert throttle.retry_after("a") is None  # least recent client was dropped
    assert throttle.retry_after("b") is not None
    assert throttle.retry_after("c") is not None


def test_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        LoginThrottle(max_failures=0)


# --- web panel ----------------------------------------------------------------------------


@pytest.fixture
def app_and_clock(tmp_path: Path) -> tuple[TestClient, FakeClock]:
    settings = Settings(
        admin_user=USER,
        admin_password=PASSWORD,
        admin_login_max_failures=3,
        admin_login_window_seconds=60,
    )
    app = create_app(db_path=tmp_path / "auth.db", template_dir=tmp_path, settings=settings)
    clock = FakeClock()
    app.state.login_throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=clock)
    return TestClient(app, client=("203.0.113.7", 50000)), clock


def test_settings_configure_the_throttle(tmp_path: Path) -> None:
    settings = Settings(admin_login_max_failures=4, admin_login_window_seconds=30)
    app = create_app(db_path=tmp_path / "x.db", settings=settings, allow_insecure_no_auth=True)
    assert app.state.login_throttle.max_failures == 4
    assert app.state.login_throttle.window_seconds == 30


def test_wrong_passwords_get_blocked_with_retry_after(
    app_and_clock: tuple[TestClient, FakeClock], caplog: pytest.LogCaptureFixture
) -> None:
    client, clock = app_and_clock
    with caplog.at_level(logging.WARNING, logger="mail_cadence.web"):
        for _ in range(3):
            assert client.get("/", auth=(USER, "wrong")).status_code == 401
    assert "Too many failed admin logins from 203.0.113.7" in caplog.text

    blocked = client.get("/", auth=(USER, "wrong"))
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert "www-authenticate" not in blocked.headers

    # While blocked even the right password is refused, so guesses cannot be confirmed.
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 429

    clock.now += 61
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 200


def test_requests_without_credentials_are_not_counted(
    app_and_clock: tuple[TestClient, FakeClock],
) -> None:
    client, _ = app_and_clock
    for _ in range(10):
        assert client.get("/").status_code == 401
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 200


def test_successful_login_resets_the_counter(app_and_clock: tuple[TestClient, FakeClock]) -> None:
    client, _ = app_and_clock
    for _ in range(2):
        client.get("/", auth=(USER, "wrong"))
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 200
    for _ in range(2):
        assert client.get("/", auth=(USER, "wrong")).status_code == 401
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 200


def test_wrong_user_name_counts_as_failure(app_and_clock: tuple[TestClient, FakeClock]) -> None:
    client, _ = app_and_clock
    for _ in range(3):
        client.get("/", auth=("root", PASSWORD))
    assert client.get("/", auth=(USER, PASSWORD)).status_code == 429


def test_other_clients_are_not_blocked(app_and_clock: tuple[TestClient, FakeClock]) -> None:
    client, _ = app_and_clock
    for _ in range(3):
        client.get("/", auth=(USER, "wrong"))
    other = TestClient(client.app, client=("198.51.100.9", 50000))
    assert other.get("/", auth=(USER, PASSWORD)).status_code == 200
