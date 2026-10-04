#!/usr/bin/env bash
# Install Switchboard AI on a fresh Ubuntu server (AWS EC2 or any VPS) and run it
# as a systemd service that starts on boot. Safe to run again: it updates the
# code and restarts the service, keeping your .env.
#
#   curl -fsSL https://raw.githubusercontent.com/meetdhamecha/switchboard-ai/main/deploy/ec2-install.sh | bash
#
# Also works as EC2 "User data" (runs as root, installs for the ubuntu user) or
# from a clone (bash deploy/ec2-install.sh, uses that clone). Optional settings:
#   SWITCHBOARD_USER    account to run as     (default: you, or ubuntu when root)
#   SWITCHBOARD_DIR     install folder        (default: ~/switchboard-ai)
#   SWITCHBOARD_REPO    git URL               SWITCHBOARD_BRANCH  (default: main)
#   SWITCHBOARD_PORT    port for a new .env   (default: 8000)
set -euo pipefail

REPO="${SWITCHBOARD_REPO:-https://github.com/meetdhamecha/switchboard-ai.git}"
BRANCH="${SWITCHBOARD_BRANCH:-main}"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
    APP_USER="${SWITCHBOARD_USER:-${SUDO_USER:-ubuntu}}"
else
    SUDO="sudo"
    APP_USER="${SWITCHBOARD_USER:-$(id -un)}"
fi
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6 || true)"
[ -n "$APP_HOME" ] || { echo "User '$APP_USER' not found. Set SWITCHBOARD_USER."; exit 1; }

# Run from a clone: install that clone instead of cloning again.
SRC="${BASH_SOURCE[0]:-}"
if [ -z "${SWITCHBOARD_DIR:-}" ] && [ -f "$SRC" ] && [ -f "$(dirname "$SRC")/../pyproject.toml" ]; then
    APP_DIR="$(cd "$(dirname "$SRC")/.." && pwd)"
else
    APP_DIR="${SWITCHBOARD_DIR:-$APP_HOME/switchboard-ai}"
fi
ENV_FILE="$APP_DIR/.env"

step() { printf '\n==> %s\n' "$*"; }

as_user() {
    if [ "$(id -un)" = "$APP_USER" ]; then "$@"; else sudo -u "$APP_USER" -H "$@"; fi
}

# EC2 instance metadata (IMDSv2); prints nothing when not on EC2.
imds() {
    local token
    token=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || return 0
    curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" \
        "http://169.254.169.254/latest/meta-data/$1" || true
}

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d '\r'; }

env_set() {
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        [ -z "$(tail -c1 "$ENV_FILE")" ] || echo >> "$ENV_FILE"
        echo "$1=$2" >> "$ENV_FILE"
    fi
}

add_trusted_host() {
    [ -n "$1" ] || return 0
    local cur
    cur=$(env_get TRUSTED_HOSTS)
    case ",$cur," in *",$1,"*) return 0 ;; esac
    env_set TRUSTED_HOSTS "${cur:+$cur,}$1"
}

# ── 1. System packages ───────────────────────────────────────
command -v apt-get >/dev/null || {
    echo "This installer needs Ubuntu or Debian (apt). On EC2, pick an Ubuntu 24.04 AMI."
    exit 1
}
step "Installing system packages"
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip git curl openssl >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    echo "Python 3.10+ is required, found $(python3 --version). Use Ubuntu 22.04 or newer."
    exit 1
}

# ── 2. Swap on small machines (t3.micro has 1 GB) ────────────
mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 1500 ] && [ -z "$(swapon --show --noheadings)" ]; then
    step "Adding 2 GB of swap (this machine has ${mem_mb} MB of RAM)"
    [ -f /swapfile ] || $SUDO fallocate -l 2G /swapfile
    $SUDO chmod 600 /swapfile
    $SUDO mkswap /swapfile >/dev/null
    $SUDO swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
