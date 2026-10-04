#!/usr/bin/env bash
# Add this server's current public address to TRUSTED_HOSTS in .env and print
# the public IP. EC2 assigns a new public IP on every stop/start (unless an
# Elastic IP is attached), so the switchboard service runs this before each
# start; otherwise requests to the new address get "Host not allowed".
#
#   bash deploy/ec2-refresh-hosts.sh [path/to/.env]
set -euo pipefail

ENV_FILE="${1:-$(cd "$(dirname "$0")/.." && pwd)/.env}"
[ -f "$ENV_FILE" ] || { echo "No $ENV_FILE" >&2; exit 1; }

# EC2 instance metadata (IMDSv2); prints nothing when not on EC2.
imds() {
    local token
    token=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || return 0
    curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" \
        "http://169.254.169.254/latest/meta-data/$1" || true
}

add_trusted_host() {
    [ -n "$1" ] || return 0
    local cur
    cur=$(sed -n 's/^TRUSTED_HOSTS=//p' "$ENV_FILE" | head -1 | tr -d '\r')
    case ",$cur," in *",$1,"*) return 0 ;; esac
    if grep -q '^TRUSTED_HOSTS=' "$ENV_FILE"; then
        sed -i "s|^TRUSTED_HOSTS=.*|TRUSTED_HOSTS=${cur:+$cur,}$1|" "$ENV_FILE"
    else
        [ -z "$(tail -c1 "$ENV_FILE")" ] || echo >> "$ENV_FILE"
        echo "TRUSTED_HOSTS=$1" >> "$ENV_FILE"
    fi
}

ip=$(imds public-ipv4)
[ -n "$ip" ] || ip=$(curl -sf -m 3 https://checkip.amazonaws.com | tr -d '[:space:]' || true)
for h in localhost 127.0.0.1 "$ip" "$(imds public-hostname)"; do
    add_trusted_host "$h"
done
echo "$ip"
