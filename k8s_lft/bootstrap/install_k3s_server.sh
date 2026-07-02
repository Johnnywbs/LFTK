#!/bin/bash
# Installs the K3s control-plane node bound to the Tailscale interface.
#
# MTU math (see migration plan section 3): Tailscale fixes tailscale0 at
# MTU 1280. VXLAN (Flannel's default backend) overhead is 50 bytes
# (14 Ethernet + 20 IPv4 + 8 UDP + 8 VXLAN), so the Flannel/flannel.1
# interface must end up at 1280 - 50 = 1230. --flannel-iface makes K3s
# derive that automatically; flannel-net-conf.json pins it explicitly too
# (belt and suspenders -- K3s embeds Flannel in-process, there is no
# kube-flannel ConfigMap to `kubectl patch` like on kubeadm clusters).
#
# Usage: install_k3s_server.sh <tailscale-ip> [node-name]
#
# [node-name] is optional -- without it K3s names the node after the
# machine's own `hostname`. Set it explicitly to control exactly what
# `kubectl get nodes` will show, since that NAME is what you'll pass as
# --server-hostname to factorial_experiment.py/run.py.
set -euo pipefail

TAILSCALE_IP="${1:?usage: install_k3s_server.sh <tailscale-ip> [node-name]}"
NODE_NAME="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLANNEL_CONF_DIR="/etc/rancher/k3s/flannel-conf"
FLANNEL_CONF_PATH="${FLANNEL_CONF_DIR}/net-conf.json"

sudo mkdir -p "${FLANNEL_CONF_DIR}"
sudo cp "${SCRIPT_DIR}/flannel-net-conf.json" "${FLANNEL_CONF_PATH}"

INSTALL_K3S_EXEC="server \
  --flannel-iface=tailscale0 \
  --flannel-conf=${FLANNEL_CONF_PATH} \
  --node-ip=${TAILSCALE_IP} \
  --advertise-address=${TAILSCALE_IP} \
  --node-external-ip=${TAILSCALE_IP} \
  --write-kubeconfig-mode=644"
if [ -n "${NODE_NAME}" ]; then
  INSTALL_K3S_EXEC="${INSTALL_K3S_EXEC} --node-name=${NODE_NAME}"
fi

curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="${INSTALL_K3S_EXEC}" sh -

echo "Node token (needed by install_k3s_agent.sh on the second host):"
sudo cat /var/lib/rancher/k3s/server/node-token
