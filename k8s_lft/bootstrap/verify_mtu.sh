#!/bin/bash
# Verifies the MTU chain (Tailscale 1280 -> Flannel/VXLAN 1230) is actually
# in effect after install_k3s_server.sh/install_k3s_agent.sh have run, and
# that there's no silent fragmentation across the two-host overlay -- see
# migration plan section 3.1 for the full packet path this is checking.
#
# Usage: verify_mtu.sh [namespace] [peer-pod-ip-on-other-host]
#
# Manual smoke-test checklist (run once real hosts + cluster exist):
#   1. install_k3s_server.sh <ip> on host A; install_k3s_agent.sh <ip> <server-ip> <token> on host B.
#   2. Run this script -- confirm MTUs below and the fragmentation boundary test.
#   3. kubectl create namespace lft
#   4. kubectl create secret generic dash-certs -n lft \
#        --from-file=onos_topologies/dash_topology/certs/cert.pem \
#        --from-file=onos_topologies/dash_topology/certs/key.pem
#   5. python -m k8s_lft.experiment.factorial_experiment --r 2 --seed 1 ... (small smoke run)
#      before committing to the full r=30 run.
#   6. python -m k8s_lft.experiment.anova --csv results/factorial/.../results.csv
set -euo pipefail

NAMESPACE="${1:-lft}"
PEER_POD_IP="${2:-}"

echo "== tailscale0 (expect mtu 1280) =="
ip -o link show tailscale0 || echo "tailscale0 not found -- is tailscale up?"

echo "== flannel.1 (expect mtu 1230) =="
ip -o link show flannel.1 || echo "flannel.1 not found -- has a pod been scheduled on this node yet?"

echo "== cni0 (expect mtu 1230) =="
ip -o link show cni0 || echo "cni0 not found -- has a pod been scheduled on this node yet?"

echo "== sample pod veth mtu (expect 1230) =="
kubectl -n "${NAMESPACE}" run mtu-probe --rm -i --restart=Never --image=busybox --command -- sh -c 'ip -o link show eth0'

if [ -n "${PEER_POD_IP}" ]; then
  echo "== fragmentation boundary test against ${PEER_POD_IP} =="
  echo "expect SUCCESS at payload 1202 (1230 - 20 IP - 8 ICMP):"
  kubectl -n "${NAMESPACE}" run mtu-ping-ok --rm -i --restart=Never --image=busybox --command -- \
    ping -M do -s 1202 -c 3 "${PEER_POD_IP}"
  echo "expect FAILURE ('Message too long' / 100% loss) at payload 1203:"
  kubectl -n "${NAMESPACE}" run mtu-ping-fail --rm -i --restart=Never --image=busybox --command -- \
    ping -M do -s 1203 -c 3 "${PEER_POD_IP}" || echo "(failure above is EXPECTED -- confirms the 1230 boundary)"
else
  echo "== fragmentation boundary test skipped (no peer pod IP given) =="
  echo "Re-run as: verify_mtu.sh ${NAMESPACE} <pod-ip-on-the-other-host>"
fi