fi

# ── 3. Code and virtual environment ──────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    step "Updating the code in $APP_DIR"
    as_user git -C "$APP_DIR" pull -q --ff-only || echo "  Couldn't fast-forward; keeping the current code."
else
    step "Cloning $REPO into $APP_DIR"
    as_user git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

step "Installing Switchboard into $APP_DIR/.venv"
[ -x "$APP_DIR/.venv/bin/python" ] || as_user python3 -m venv "$APP_DIR/.venv"
as_user "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
as_user "$APP_DIR/.venv/bin/pip" install -q -e "${APP_DIR}[api]"

# ── 4. Provider CLIs (official installers, into ~/.local/bin) ─
step "Installing Claude Code and the Antigravity CLI"
as_user bash -c 'cd "$1" && .venv/bin/python -m switchboard_ai setup --yes' _ "$APP_DIR" \
    || echo "  A provider failed to install. Check later with: .venv/bin/python -m switchboard_ai setup"

# ── 5. .env: listen on the network, require an API key ───────
if [ ! -f "$ENV_FILE" ]; then
    step "Creating $ENV_FILE"
    as_user bash -c 'tr -d "\r" < "$1/.env.example" > "$1/.env"' _ "$APP_DIR"
    env_set API_HOST 0.0.0.0
    env_set API_PORT "${SWITCHBOARD_PORT:-8000}"
    env_set API_KEY "$(openssl rand -hex 32)"
fi
# The public address changes on every stop/start unless you attach an Elastic
# IP, so add the current one each run.
PUBLIC_IP=$(imds public-ipv4)
[ -n "$PUBLIC_IP" ] || PUBLIC_IP=$(curl -sf -m 3 https://checkip.amazonaws.com | tr -d '[:space:]' || true)
for h in localhost 127.0.0.1 "$PUBLIC_IP" "$(imds public-hostname)"; do
    add_trusted_host "$h"
done
[ "$(id -u)" -ne 0 ] || chown "$APP_USER:" "$ENV_FILE"
chmod 600 "$ENV_FILE"
PORT=$(env_get API_PORT)
PORT="${PORT:-8000}"

# ── 6. systemd service ───────────────────────────────────────
step "Installing and starting the 'switchboard' service"
$SUDO tee /etc/systemd/system/switchboard.service >/dev/null <<EOF
[Unit]
Description=Switchboard AI
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_HOME
Environment=PATH=$APP_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$APP_DIR/.venv/bin/python -m switchboard_ai server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable -q switchboard
$SUDO systemctl restart switchboard

up=""
for _ in $(seq 30); do
    if curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null; then up=1; break; fi
    sleep 1
done

# ── Summary ──────────────────────────────────────────────────
echo
if [ -n "$up" ]; then
    echo "Switchboard AI is running: http://${PUBLIC_IP:-<public-ip>}:$PORT/"
else
    echo "The service didn't answer yet. Logs: journalctl -u switchboard -n 50"
fi
case "$(env_get API_HOST)" in
    127.0.0.1|localhost|::1)
        echo "Note: API_HOST in .env is $(env_get API_HOST), so it's only reachable through an SSH tunnel." ;;
esac
# Don't print the key into non-interactive logs (EC2 user data ends up in the
# instance's console output).
if [ -t 1 ]; then
    echo "API key: $(env_get API_KEY)"
else
    echo "API key: grep ^API_KEY $ENV_FILE"
fi
cat <<EOF

Next steps
  1. EC2 security group: allow inbound TCP $PORT from your IP only.
  2. Sign in to Claude Code once, then restart the service:
       $APP_HOME/.local/bin/claude auth login
       sudo systemctl restart switchboard
  3. Antigravity: open the page above, enter the API key,
     then Status & Auth > Sign in.

Manage:  sudo systemctl status|restart|stop switchboard
Logs:    journalctl -u switchboard -f
Update:  run this installer again
EOF
