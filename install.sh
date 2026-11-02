#!/usr/bin/env bash
# sudo bash install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo bash "$0" "$@"

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/vc-keeper
ENV_FILE=/etc/vc-keeper.env
say() { echo -e "\e[36m[vc-keeper]\e[0m $*"; }

apt-get install -y -qq curl ca-certificates git >/dev/null

if ! command -v uv >/dev/null; then
  say "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
fi

say "copying app to $APP"
mkdir -p "$APP"
cp -r "$SRC"/pyproject.toml "$SRC"/uv.lock "$SRC"/.python-version "$SRC"/LICENSE "$SRC"/src "$APP"/

say "installing python + dependencies (locked versions)"
cd "$APP"
export UV_PYTHON_INSTALL_DIR="$APP/python" UV_PROJECT_ENVIRONMENT="$APP/.venv" UV_PYTHON_PREFERENCE=only-managed
uv sync --frozen --no-dev --compile-bytecode --quiet
chmod -R a+rX "$APP"

if [[ ! -f $ENV_FILE ]]; then
  say "first run, let's set it up (the token input is hidden)"
  read -rsp "discord token: " TOKEN; echo
  read -rp  "server (guild) id: " GUILD
  read -rp  "voice channel id: " CHANNEL
  ( umask 077; cat > "$ENV_FILE" <<ENV
DISCORD_TOKEN=$TOKEN
DISCORD_GUILD_ID=$GUILD
DISCORD_CHANNEL_ID=$CHANNEL
LOG_LEVEL=INFO
SELF_DEAF=false
REJOIN_COOLDOWN_MIN=10
REJOIN_COOLDOWN_MAX=120
ENV
  )
else
  say "keeping existing $ENV_FILE"
fi

if systemctl list-unit-files vc-stay.service >/dev/null 2>&1 && systemctl is-enabled vc-stay >/dev/null 2>&1; then
  say "disabling the old vc-stay service"
  systemctl disable --now vc-stay || true
fi

install -m 644 "$SRC/deploy/vc-keeper.service" /etc/systemd/system/vc-keeper.service
systemctl daemon-reload
systemctl enable --now vc-keeper >/dev/null
systemctl restart vc-keeper
sleep 3
systemctl --no-pager --lines=15 status vc-keeper || true
say "done! follow the logs with:  journalctl -u vc-keeper -f"
