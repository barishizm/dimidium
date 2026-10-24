import base64
import json
from typing import Any

import pytest
from openai import InvalidWebhookSignatureError, OpenAI

from app.live.accept_errors import explain_accept_failure
from app.live.setup_checks import (
    audit_call_recording,
    audit_connection,
    audit_number,
    audit_translated_number,
    signed_headers,
    sip_destination,
    valid_e164,
    valid_project_id,
)

PROJECT = "proj_AbC123xyz"
NUMBER = "+37060000099"
SECRET = "whsec_" + base64.b64encode(b"doctor-test-signing-key-0123").decode()


def _by_check(findings: list[Any]) -> dict[str, str]:
    return {f.check: f.status for f in findings}


# -- formats -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (PROJECT, True),
        ("proj_abc", False),
        ("org_AbC123xyz", False),
        ("my project", False),
        ("", False),
        (None, False),
    ],
)
def test_project_id_format(value: str | None, ok: bool) -> None:
    assert valid_project_id(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("+37060000099", True),
        ("37060000099", False),
        ("+370 600 00099", False),
        ("+0123456", False),
    ],
)
def test_e164_format(value: str, ok: bool) -> None:
    assert valid_e164(value) is ok


def test_sip_destination_matches_the_telnyx_guide() -> None:
    assert sip_destination(PROJECT) == f"sip:{PROJECT}@sip.api.openai.com:5061;transport=tls"


# -- signing -----------------------------------------------------------------------------------


def test_locally_signed_payload_verifies_with_the_openai_sdk() -> None:
    body = json.dumps({"type": "dimidium.selftest"})
    client = OpenAI(api_key="unused", webhook_secret=SECRET)
    client.webhooks.verify_signature(body, signed_headers(SECRET, body), secret=SECRET)


def test_wrong_signature_and_wrong_secret_are_refused() -> None:
    body = json.dumps({"type": "dimidium.selftest"})
    client = OpenAI(api_key="unused", webhook_secret=SECRET)
    headers = signed_headers(SECRET, body)
    with pytest.raises(InvalidWebhookSignatureError):
        client.webhooks.verify_signature(
            body, {**headers, "webhook-signature": "v1,AAAA"}, secret=SECRET
        )
    other = "whsec_" + base64.b64encode(b"another-secret-entirely-0123").decode()
    with pytest.raises(InvalidWebhookSignatureError):
        client.webhooks.verify_signature(body, signed_headers(other, body), secret=SECRET)


# -- Telnyx audits -----------------------------------------------------------------------------

GOOD_NUMBER = {
    "id": "num_1",
    "phone_number": NUMBER,
    "status": "active",
    "connection_id": "conn_1",
    "connection_name": "OpenAI GPT-Live",
}
GOOD_CONNECTION: dict[str, Any] = {
    "connection_name": "OpenAI GPT-Live",
    "active": True,
    "transport_protocol": "TLS",
    "encrypted_media": "SRTP",
    "inbound": {"codecs": ["OPUS", "G711U", "G711A"]},
}
GOOD_FQDNS = [{"fqdn": "sip.api.openai.com", "port": 5061, "dns_record_type": "a"}]


def test_number_found_active_and_assigned() -> None:
    record, finding = audit_number([GOOD_NUMBER], "+370 600 00099")
    assert record is GOOD_NUMBER and finding.status == "PASS"


@pytest.mark.parametrize(
    ("records", "text"),
    [
        ([], "not on this Telnyx account"),
        ([{**GOOD_NUMBER, "status": "port-pending"}], "port-pending"),
        ([{**GOOD_NUMBER, "connection_id": None}], "not assigned to a connection"),
    ],
)
def test_number_problems_fail(records: list[dict[str, Any]], text: str) -> None:
    _, finding = audit_number(records, NUMBER)
    assert finding.status == "FAIL" and text in finding.detail


def test_good_connection_passes_everything() -> None:
    findings = audit_connection(GOOD_CONNECTION, GOOD_FQDNS)
    assert set(_by_check(findings).values()) == {"PASS"}


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"active": False}, "Telnyx connection active"),
        ({"transport_protocol": "UDP"}, "Telnyx transport"),
        ({"encrypted_media": None}, "Telnyx SRTP"),
        ({"inbound": {"codecs": ["G722", "G729"]}}, "Telnyx codecs"),
    ],
)
def test_connection_misconfigurations_fail(change: dict[str, Any], check: str) -> None:
    findings = audit_connection({**GOOD_CONNECTION, **change}, GOOD_FQDNS)
    assert _by_check(findings)[check] == "FAIL"


def test_wrong_fqdn_port_or_missing_fqdn_fails() -> None:
    wrong_port = [{"fqdn": "sip.api.openai.com", "port": 5060, "dns_record_type": "a"}]
    srv = [{"fqdn": "sip.api.openai.com", "port": 5061, "dns_record_type": "srv"}]
    other = [{"fqdn": "example.com", "port": 5061, "dns_record_type": "a"}]
    for fqdns in (wrong_port, srv, other, []):
        assert _by_check(audit_connection(GOOD_CONNECTION, fqdns))["Telnyx FQDN"] == "FAIL"


def test_codec_order_warns_but_does_not_fail() -> None:
    connection = {**GOOD_CONNECTION, "inbound": {"codecs": ["G722", "OPUS", "G711U"]}}
    assert _by_check(audit_connection(connection, GOOD_FQDNS))["Telnyx codecs"] == "WARN"


def test_translated_number_must_be_the_project_id() -> None:
    assert audit_translated_number({"translated_number": PROJECT}, PROJECT).status == "PASS"
    assert audit_translated_number({"translated_number": ""}, PROJECT).status == "FAIL"
    assert audit_translated_number({}, PROJECT).status == "FAIL"
    assert (
        audit_translated_number({"translated_number": "proj_Other12345"}, PROJECT).status == "FAIL"
    )
    assert audit_translated_number({"translated_number": "+37060000099"}, None).status == "FAIL"


def test_call_recording_on_warns() -> None:
    on = {"call_recording": {"inbound_call_recording_enabled": True}}
    off = {"call_recording": {"inbound_call_recording_enabled": False}}
    assert audit_call_recording(on).status == "WARN"
    assert audit_call_recording(off).status == "PASS"
    assert audit_call_recording({}).status == "PASS"


# -- accept failures ---------------------------------------------------------------------------


class _ApiError(Exception):
    def __init__(self, status_code: int | None, code: str | None = None) -> None:
        self.status_code, self.code = status_code, code


@pytest.mark.parametrize(
    ("exc", "key"),
    [
        (_ApiError(409, "decision_already_made"), "already_decided"),
        (_ApiError(404, "session_id_not_found"), "session_not_found"),
        (_ApiError(404), "session_not_found"),
        (_ApiError(401), "bad_key"),
        (_ApiError(403), "sip_not_enabled"),
        (_ApiError(500), "other"),
        (TimeoutError(), "other"),
    ],
)
def test_accept_failures_are_explained(exc: BaseException, key: str) -> None:
    failure = explain_accept_failure(exc)
    assert failure.key == key and failure.english and failure.turkish
