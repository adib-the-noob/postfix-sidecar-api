import logging
import os
import re
import secrets
import smtplib
import sqlite3
import subprocess
import threading
from contextlib import closing
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import dns.exception
import dns.resolver
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator

log = logging.getLogger("mailapi")

API_KEY = os.environ.get("MAILAPI_KEY", "")
if not API_KEY:
    raise RuntimeError("MAILAPI_KEY is not set")

DB = Path(os.environ.get("MAILAPI_DB", "/var/lib/mailapi/mailapi.db"))
ADD_DOMAIN = os.environ.get("MAILAPI_ADD_DOMAIN", "/usr/local/bin/add-domain.sh")
SMTP_HOST = os.environ.get("MAILAPI_SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("MAILAPI_SMTP_PORT", "25"))

SELECTOR = "mail"
SPF_RECORD = "v=spf1 include:_spf.metrovps.art -all"
DMARC_RECORD = "v=DMARC1; p=none"
DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

LOCK = threading.Lock()


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 253 or not DOMAIN.fullmatch(value):
        raise ValueError("invalid domain name")
    return value


def check_domain(value: str) -> str:
    try:
        return normalize_domain(value)
    except ValueError:
        raise HTTPException(422, "invalid domain name") from None


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain TEXT PRIMARY KEY,
    dkim TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0
)
"""


def db_connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def get_domain_entry(domain: str) -> dict | None:
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT dkim, verified FROM domains WHERE domain = ?", (domain,)
        ).fetchone()
        if row is None:
            return None
        return {"dkim": row["dkim"], "verified": bool(row["verified"])}


def insert_domain(domain: str, dkim: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute("INSERT INTO domains (domain, dkim, verified) VALUES (?, ?, 0)", (domain, dkim))


def set_verified(domain: str, verified: bool) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute("UPDATE domains SET verified = ? WHERE domain = ?", (int(verified), domain))


def all_domains() -> dict[str, bool]:
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT domain, verified FROM domains ORDER BY domain").fetchall()
        return {row["domain"]: bool(row["verified"]) for row in rows}


def dns_records(domain: str, dkim: str) -> dict:
    return {
        "domain": domain,
        "records": [
            {"name": f"{SELECTOR}._domainkey.{domain}", "type": "TXT", "value": dkim},
            {"name": domain, "type": "TXT", "value": SPF_RECORD},
            {"name": f"_dmarc.{domain}", "type": "TXT", "value": DMARC_RECORD},
        ],
    }


def parse_dkim(output: str) -> str:
    pieces = re.findall(r'"([^"]*)"', output)
    return " ".join("".join(pieces).split())


def txt_values(name: str) -> list[str]:
    try:
        answer = dns.resolver.resolve(name, "TXT")
    except dns.exception.DNSException:
        return []
    values = []
    for rdata in answer:
        strings = getattr(rdata, "strings", None)
        if strings is None:
            values.append(str(rdata).strip('"'))
        else:
            values.append("".join(s.decode() if isinstance(s, bytes) else s for s in strings))
    return values


def compact(value: str) -> str:
    return "".join(value.split())


def dkim_passes(domain: str, expected: str) -> bool:
    return any(compact(value) == compact(expected) for value in txt_values(f"{SELECTOR}._domainkey.{domain}"))


def spf_passes(domain: str) -> bool:
    for value in txt_values(domain):
        value = compact(value).lower()
        if value.startswith("v=spf1") and "include:_spf.metrovps.art" in value and value.endswith("-all"):
            return True
    return False


def dmarc_passes(domain: str) -> bool:
    return any(compact(value).lower().startswith("v=dmarc1") for value in txt_values(f"_dmarc.{domain}"))


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(401, "invalid API key")


class DomainIn(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, value: str) -> str:
        try:
            return normalize_domain(value)
        except ValueError:
            raise ValueError("invalid domain name") from None


class MailIn(BaseModel):
    sender: str
    to: str
    subject: str
    text: str
    html: str | None = None


router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/domains")
def add_domain(body: DomainIn) -> dict:
    domain = body.domain
    with LOCK:
        if get_domain_entry(domain) is not None:
            raise HTTPException(409, f"domain {domain} already added")
        try:
            result = subprocess.run(
                ["sudo", "-n", ADD_DOMAIN, domain],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.error("add-domain.sh failed for %s: %s", domain, exc.stderr.strip())
            raise HTTPException(500, "failed to add domain") from exc
        dkim = parse_dkim(result.stdout)
        if not dkim:
            raise HTTPException(500, "add-domain.sh returned no DKIM record")
        insert_domain(domain, dkim)
    return dns_records(domain, dkim)


@router.get("/domains")
def list_domains() -> dict[str, bool]:
    return all_domains()


@router.get("/domains/{domain}")
def get_domain(domain: str) -> dict:
    domain = check_domain(domain)
    entry = get_domain_entry(domain)
    if entry is None:
        raise HTTPException(404, f"domain {domain} is not added")
    result = dns_records(domain, entry["dkim"])
    result["verified"] = entry["verified"]
    return result


@router.post("/domains/{domain}/verify")
def verify_domain(domain: str) -> dict:
    domain = check_domain(domain)
    with LOCK:
        entry = get_domain_entry(domain)
        if entry is None:
            raise HTTPException(404, f"domain {domain} is not added")
        checks = {
            "dkim": dkim_passes(domain, entry["dkim"]),
            "spf": spf_passes(domain),
            "dmarc": dmarc_passes(domain),
        }
        verified = all(checks.values())
        set_verified(domain, verified)
    return {"domain": domain, "verified": verified, "checks": checks}


@router.post("/send")
def send_mail(body: MailIn) -> dict:
    sender = body.sender.strip()
    local, sep, domain = sender.rpartition("@")
    domain = domain.lower()
    if not sep or not local or len(domain) > 253 or not DOMAIN.fullmatch(domain):
        raise HTTPException(400, "invalid sender")

    entry = get_domain_entry(domain)
    if entry is None or not entry["verified"]:
        raise HTTPException(400, f"sender domain {domain} is not added and verified")

    recipients = [address.strip() for address in body.to.split(",") if address.strip()]
    if not recipients or any("@" not in address for address in recipients):
        raise HTTPException(400, "invalid recipient")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = body.to
    message["Subject"] = body.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body.text)
    if body.html:
        message.add_alternative(body.html, subtype="html")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.send_message(message, from_addr=sender, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("SMTP send failed: %s", exc)
        raise HTTPException(502, f"SMTP error: {exc}") from exc
    return {"status": "sent"}


app = FastAPI(title="mailapi")
app.include_router(router)
