#!/bin/bash
set -e

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m      Iniciando Setup do Worker (Destroyer)      \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Coletando Dados do Master
echo -e "\e[1;33mInsira as informações geradas pelo script do Master:\e[0m"
read -p "Digite o IP do Master (ex: 100.x.x.x): " MASTER_IP
read -p "Digite o TOKEN do Master: " TOKEN
echo ""

# 2. Dependências
echo -e "\e[1;33m[1/5] Instalando dependências (NFS Client, iproute2, wget)...\e[0m"
sudo apt-get update > /dev/null 2>&1
sudo apt-get install -y curl nfs-common iproute2 wget > /dev/null 2>&1

# 3. Tailscale
echo -e "\e[1;33m[2/5] Instalando Tailscale...\e[0m"
curl -fsSL https://tailscale.com/install.sh | sh > /dev/null 2>&1
echo -e "\e[1;32m-> Por favor, autentique o Tailscale no link abaixo (se solicitado):\e[0m"
sudo tailscale up

WORKER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Worker capturado: $WORKER_IP\e[0m\n"

# 4. Ajuste de MTU do Tailscale para suportar VXLAN sem fragmentação
# Tailscale MTU=1280, VXLAN overhead=50 bytes → MTU interno precisa ser ≤1230
echo -e "\e[1;33m[3/5] Ajustando MTU do Tailscale para VXLAN (1230)...\e[0m"
sudo mkdir -p /etc/systemd/network
cat <<EOF | sudo tee /etc/systemd/network/tailscale0.network > /dev/null
[Match]
Name=tailscale0

[Link]
MTUBytes=1230
EOF
sudo ip link set dev tailscale0 mtu 1230 2>/dev/null || true
echo -e "\e[1;32m-> MTU ajustado para 1230\e[0m"

# 5. Instalando K3s Agent
echo -e "\e[1;33m[4/5] Instalando Agente do K3s e conectando ao Master...\e[0m"
curl -sfL https://get.k3s.io | K3S_URL="https://${MASTER_IP}:6443" K3S_TOKEN="${TOKEN}" sh -s - agent \
  --node-ip=${WORKER_IP} \
  --flannel-iface=tailscale0 > /dev/null 2>&1

# 6. Criar symlink do diretório CNI do K3s para o path padrão esperado pelo Multus
echo -e "\e[1;33m[5/5a] Criando symlink do CNI config do K3s para /etc/cni/net.d...\e[0m"
sudo mkdir -p /etc/cni/net.d
K3S_CNI_DIR="/var/lib/rancher/k3s/agent/etc/cni/net.d"
until [ -d "$K3S_CNI_DIR" ] && ls "$K3S_CNI_DIR"/*.conflist > /dev/null 2>&1; do
  sleep 2
done
for f in "$K3S_CNI_DIR"/*; do
  sudo ln -sf "$f" /etc/cni/net.d/$(basename "$f") 2>/dev/null || true
done
echo -e "\e[1;32m-> Symlinks criados de $K3S_CNI_DIR para /etc/cni/net.d\e[0m"

# Criar symlink do plugin macvlan no bundle do K3s
echo -e "\e[1;33m[5/5] Habilitando plugin macvlan no K3s...\e[0m"
sleep 5
CNI_BIN=$(readlink -f /var/lib/rancher/k3s/data/cni/bridge)
if [ -z "$CNI_BIN" ]; then
  echo -e "\e[1;31m[ERRO] Binário CNI do K3s não encontrado.\e[0m"
  exit 1
fi
sudo ln -sf "$CNI_BIN" /var/lib/rancher/k3s/data/cni/macvlan
echo -e "\e[1;32m-> Symlink macvlan criado -> $CNI_BIN\e[0m"

# Verificar interface flannel.1
if ip link show flannel.1 > /dev/null 2>&1; then
  echo -e "\e[1;32m-> Interface flannel.1 encontrada — Multus usará macvlan sobre ela\e[0m"
else
  echo -e "\e[1;31m[ERRO] Interface flannel.1 nao encontrada.\e[0m"
  echo -e "\e[1;31m       Verifique se o K3s e o Flannel estao rodando.\e[0m"
  exit 1
fi

# Abrir porta VXLAN no firewall
sudo ufw allow 4789/udp 2>/dev/null || true

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m WORKER CONFIGURADO E CONECTADO COM SUCESSO!     \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "Worker IP: \e[1;37m$WORKER_IP\e[0m"
echo -e "\n\e[1;33mVolte ao Master e execute:\e[0m"
echo -e "\e[1;37m  kubectl get nodes\e[0m  (verifique se este nó aparece)"
echo -e "\e[1;37m  kubectl label node $(hostname) lft-role=worker\e[0m"
echo -e "\e[1;37m  bash lft-k8s/run.sh\e[0m\n"
