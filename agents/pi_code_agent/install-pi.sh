#!/usr/bin/env bash
set -euo pipefail

if command -v pi >/dev/null 2>&1 && [ "$(pi --version)" = "0.84.1" ]; then
  exit 0
fi

apt-get update
apt-get install -y ca-certificates curl

export NVM_DIR=/root/.nvm
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash
fi
. "$NVM_DIR/nvm.sh"
nvm install 22
npm install -g @earendil-works/pi-coding-agent@0.84.1
pi --version
