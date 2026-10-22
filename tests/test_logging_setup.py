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
