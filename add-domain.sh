#!/usr/bin/env bash
# Creates the DKIM key for one domain and registers it with OpenDKIM.
# Idempotent: safe to run again for an existing domain. Prints mail.txt on success.
set -euo pipefail

DOMAIN="${1:-}"
DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
SELECTOR=mail
KEY_ROOT=/etc/opendkim/keys
KEY_TABLE=/etc/opendkim/KeyTable
SIGNING_TABLE=/etc/opendkim/SigningTable

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

if [ -z "$DOMAIN" ] || [ "${#DOMAIN}" -gt 253 ] || ! printf '%s' "$DOMAIN" | grep -Eq "$DOMAIN_RE"; then
  echo "usage: $0 <domain>" >&2
  exit 2
fi

KEY_DIR="$KEY_ROOT/$DOMAIN"
KEY_FILE="$KEY_DIR/$SELECTOR.private"
TXT_FILE="$KEY_DIR/$SELECTOR.txt"

install -d -o opendkim -g opendkim -m 750 "$KEY_DIR"

if [ ! -f "$KEY_FILE" ]; then
  opendkim-genkey -b 2048 -d "$DOMAIN" -s "$SELECTOR" -D "$KEY_DIR"
fi

chown opendkim:opendkim "$KEY_FILE" "$TXT_FILE"
chmod 600 "$KEY_FILE"
chmod 644 "$TXT_FILE"

KEY_ENTRY="$SELECTOR._domainkey.$DOMAIN $DOMAIN:$SELECTOR:$KEY_FILE"
SIGN_ENTRY="*@$DOMAIN $SELECTOR._domainkey.$DOMAIN"
grep -qxF -- "$KEY_ENTRY" "$KEY_TABLE" || printf '%s\n' "$KEY_ENTRY" >> "$KEY_TABLE"
grep -qxF -- "$SIGN_ENTRY" "$SIGNING_TABLE" || printf '%s\n' "$SIGN_ENTRY" >> "$SIGNING_TABLE"

systemctl restart opendkim
cat "$TXT_FILE"
