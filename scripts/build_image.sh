#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f agent_config.json ]; then
  echo "agent_config.json is missing." >&2
  echo "cp agent_config.example.json agent_config.json, fill in the gateway URL/key/model, then rebuild." >&2
  exit 1
fi

IMAGE="${IMAGE:-contest-agent:1.0}"
TEAM_ID="${TEAM_ID:-team-001}"
AGENT_VERSION="${AGENT_VERSION:-1.0.0}"

docker build \
  --platform linux/amd64 \
  --build-arg "TEAM_ID=${TEAM_ID}" \
  --build-arg "AGENT_VERSION=${AGENT_VERSION}" \
  -t "${IMAGE}" .

echo "Built ${IMAGE}"
echo "Export with: docker save ${IMAGE} -o contest-agent.tar"
