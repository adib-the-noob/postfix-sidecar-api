# AGENTS.md

Instructions for AI coding agents working on the **mail sidecar**. Keep this file short and update it when behavior changes.

## What this project is

A small FastAPI service that runs on the **same VM as Postfix and OpenDKIM** and automates outbound-only, multi-domain email for client domains (PoC stage). It:

1. **Onboards a domain**: creates a DKIM key, registers it with OpenDKIM, returns the DNS records the client must publish.
2. **Verifies DNS** (DKIM, SPF, DMARC) for that domain.
3. **Sends mail** for verified domains through the local Postfix. Every message leaves DKIM-signed.

It is a PoC: prefer the simplest thing that works. No inbound mail, no Dovecot, no user logins.

## Architecture

```
client / app
   | HTTP + X-API-Key  (0.0.0.0:8000)
   v
FastAPI sidecar  (on the host, user `mailapi`, systemd unit `mailapi`)
   |- POST /domains  -> sudo -n /usr/local/bin/add-domain.sh <domain>   (runs as root)
   |                    key in /etc/opendkim/keys/<domain>/, KeyTable + SigningTable, restart opendkim
   |- POST /verify   -> DNS TXT lookups (dnspython)
   `- POST /send     -> smtplib -> 127.0.0.1:25 -> Postfix -> OpenDKIM milter (127.0.0.1:8891) signs -> internet
```

Caddy (Docker, `/opt/caddy`) only obtains the TLS certificate for `mail.metrovps.art`. It is not in the mail path.

## Environment

- Ubuntu 22.04/24.04 VM, static public IP, outbound port 25 open, PTR record = `mail.metrovps.art`.
- Python 3.10+, FastAPI, pydantic v2, uvicorn, dnspython (`requirements.txt`; tests need `requirements-dev.txt`). Virtualenv at `/opt/mailapi/venv`.
- Env overrides (defaults in brackets): `MAILAPI_DB` [`/var/lib/mailapi/mailapi.db`], `MAILAPI_ADD_DOMAIN` [`/usr/local/bin/add-domain.sh`], `MAILAPI_SMTP_HOST` [`127.0.0.1`], `MAILAPI_SMTP_PORT` [`25`]. `MAILAPI_KEY` is required and read from `/etc/mailapi.env`.
- **Postfix**: `mydestination = localhost`, `inet_interfaces = loopback-only`, `milter_default_action = tempfail`. Accepts mail only from the VM itself.
- **OpenDKIM**: socket `inet:8891@localhost`; `KeyTable` and `SigningTable` are `refile:` files; selector `mail` for every domain; 2048-bit keys.
- State is SQLite at `/var/lib/mailapi/mailapi.db`, table `domains(domain, dkim, verified)`. No ORM.

## Deployed files

| Path | Purpose |
|---|---|
| `/opt/mailapi/main.py` | The whole API |
| `/usr/local/bin/add-domain.sh` | Root-owned (755). Idempotent: creates the key, appends to both tables, restarts OpenDKIM, prints `mail.txt` |
| `/var/lib/mailapi/mailapi.db` | SQLite state: `domains(domain TEXT PRIMARY KEY, dkim TEXT, verified INTEGER)` |
| `/etc/mailapi.env` | `MAILAPI_KEY=...` (mode 600) |
| `/etc/systemd/system/mailapi.service` | Runs uvicorn on `0.0.0.0:8000` as user `mailapi` |
| `/etc/sudoers.d/mailapi` | Lets `mailapi` run only `add-domain.sh` as root |
| `/etc/opendkim/` | `KeyTable`, `SigningTable`, `keys/<domain>/`. Written only by `add-domain.sh` |

## API

All routes need the header `X-API-Key`.

| Route | Purpose |
|---|---|
| `POST /domains` `{"domain": "x.com"}` | Add a domain, return the 3 DNS records. 409 if already added |
| `GET /domains` | Map of `{domain: verified}` |
| `GET /domains/{d}` | Status plus the DNS records again |
| `POST /domains/{d}/verify` | Check DNS; `verified` becomes true only if DKIM, SPF and DMARC all pass |
| `POST /send` `{"sender","to","subject","text","html"?}` | Send. 400 unless the sender's domain is added and verified; 502 on SMTP error |

DNS records each client domain must publish (selector `mail`):

| Name | Value |
|---|---|
| `mail._domainkey.<domain>` | `v=DKIM1; h=sha256; k=rsa; p=...` (from `mail.txt`, split pieces joined into one string) |
| `<domain>` | `v=spf1 include:_spf.metrovps.art -all` |
| `_dmarc.<domain>` | `v=DMARC1; p=none` |

## Commands

```bash
./run.sh                                        # local dev server (reload, ./data/mailapi.db)
sudo systemctl restart mailapi                   # after deploying main.py
sudo journalctl -u mailapi -f                    # API logs
sudo tail -f /var/log/mail.log                   # delivery: status=sent|deferred|bounced
mailq                                            # Postfix queue
sudo opendkim-testkey -d <domain> -s mail -vvv   # expect "key OK"

