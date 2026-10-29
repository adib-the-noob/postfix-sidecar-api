# Mail Sidecar

A small FastAPI service that runs on the same VM as Postfix and OpenDKIM and automates outbound-only, multi-domain email for client domains.

It does three things:

1. **Onboards a domain** — creates a 2048-bit DKIM key, registers it with OpenDKIM, and returns the DNS records the client must publish.
2. **Verifies DNS** — checks that DKIM, SPF and DMARC records are published correctly.
3. **Sends mail** — accepts messages for verified domains and hands them to the local Postfix, which signs every message with OpenDKIM.

State is stored in SQLite. There is no inbound mail, no Dovecot and no user login. The HTTP API listens on `127.0.0.1` only.

See [AGENT.md](AGENT.md) for the full mail-server setup, architecture and operating rules.

## Requirements

- Ubuntu 22.04/24.04 VM with Postfix and OpenDKIM configured as described in [AGENT.md](AGENT.md).
- Python 3.10+ (`python3-venv` for a virtualenv).
- Root access for the one-time deployment; the API itself runs as the unprivileged `mailapi` user.

## Quick start (local development)

Local mode is for editing and tests. Domain onboarding needs the VM's root script via `sudo`, and `/send` needs a local Postfix, so those calls fail off the VM.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
./run.sh
```

`run.sh` starts uvicorn on <http://127.0.0.1:8000> with auto-reload and these defaults:

| Variable | Default in `run.sh` |
|---|---|
| `MAILAPI_KEY` | `dev-key` (a warning is printed) |
| `MAILAPI_DB` | `./data/mailapi.db` |
| `MAILAPI_SMTP_HOST` | `127.0.0.1` |
| `MAILAPI_SMTP_PORT` | `25` |

Check that it is up:

```bash
curl -s 127.0.0.1:8000/domains -H 'X-API-Key: dev-key'
# {}
```

To use a different port or database:

```bash
PORT=9000 MAILAPI_DB=/tmp/mailapi.db ./run.sh
```

Run the test suite (mocks `sudo`, DNS and SMTP, so it works anywhere):

```bash
python -m pytest -q
```

## Configuration

All configuration comes from environment variables. In production they are loaded from `/etc/mailapi.env` by the systemd unit.

| Variable | Default | Purpose |
|---|---|---|
| `MAILAPI_KEY` | **required** | Expected value of the `X-API-Key` header |
| `MAILAPI_DB` | `/var/lib/mailapi/mailapi.db` | SQLite database file |
| `MAILAPI_ADD_DOMAIN` | `/usr/local/bin/add-domain.sh` | Root-owned onboarding script |
| `MAILAPI_SMTP_HOST` | `127.0.0.1` | Local Postfix |
| `MAILAPI_SMTP_PORT` | `25` | Local Postfix |

The service refuses to start if `MAILAPI_KEY` is unset.

## Start on the VM (production)

These steps assume Postfix and OpenDKIM are already configured (see AGENT.md). Run them from the repository directory.

### 1. Create the user, directory and virtualenv

```bash
sudo useradd --system --no-create-home --home-dir /opt/mailapi --shell /usr/sbin/nologin mailapi 2>/dev/null || true
sudo install -d -m 755 /opt/mailapi
sudo apt install -y python3-venv
sudo python3 -m venv /opt/mailapi/venv
sudo /opt/mailapi/venv/bin/pip install -r requirements.txt
```

### 2. Store the API key

```bash
sudo sh -c 'umask 077; echo "MAILAPI_KEY=$(openssl rand -hex 32)" > /etc/mailapi.env'
```

### 3. Install the application files

```bash
sudo install -o root -g root -m 644 main.py /opt/mailapi/main.py
sudo install -o root -g root -m 755 add-domain.sh /usr/local/bin/add-domain.sh
sudo install -o root -g root -m 644 mailapi.service /etc/systemd/system/mailapi.service
sudo install -o root -g root -m 440 mailapi.sudoers /etc/sudoers.d/mailapi
sudo visudo -cf /etc/sudoers.d/mailapi
```

### 4. Start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mailapi
sudo systemctl status mailapi
```

