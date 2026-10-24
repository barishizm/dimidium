from app.logging_setup import redact_phone_numbers


def test_redacts_e164_number() -> None:
    assert redact_phone_numbers("call from +37061234567") == "call from +370******67"


def test_redacts_spaced_number() -> None:
    assert redact_phone_numbers("8 612 34567") == "8612***67"


def test_leaves_short_numbers_alone() -> None:
    assert redact_phone_numbers("hold 75 s, order 12345") == "hold 75 s, order 12345"


def test_leaves_times_and_decimals_alone() -> None:
    text = "at 09:39:10.300190, took 1234567.5 ms"
    assert redact_phone_numbers(text) == text


def _format(message: str, secrets: list[str] | None = None) -> str:
    import logging

    from app.logging_setup import JsonFormatter

    record = logging.makeLogRecord({"msg": message, "levelname": "INFO", "name": "t"})
    return JsonFormatter(secrets).format(record)


def test_configured_secrets_are_masked() -> None:
    out = _format("auth failed for token s3cr3t-twilio-token", ["s3cr3t-twilio-token", "short"])
    assert "s3cr3t-twilio-token" not in out and "***" in out


def test_credential_shapes_are_masked_without_configuration() -> None:
    token = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    out = _format(f"POST https://api.telegram.org/bot{token}/getMe sk-ant-api03-abcdefghijklmnop")
    assert token not in out and "sk-ant-api03" not in out


def test_openai_credentials_are_masked_without_configuration() -> None:
    key = "sk-proj-" + "a1B2" * 12
    secret = "whsec_" + "QUJDREVGR0hJSktM" * 2
    out = _format(f"auth failed: {key} / {secret}")
    assert key not in out and secret not in out and out.count("***") == 2


def test_caller_numbers_in_structured_fields_are_masked() -> None:
    import json
    import logging

    from app.logging_setup import JsonFormatter

    record = logging.makeLogRecord(
        {"msg": "Incoming SIP call accepted", "caller": "+37061234567", "levelname": "INFO"}
    )
    out = json.loads(JsonFormatter().format(record))
    assert "61234567" not in out["caller"] and out["caller"].endswith("67")


def test_secret_values_lists_only_set_secrets() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, anthropic_api_key="sk-ant-x", twilio_auth_token="tok")
    assert sorted(settings.secret_values()) == ["sk-ant-x", "tok"]
