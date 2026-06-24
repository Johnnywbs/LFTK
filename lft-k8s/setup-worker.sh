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

# 6. Instalando o plugin vxlan-cni no path do K3s
echo -e "\e[1;33m[5/5] Instalando plugin vxlan-cni...\e[0m"
# Aguardar K3s criar os diretórios
sleep 5
VXLAN_CNI_URL="https://github.com/phdata/vxlan-cni/releases/latest/download/vxlan-cni-amd64.tgz"
wget -qO /tmp/vxlan-cni.tgz "$VXLAN_CNI_URL"
sudo mkdir -p /var/lib/rancher/k3s/data/cni
sudo tar xzf /tmp/vxlan-cni.tgz -C /var/lib/rancher/k3s/data/cni/ 2>/dev/null || \
  sudo tar xzf /tmp/vxlan-cni.tgz -C /opt/cni/bin/
rm /tmp/vxlan-cni.tgz
echo -e "\e[1;32m-> vxlan-cni instalado\e[0m"

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