The unit creates `/var/lib/mailapi` and runs uvicorn on `127.0.0.1:8000` as `mailapi`. The SQLite file is created on the first request.

### 5. Verify

```bash
KEY=$(sudo cut -d= -f2 /etc/mailapi.env)
curl -s 127.0.0.1:8000/domains -H "X-API-Key: $KEY"
```

After changing `main.py`, redeploy and restart:

```bash
sudo install -o root -g root -m 644 main.py /opt/mailapi/main.py
sudo systemctl restart mailapi
```

If `add-domain.sh`, the unit or the sudoers rule changes, reinstall those files the same way as in step 3, then `sudo systemctl daemon-reload && sudo systemctl restart mailapi`.

## API

Every route requires the header `X-API-Key`.

```bash
KEY=$(sudo cut -d= -f2 /etc/mailapi.env)
H="X-API-Key: $KEY"
```

| Method | Path | Body | Result |
|---|---|---|---|
| `POST` | `/domains` | `{"domain":"client.example"}` | The three DNS records. `409` if already added, `422` if invalid |
| `GET` | `/domains` | — | `{"client.example": false}` — map of domain to verified |
| `GET` | `/domains/{domain}` | — | Verified status plus the DNS records. `404` if unknown |
| `POST` | `/domains/{domain}/verify` | — | DNS check results; sets `verified`. `404` if unknown |
| `POST` | `/send` | `{"sender","to","subject","text","html"?}` | `{"status":"sent"}`. `400` if the sender's domain is not verified, `502` on SMTP failure |

Authentication failures return `401`.

### 1. Add a domain

```bash
curl -s -X POST 127.0.0.1:8000/domains \
  -H "$H" -H "Content-Type: application/json" \
  -d '{"domain":"client.example"}'
```

Response:

```json
{
  "domain": "client.example",
  "records": [
    {"name": "mail._domainkey.client.example", "type": "TXT", "value": "v=DKIM1; h=sha256; k=rsa; p=MIIB..."},
    {"name": "client.example", "type": "TXT", "value": "v=spf1 include:_spf.metrovps.art -all"},
    {"name": "_dmarc.client.example", "type": "TXT", "value": "v=DMARC1; p=none"}
  ]
}
```

This also creates `/etc/opendkim/keys/client.example/mail.private` and adds the domain to OpenDKIM's `KeyTable` and `SigningTable`.

### 2. Publish the DNS records

Publish exactly the three records above at the client's DNS provider:

| Name | Type | Value |
|---|---|---|
| `mail._domainkey.<domain>` | TXT | The `value` returned for it (one single string) |
| `<domain>` | TXT | `v=spf1 include:_spf.metrovps.art -all` |
| `_dmarc.<domain>` | TXT | `v=DMARC1; p=none` |

Notes:

- The DKIM value must be one TXT string. If the provider shows "split" records, join the pieces with no spaces.
- Publish only one SPF record for the domain.
- DNS caching can delay verification; retry after a few minutes.

### 3. Verify DNS

```bash
curl -s -X POST 127.0.0.1:8000/domains/client.example/verify -H "$H"
```

Response:

```json
{"domain": "client.example", "verified": true, "checks": {"dkim": true, "spf": true, "dmarc": true}}
```

`verified` becomes `true` only when all three checks pass:

- **DKIM** — the published key at `mail._domainkey.<domain>` matches the generated key.
- **SPF** — the domain has an SPF record containing `include:_spf.metrovps.art` and ending in `-all`.
- **DMARC** — `_dmarc.<domain>` has a record starting with `v=DMARC1`.

`GET /domains` shows the current state:

```bash
curl -s 127.0.0.1:8000/domains -H "$H"
# {"client.example": true}
```

### 4. Send mail

