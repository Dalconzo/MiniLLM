#!/bin/bash
set -euo pipefail

PI_TARGET="${PI_TARGET:-dave@DavesDev}"
REMOTE_BASE="${REMOTE_BASE:-/home/dave/bake_cam/day7}"
REMOTE_HOST="${BAKE_CAM_REMOTE_HOST:-100.96.213.25}"
REMOTE_USER="${BAKE_CAM_REMOTE_USER:-daviddalconzo}"
REMOTE_DIR="${BAKE_CAM_REMOTE_DIR:-/Users/daviddalconzo/MiniLLM/data/home_mcp/recipes/images/starters/day-7}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/pi_bake_cam_day7_runner.sh"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PI_TARGET" "mkdir -p '$REMOTE_BASE'"
scp -O -q -o BatchMode=yes -o ConnectTimeout=12 "$RUNNER" "$PI_TARGET:$REMOTE_BASE/runner.sh"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PI_TARGET" "cat > '$REMOTE_BASE/schedule.tsv' <<'EOF'
t+4h	1783917000	2026-07-12T21:30:00-07:00
t+6h	1783924200	2026-07-12T23:30:00-07:00
t+9h	1783935000	2026-07-13T02:30:00-07:00
t+12h	1783945800	2026-07-13T05:30:00-07:00
t+16h	1783960200	2026-07-13T09:30:00-07:00
EOF
chmod +x '$REMOTE_BASE/runner.sh'
mkdir -p '$REMOTE_BASE/spool' '$REMOTE_BASE/done' '$REMOTE_BASE/logs' '/home/dave/.ssh'
chmod 700 '/home/dave/.ssh'
touch '/home/dave/.ssh/config'
chmod 600 '/home/dave/.ssh/config'
grep -q 'Host bakecam-mac-target' '/home/dave/.ssh/config' 2>/dev/null || cat >> '/home/dave/.ssh/config' <<EOF

Host bakecam-mac-target
  HostName $REMOTE_HOST
  User $REMOTE_USER
  IdentityFile ~/.ssh/bakecam_upload_ed25519
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 12
  ServerAliveInterval 5
  ServerAliveCountMax 2
EOF
"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PI_TARGET" "tmpcron=\$(mktemp); crontab -l 2>/dev/null | grep -v 'bake_cam/day7/runner.sh' > \$tmpcron || true; cat >> \$tmpcron <<'EOF'
* * * * * BAKE_CAM_REMOTE_HOST=$REMOTE_HOST BAKE_CAM_REMOTE_USER=$REMOTE_USER BAKE_CAM_REMOTE_DIR='$REMOTE_DIR' /home/dave/bake_cam/day7/runner.sh
EOF
crontab \$tmpcron
rm -f \$tmpcron
crontab -l | tail -5
"

echo "Installed Pi-side bake-cam Day 7 runner on $PI_TARGET"
