#!/bin/bash
# Desfaz completamente o ambiente lft-k3s.
# Rode no MASTER primeiro, depois no WORKER (com a flag --worker).
#
# Uso:
#   ./teardown.sh           -> limpa tudo no Master
#   ./teardown.sh --worker  -> limpa tudo no Worker

set -euo pipefail

WORKER_MODE=false
[[ "${1:-}" == "--worker" ]] && WORKER_MODE=true

RED='\e[1;31m'
YLW='\e[1;33m'
GRN='\e[1;32m'
NC='\e[0m'

step() { echo -e "${YLW}[teardown] $*${NC}"; }
ok()   { echo -e "${GRN}[ok] $*${NC}"; }
warn() { echo -e "${RED}[warn] $*${NC}"; }

# ─────────────────────────────────────────────
# PARTE COMUM (Master + Worker)
# ─────────────────────────────────────────────

step "Desinstalando K3s..."
if command -v k3s-uninstall.sh &>/dev/null; then
  sudo k3s-uninstall.sh
  ok "K3s server removido"
elif command -v k3s-agent-uninstall.sh &>/dev/null; then
  sudo k3s-agent-uninstall.sh
  ok "K3s agent removido"
else
  warn "Nenhum script de desinstalação do K3s encontrado — talvez já removido"
fi

step "Removendo dados do K3s..."
sudo rm -rf /etc/rancher/k3s /var/lib/rancher/k3s /var/lib/kubelet
sudo rm -f /etc/cni/net.d/10-flannel.conflist /etc/cni/net.d/flannel.conflist
sudo rm -rf /etc/cni/net.d/multus.d /etc/cni/net.d/whereabouts.d
ok "Dados do K3s limpos"

step "Removendo interfaces de rede residuais..."
for iface in flannel.1 cni0 lft-br-master lft-br-worker lft-vxbr lft-vxlan; do
  if ip link show "$iface" &>/dev/null; then
    sudo ip link delete "$iface" 2>/dev/null && ok "Interface $iface removida" || warn "Falha ao remover $iface"
  fi
done

step "Removendo regras iptables do K3s..."
# K3s cria chains CNI-ISOLATION-*, KUBE-* e FLANNEL-FWD
for table in filter nat mangle; do
  sudo iptables -t "$table" -F 2>/dev/null || true
  sudo iptables -t "$table" -X 2>/dev/null || true
done
ok "Chains iptables limpas"

# ─────────────────────────────────────────────
# APENAS MASTER
# ─────────────────────────────────────────────

if [[ "$WORKER_MODE" == false ]]; then

  step "Removendo Helm..."
  if command -v helm &>/dev/null; then
    sudo rm -f "$(command -v helm)"
    ok "Helm removido"
  else
    warn "Helm não encontrado"
  fi

  step "Parando e desabilitando servidor NFS..."
  sudo systemctl stop nfs-kernel-server 2>/dev/null || true
  sudo systemctl disable nfs-kernel-server 2>/dev/null || true

  step "Removendo exportação NFS (/srv/lft_results)..."
  sudo sed -i '\|/srv/lft_results|d' /etc/exports 2>/dev/null || true
  sudo exportfs -a 2>/dev/null || true

  step "Removendo diretório de resultados NFS..."
  echo -e "${RED}ATENÇÃO: Isso apaga todos os resultados salvos em /srv/lft_results${NC}"
  read -rp "Confirma remoção de /srv/lft_results? [s/N] " CONFIRM
  if [[ "${CONFIRM,,}" == "s" ]]; then
    sudo rm -rf /srv/lft_results
    ok "/srv/lft_results removido"
  else
    ok "/srv/lft_results mantido"
  fi

  step "Removendo ConfigMap VXLAN (era aplicado com kubectl)..."
  # Se kubeconfig ainda existir de alguma instalação parcial, tenta limpar
  if kubectl get configmap lft-vxlan-config &>/dev/null 2>&1; then
    kubectl delete configmap lft-vxlan-config
  fi

fi

# ─────────────────────────────────────────────
# OPCIONAL — Tailscale
# ─────────────────────────────────────────────

echo ""
read -rp "Deseja remover o Tailscale também? [s/N] " RM_TS
if [[ "${RM_TS,,}" == "s" ]]; then
  step "Removendo Tailscale..."
  if command -v tailscale &>/dev/null; then
    sudo tailscale down 2>/dev/null || true
    # Detecção de método de instalação
    if dpkg -l tailscale &>/dev/null 2>&1; then
      sudo apt-get remove --purge -y tailscale
      sudo apt-get autoremove -y
    else
      warn "Tailscale não instalado via apt — remova manualmente se necessário"
    fi
    ok "Tailscale removido"
  else
    warn "Tailscale não encontrado"
  fi
else
  ok "Tailscale mantido"
fi

echo ""
echo -e "${GRN}==================================================${NC}"
if [[ "$WORKER_MODE" == false ]]; then
  echo -e "${GRN}  Master limpo. Execute agora no Worker:         ${NC}"
  echo -e "${GRN}  ./teardown.sh --worker                        ${NC}"
else
  echo -e "${GRN}  Worker limpo. Ambiente totalmente resetado.   ${NC}"
fi
echo -e "${GRN}==================================================${NC}"
