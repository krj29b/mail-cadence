"""Items 5 and 6: passwords are secret values; SMTP login without TLS is refused."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mail_cadence.mailer import INSECURE_SMTP_AUTH_MESSAGE, AsyncMailer, InsecureSMTPAuthError
from mail_cadence.models import Settings
from mail_cadence.pipeline import EXIT_CONFIG_ERROR, EXIT_OK, DispatchOptions, run_dispatch
from mail_cadence.web import create_app

SMTP_SECRET = "smtp-s3cret-value"  # noqa: S105
ADMIN_SECRET = "admin-s3cret-value"  # noqa: S105
SAMPLES = Path(__file__).resolve().parents[1] / "config" / "samples"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "smtp_user": "mailer@example.com",
        "smtp_password": SMTP_SECRET,
        "smtp_start_tls": True,
        "admin_user": "admin",
        "admin_password": ADMIN_SECRET,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# --- item 5: SecretStr ----------------------------------------------------------------


def test_passwords_never_appear_in_repr_or_dumps() -> None:
    settings = _settings()
    for text in (
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ):
        assert SMTP_SECRET not in text
        assert ADMIN_SECRET not in text
    assert settings.smtp_password is not None
    assert settings.smtp_password.get_secret_value() == SMTP_SECRET


def test_passwords_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", SMTP_SECRET)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_SECRET)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.smtp_password is not None
    assert settings.smtp_password.get_secret_value() == SMTP_SECRET
    assert settings.admin_password is not None
    assert settings.admin_password.get_secret_value() == ADMIN_SECRET


@pytest.mark.asyncio
async def test_smtp_client_gets_the_plain_password() -> None:
    mailer = AsyncMailer(_settings())
    with patch("mail_cadence.mailer.aiosmtplib.SMTP") as smtp_cls:
        mailer._get_smtp_connection()
    kwargs = smtp_cls.call_args.kwargs
    assert kwargs["username"] == "mailer@example.com"
    assert kwargs["password"] == SMTP_SECRET


def test_admin_login_works_with_secret_password(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db", settings=_settings()))
    assert client.get("/", auth=("admin", ADMIN_SECRET)).status_code == 200
    assert client.get("/", auth=("admin", "nope")).status_code == 401


def test_empty_admin_password_still_means_not_configured(tmp_path: Path) -> None:
    client = TestClient(
        create_app(db_path=tmp_path / "a.db", settings=_settings(admin_password=""))
    )
    assert client.get("/").status_code == 503


def test_settings_page_masks_the_smtp_password(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "a.db", settings=_settings()))
    html = client.get("/settings", auth=("admin", ADMIN_SECRET)).text
    assert SMTP_SECRET not in html
    assert "••••••••" in html


# --- item 6: no SMTP login without TLS ---------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "unencrypted", "blocked"),
    [
        ({"smtp_start_tls": False}, True, True),
        ({"smtp_start_tls": False, "smtp_allow_insecure_auth": True}, True, False),
        ({"smtp_start_tls": True}, False, False),
        ({"smtp_start_tls": False, "smtp_use_tls": True}, False, False),
        ({"smtp_start_tls": False, "smtp_password": None}, False, False),  # no login at all
        ({"smtp_start_tls": False, "smtp_user": None}, False, False),
    ],
)
def test_unencrypted_auth_detection(
    overrides: dict[str, object], unencrypted: bool, blocked: bool
) -> None:
    settings = _settings(**overrides)
    assert settings.smtp_auth_unencrypted is unencrypted
    assert settings.smtp_auth_blocked is blocked


def test_mailer_refuses_unencrypted_login() -> None:
    with pytest.raises(InsecureSMTPAuthError, match="sent unencrypted"):
        AsyncMailer(_settings(smtp_start_tls=False))


def test_dry_run_and_injected_clients_are_not_blocked() -> None:
    AsyncMailer(_settings(smtp_start_tls=False), dry_run=True)
    AsyncMailer(_settings(smtp_start_tls=False), smtp_client=object())  # type: ignore[arg-type]


def test_explicit_opt_in_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mail_cadence.mailer"):
        AsyncMailer(_settings(smtp_start_tls=False, smtp_allow_insecure_auth=True))
    assert "without TLS/STARTTLS" in caplog.text


def _options(tmp_path: Path, dry_run: bool) -> DispatchOptions:
    return DispatchOptions(
        db_path=tmp_path / "p.db",
        dry_run=dry_run,
        tasks_xml=SAMPLES / "tasks.xml",
        schedule_xml=SAMPLES / "schedules.xml",
    )


@pytest.mark.asyncio
async def test_pipeline_stops_before_any_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    code = await run_dispatch(_options(tmp_path, dry_run=False), _settings(smtp_start_tls=False))
    assert code == EXIT_CONFIG_ERROR
    assert INSECURE_SMTP_AUTH_MESSAGE in caplog.text
    assert not (tmp_path / "p.db").exists()


@pytest.mark.asyncio
async def test_pipeline_dry_run_is_allowed(tmp_path: Path) -> None:
    code = await run_dispatch(_options(tmp_path, dry_run=True), _settings(smtp_start_tls=False))
    assert code == EXIT_OK


def test_settings_page_warns_about_unencrypted_login(tmp_path: Path) -> None:
    for overrides, text in (
        ({"smtp_start_tls": False}, "real dispatches are refused"),
        ({"smtp_start_tls": False, "smtp_allow_insecure_auth": True}, "sent without encryption"),
    ):
        app = create_app(db_path=tmp_path / "w.db", settings=_settings(**overrides))
        html = TestClient(app).get("/settings", auth=("admin", ADMIN_SECRET)).text
        assert text in html
    html = (
        TestClient(create_app(db_path=tmp_path / "w.db", settings=_settings()))
        .get("/settings", auth=("admin", ADMIN_SECRET))
        .text
    )
    assert "unencrypted" not in html
