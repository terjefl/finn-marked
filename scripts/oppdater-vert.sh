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

echo "== redeploy"
# Portainer 2.45 har ikke lenger et brukbart REST-endepunkt for «pull and
# redeploy» (POST /api/stacks/<id>/git/redeploy svarer 405, webhook-ruten 404).
# Redeploy derfor via Portainer-verktøyet (MCP) eller UI-et. Har stacken en
# webhook, kan den brukes i stedet: sett PORTAINER_WEBHOOK til webhook-URL-en.
if [ -n "${PORTAINER_WEBHOOK:-}" ]; then
  echo "  kaller webhook"
  curl -fsS -m 120 -X POST "$PORTAINER_WEBHOOK" && echo "  webhook OK"
else
  echo "  stack $STACK_ID er oppdatert på verten — kjør «Pull and redeploy» i Portainer"
  echo "  (eller sett PORTAINER_WEBHOOK=<url> for automatisk redeploy)."
fi