```bash
curl -s -X POST 127.0.0.1:8000/send \
  -H "$H" -H "Content-Type: application/json" \
  -d '{
    "sender": "hello@client.example",
    "to": "you@gmail.com",
    "subject": "Test",
    "text": "Hello from the sidecar",
    "html": "<p>Hello from the sidecar</p>"
  }'
```

Response:

```json
{"status": "sent"}
```

`html` is optional. Multiple recipients can be comma-separated in `to`. The API sets `Date` and `Message-ID` itself so they are covered by the DKIM signature. Delivery goes to the local Postfix on `127.0.0.1:25`; OpenDKIM signs it.

The sender's domain must already be added and verified, otherwise the request returns `400`.

## Data and operations

State is SQLite:

```bash
sudo sqlite3 /var/lib/mailapi/mailapi.db 'SELECT domain, verified FROM domains;'
```

Table: `domains(domain TEXT PRIMARY KEY, dkim TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0)`.

If you are upgrading from the earlier JSON store, `domains.json` is not imported automatically. Re-add each domain with `POST /domains`; the idempotent script reuses the existing key and the SQLite row is recreated.

Useful commands:

```bash
sudo journalctl -u mailapi -f                  # API logs
sudo tail -f /var/log/mail.log                 # delivery: status=sent|deferred|bounced
mailq                                          # Postfix queue
sudo opendkim-testkey -d <domain> -s mail -vvv # expect "key OK"
sudo systemctl restart mailapi                 # after deploying main.py
```

Do not edit `KeyTable`, `SigningTable` or the key directories by hand, and do not delete the SQLite rows directly. They must stay in sync; use the API and the root script.

## Tests

```bash
pip install -r requirements-dev.txt
python -m py_compile main.py
python -m pytest -q
```

The tests mock `subprocess.run`, `dns.resolver.resolve` and `smtplib.SMTP` and point `DB` at a temporary SQLite file, so no Postfix, OpenDKIM or root access is needed.

## Project files

| File | Purpose |
|---|---|
| `main.py` | The whole API, including the SQLite layer |
| `add-domain.sh` | Root-owned, idempotent DKIM onboarding script |
| `run.sh` | Local development server |
| `mailapi.service` | systemd unit for production |
| `mailapi.sudoers` | Narrow sudo rule for `add-domain.sh` |
| `requirements.txt` / `requirements-dev.txt` | Runtime and test dependencies |
| `test_main.py` | pytest suite |
| `AGENT.md` | Mail-server setup, architecture and rules |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 invalid API key` | Missing or wrong `X-API-Key` |
| `422 invalid domain name` | Domain does not match the allowed pattern |
| `409 already added` | Domain already exists in the database |
| `500 failed to add domain` | `add-domain.sh` failed — check the sudoers rule and `journalctl -u mailapi` |
| `verify` is false right after publishing DNS | Resolver cache; wait a few minutes and retry |
| `/send` returns `502` with a 451 | OpenDKIM is restarting (about one second); retry |
| No `DKIM-Signature` in the received mail | Domain missing from `SigningTable`, or OpenDKIM is down |
| `opendkim-testkey` says `key not found` | TXT record not published or not propagated yet |
| Timeout connecting to a remote MX on port 25 | Provider blocks outbound port 25 |
| Mail lands in spam | PTR is not `mail.metrovps.art`, SPF/DMARC missing, or the new IP needs warm-up |

## Security notes

- Keep uvicorn on `127.0.0.1`; do not expose port 8000 in the firewall.
- Treat `MAILAPI_KEY` as a secret: it is stored in `/etc/mailapi.env` (mode 600) and is never logged by the app.
- The API never runs as root. Its only privileged action is `sudo -n /usr/local/bin/add-domain.sh <domain>`, constrained by `/etc/sudoers.d/mailapi`.
- `add-domain.sh` must stay root-owned and must never be writable by `mailapi`.
- Every requested domain is validated against the same strict regex in `main.py` and `add-domain.sh` because it becomes part of file paths and OpenDKIM tables.