KEY=$(sudo cut -d= -f2 /etc/mailapi.env); H="X-API-Key: $KEY"
curl -s -X POST 127.0.0.1:8000/domains/<domain>/verify -H "$H"
curl -s -X POST 127.0.0.1:8000/send -H "$H" -H "Content-Type: application/json" \
  -d '{"sender":"hello@<domain>","to":"you@gmail.com","subject":"Test","text":"Hi"}'
```

Deploy:

```bash
sudo cp main.py /opt/mailapi/main.py && sudo systemctl restart mailapi
sudo install -o root -g root -m 755 add-domain.sh /usr/local/bin/add-domain.sh   # only if the script changed
sudo install -o root -g root -m 644 mailapi.service /etc/systemd/system/mailapi.service   # only if it changed
sudo install -o root -g root -m 440 mailapi.sudoers /etc/sudoers.d/mailapi                 # only if it changed
# after changing the unit: sudo systemctl daemon-reload && sudo systemctl restart mailapi
```

Test domain: `unifyed.tech`. For any change to sending, the received mail in Gmail ("Show original") must show SPF, DKIM and DMARC = PASS.

## Rules

**Security (never break these)**
- The API never runs as root. Root access is only `sudo -n /usr/local/bin/add-domain.sh`. Don't widen the sudoers rule, and never make the script writable by `mailapi`.
- Every domain from a request must match the `DOMAIN` regex in `main.py` and in `add-domain.sh`. It ends up in file paths and OpenDKIM tables, so never loosen it.
- No `shell=True`. Call `subprocess.run` with a list.
- The API binds `0.0.0.0:8000`, so it is reachable from the network. Do not expose port 8000 to the internet without a firewall and TLS; only the API routes check `X-API-Key` (`/docs` and `/openapi.json` are open).
- Check the API key with `secrets.compare_digest`. Never log, print or commit it.
- `/send` must refuse any domain that is not added and verified.

**Mail setup (don't change without asking)**
- Outbound only: keep `mydestination = localhost` and `inet_interfaces = loopback-only`. No Dovecot, no inbound ports.
- Keep `milter_default_action = tempfail`. Reverting to `accept` lets unsigned mail out when OpenDKIM is down.
- One key per domain, selector `mail`. Private keys are `opendkim:opendkim`, mode 600.
- The app sets `Date` and `Message-ID` itself so they are covered by the DKIM signature.
- Don't edit `KeyTable`/`SigningTable` or delete keys by hand; it desyncs from the `domains` table.

**Code style**
- Simple and short beats clever. One file until it hurts; no ORM, queue or new dependency without a clear need.
- Pydantic models for request bodies, plain `def` endpoints (FastAPI runs them in a threadpool), `HTTPException` with the right status (400 invalid or wrong state, 404 unknown, 409 duplicate, 502 SMTP failure).
- Comment only the non-obvious.

## Testing

- Off the VM there is no Postfix, OpenDKIM or sudo. Tests (pytest + FastAPI `TestClient`) must mock `subprocess.run`, `dns.resolver.resolve` and `smtplib.SMTP`, and point `DB` at a temp file.
- Cover at least: invalid domain (422), duplicate (409), unknown domain (404), `/send` on an unverified domain (400), verify true and false, SMTP error (502), missing or wrong API key (401).
- Run them with `python -m pytest -q`; the suite sets `MAILAPI_KEY` itself.
- Before you finish: `python -m py_compile main.py`, run the tests, and say which files need re-deploying.

## Common problems

| Symptom | Likely cause |
|---|---|
| `verify` is false right after publishing DNS | Resolver cache; wait a few minutes and retry |
| `/send` returns 502 with a 451 | OpenDKIM is restarting (about 1 second); retry |
| No `DKIM-Signature` in the received mail | Domain missing from `SigningTable`, or OpenDKIM is down |
| `opendkim-testkey` says `key not found` | TXT record not published or not propagated yet |
| Timeout connecting to a remote MX on port 25 | Provider blocks outbound port 25 |
| Mail lands in spam | PTR is not `mail.metrovps.art`, SPF/DMARC missing, or the new IP needs warm-up |

## Known gaps / TODO (in order)

1. `DELETE /domains/{d}` via a root-owned `del-domain.sh` (remove the key dir and both table lines, restart OpenDKIM, drop the row) plus a sudoers entry for it.
2. Bounces: VERP envelope sender on `bounce.metrovps.art`, bounce parsing, suppression list.
3. Per-domain daily quota, Postfix rate limits, IP warm-up (all clients share one IP and one reputation).
4. Expose the API through Caddy with TLS and the API key. Caddy runs in Docker; the API binds `0.0.0.0:8000`, so firewall 8000 to the Docker bridge/trusted hosts and proxy to the host.
5. Retry `/send` automatically on a 451.

## Ask before you

- change Postfix or OpenDKIM config, the sudoers rule, firewall rules or DNS records,
- delete keys or domain state,
- send real mail to anyone except the test address given in the task.