#!/bin/bash
# Joins the second physical host to the K3s cluster over Tailscale.
# Run install_k3s_server.sh on the first host first, then run this on
# the second host with the token it prints.
#
# Usage: install_k3s_agent.sh <this-host-tailscale-ip> <server-tailscale-ip> <k3s-token> [node-name]
#
# [node-name] is optional -- without it K3s names the node after the
# machine's own `hostname`. Set it explicitly to control exactly what
# `kubectl get nodes` will show, since that NAME is what you'll pass as
# --client-hostname to factorial_experiment.py/run.py.
set -euo pipefail

TAILSCALE_IP="${1:?usage: install_k3s_agent.sh <this-host-tailscale-ip> <server-tailscale-ip> <k3s-token> [node-name]}"
SERVER_TAILSCALE_IP="${2:?server tailscale ip required}"
K3S_TOKEN="${3:?k3s node-token required (see /var/lib/rancher/k3s/server/node-token on the server host)}"
NODE_NAME="${4:-}"

INSTALL_K3S_EXEC="agent \
  --flannel-iface=tailscale0 \
  --node-ip=${TAILSCALE_IP} \
  --node-external-ip=${TAILSCALE_IP}"
if [ -n "${NODE_NAME}" ]; then
  INSTALL_K3S_EXEC="${INSTALL_K3S_EXEC} --node-name=${NODE_NAME}"
fi

curl -sfL https://get.k3s.io \
  | K3S_URL="https://${SERVER_TAILSCALE_IP}:6443" \
    K3S_TOKEN="${K3S_TOKEN}" \
    INSTALL_K3S_EXEC="${INSTALL_K3S_EXEC}" \
    sh -
