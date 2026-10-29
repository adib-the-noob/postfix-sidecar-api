import os
import smtplib
import sqlite3
import subprocess
from types import SimpleNamespace

import dns.resolver
import pytest
from fastapi.testclient import TestClient

os.environ["MAILAPI_KEY"] = "test-key"

import main  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}
MAIL_TXT = """mail._domainkey\tIN\tTXT\t( "v=DKIM1; h=sha256; k=rsa; "
\t  "p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuExampleKey"
\t  )  ; ----- DKIM key mail for example.com
"""
DKIM_KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuExampleKey"
DKIM = f"v=DKIM1; h=sha256; k=rsa; p={DKIM_KEY}"
SPF = "v=spf1 include:_spf.metrovps.art -all"
DMARC = "v=DMARC1; p=none"


class FakeRdata:
    def __init__(self, value: str):
        self.strings = [value.encode()]


def fake_dns(monkeypatch, mapping: dict[str, list[str]]) -> None:
    def resolve(name, rdtype):
        if name not in mapping:
            raise dns.resolver.NXDOMAIN()
        return [FakeRdata(value) for value in mapping[name]]

    monkeypatch.setattr(main.dns.resolver, "resolve", resolve)


def mark_verified(domain: str = "example.com") -> None:
    main.set_verified(domain, True)


def send_body(**overrides) -> dict:
    body = {"sender": "hello@example.com", "to": "you@gmail.com", "subject": "Test", "text": "Hi"}
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "mailapi.db")


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def add_domain(client, monkeypatch):
    def fake_run(args, **kwargs):
        return SimpleNamespace(stdout=MAIL_TXT, stderr="", returncode=0)

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    def add(domain: str = "example.com"):
        return client.post("/domains", json={"domain": domain}, headers=HEADERS)

    return add


@pytest.fixture
def smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, message, from_addr=None, to_addrs=None):
            sent.append((message, from_addr, to_addrs))

    monkeypatch.setattr(main.smtplib, "SMTP", FakeSMTP)
    return sent


def test_add_domain_returns_records(client, add_domain):
    response = add_domain()
    assert response.status_code == 200
    body = response.json()
    assert body["domain"] == "example.com"
    records = {record["name"]: record["value"] for record in body["records"]}
    assert records["mail._domainkey.example.com"] == DKIM
    assert records["example.com"] == SPF
    assert records["_dmarc.example.com"] == DMARC
    assert client.get("/domains", headers=HEADERS).json() == {"example.com": False}


def test_add_domain_normalizes_case(client, add_domain):
    response = add_domain("Example.COM")
    assert response.status_code == 200
    assert response.json()["domain"] == "example.com"


def test_domain_stored_in_sqlite(client, add_domain):
    add_domain()
    conn = sqlite3.connect(main.DB)
    try:
        row = conn.execute("SELECT domain, dkim, verified FROM domains").fetchone()
    finally:
        conn.close()
    assert row == ("example.com", DKIM, 0)


def test_add_domain_script_failure(client, monkeypatch):
    def failing_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="boom")

    monkeypatch.setattr(main.subprocess, "run", failing_run)
    response = client.post("/domains", json={"domain": "example.com"}, headers=HEADERS)
    assert response.status_code == 500


@pytest.mark.parametrize("domain", ["not a domain", "bad_domain.com", "-bad.com", "localhost", ""])
def test_invalid_domain(client, domain):
    assert client.post("/domains", json={"domain": domain}, headers=HEADERS).status_code == 422


def test_invalid_path_domain(client):
    assert client.get("/domains/bad_domain.com", headers=HEADERS).status_code == 422


def test_duplicate_domain(client, add_domain):
    assert add_domain().status_code == 200
    assert add_domain().status_code == 409


def test_unknown_domain(client):
    assert client.get("/domains/nope.com", headers=HEADERS).status_code == 404
    assert client.post("/domains/nope.com/verify", headers=HEADERS).status_code == 404


def test_get_domain(client, add_domain):
    add_domain()
    response = client.get("/domains/example.com", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["verified"] is False
    assert response.json()["records"][0]["value"] == DKIM


def test_verify_true(client, add_domain, monkeypatch):
    add_domain()
    fake_dns(
        monkeypatch,
        {
            "mail._domainkey.example.com": [DKIM],
            "example.com": [SPF],
            "_dmarc.example.com": [DMARC],
        },
    )
    response = client.post("/domains/example.com/verify", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["verified"] is True
    assert response.json()["checks"] == {"dkim": True, "spf": True, "dmarc": True}
    assert client.get("/domains", headers=HEADERS).json() == {"example.com": True}


def test_verify_false(client, add_domain, monkeypatch):
    add_domain()
    fake_dns(
        monkeypatch,
        {
            "mail._domainkey.example.com": [DKIM],
            "example.com": [SPF],
        },
    )
    response = client.post("/domains/example.com/verify", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["verified"] is False
    assert response.json()["checks"] == {"dkim": True, "spf": True, "dmarc": False}
    assert client.get("/domains", headers=HEADERS).json() == {"example.com": False}


def test_verify_false_on_wrong_dkim_key(client, add_domain, monkeypatch):
    add_domain()
    fake_dns(
        monkeypatch,
        {
            "mail._domainkey.example.com": [DKIM.replace(DKIM_KEY, DKIM_KEY.lower())],
            "example.com": [SPF],
            "_dmarc.example.com": [DMARC],
        },
    )
    response = client.post("/domains/example.com/verify", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["checks"]["dkim"] is False


def test_send_unverified_domain(client, add_domain):
    add_domain()
    assert client.post("/send", json=send_body(), headers=HEADERS).status_code == 400


def test_send_unknown_domain(client):
    assert client.post("/send", json=send_body(), headers=HEADERS).status_code == 400


def test_send_ok(client, add_domain, smtp):
    add_domain()
    mark_verified()
    response = client.post("/send", json=send_body(), headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"status": "sent"}
    message, from_addr, to_addrs = smtp[0]
    assert from_addr == "hello@example.com"
    assert to_addrs == ["you@gmail.com"]
    assert message["Date"] is not None
    assert message["Message-ID"].endswith("@example.com>")


def test_send_smtp_error(client, add_domain, monkeypatch):
    add_domain()
    mark_verified()

    class BrokenSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, *args, **kwargs):
            raise smtplib.SMTPServerDisconnected("opendkim restarting")

    monkeypatch.setattr(main.smtplib, "SMTP", BrokenSMTP)
    assert client.post("/send", json=send_body(), headers=HEADERS).status_code == 502


def test_api_key_required(client):
    assert client.get("/domains").status_code == 401
    assert client.get("/domains", headers={"X-API-Key": "wrong"}).status_code == 401
