#!/bin/bash
# Synker arbeidskopien på verten og redeployer stacken i Portainer.
#
# Kjøres fra Hermes-hosten (der app-repoet ligger og Portainer-API-nøkkelen finnes):
#   bash scripts/oppdater-vert.sh [vert] [portainer-id]
#
# Verten bygger imaget selv (pull_policy: build), så rekkefølgen er:
#   1) pakk kildekoden fra dette repoet
#   2) kopier til verten og bytt ut arbeidskopien
#   3) be Portainer redeploye stacken (som da bygger fra den ferske kopien)
set -euo pipefail

VERT="${1:-dockeradmin@10.11.12.16}"
STACK_ID="${2:-}"
ARBEIDSKOPI="/home/dockeradmin/docker_volumes/finn-marked/src"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "== pakker kildekoden"
TARBALL="$(mktemp /tmp/finn-marked-src-XXXX.tgz)"
tar --exclude=.venv --exclude=data --exclude=.git --exclude=__pycache__ -czf "$TARBALL" -C "$REPO" .

echo "== kopierer til $VERT"
scp -q "$TARBALL" "$VERT:/tmp/"
ssh "$VERT" "mkdir -p '$ARBEIDSKOPI' && tar xzf '$TARBALL' -C '$ARBEIDSKOPI' && rm -f '$TARBALL'"
rm -f "$TARBALL"

echo "== bygger imaget på verten (pull_policy: never i stacken bruker dette)"
ssh "$VERT" "cd '$ARBEIDSKOPI' && docker build -q -t ghcr.io/terjefl/finn-marked:latest . && docker image inspect ghcr.io/terjefl/finn-marked:latest --format '   image {{.Id}} bygget {{.Created}}'"

if [ -z "$STACK_ID" ]; then
  echo "== ingen stack-id oppgitt — hopper over redeploy."
  echo "   Redeploy stacken i Portainer (Pull and redeploy) eller oppgi id som 2. argument."
  exit 0
fi

echo "== ber Portainer redeploye stack $STACK_ID"
python3 - "$STACK_ID" <<'PY'
import os, sys, urllib.request, pathlib

# API-nøkkelen leses fra ~/.hermes/config.yaml. MERK: nøkkelen inneholder
# base64-tegn (+ / =), så den leses som «alt etter første kolon» på linjen —
# aldri med et regex som begrenser tegnsettet (det kutter verdien ved første +).
# Miljøvariabelen PORTAINER_API_KEY går foran hvis den er satt.
stack_id = sys.argv[1]
api = os.environ.get("PORTAINER_API_KEY", "")
base = "http://10.11.12.16:9000"
cfg_sti = pathlib.Path.home() / ".hermes/config.yaml"
if not api and cfg_sti.exists():
    linjer = cfg_sti.read_text(encoding="utf-8").splitlines()
    for n, l in enumerate(linjer):
        if "X-Portainer-API-Key" in l:
            api = l.split(":", 1)[1].strip().strip('"').strip("'")
            # leter etter url i portainer-blokken over
            for j in range(n, max(0, n - 15), -1):
                if "url:" in linjer[j] and "portainer" not in linjer[j].split("url:")[-1]:
                    base = linjer[j].split("url:", 1)[1].strip().strip('"').strip("'").rstrip("/")
                    break
            break
if not api:
    sys.exit("  fant ikke Portainer-API-nøkkelen (sett PORTAINER_API_KEY)")
req = urllib.request.Request(f"{base}/api/stacks/{stack_id}/git/redeploy?endpointId=2",
                            method="POST", data=b'{"Prune": false}',
                            headers={"X-API-Key": api, "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as r:
    print("  redeploy:", r.status)
PY
