#!/bin/bash
set -e

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m       Iniciando Setup do Master (Lenovo)        \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Dependencias
echo -e "\e[1;33m[1/6] Instalando dependencias (NFS, jq, curl)...\e[0m"
sudo apt-get update -qq
sudo apt-get install -y curl jq nfs-kernel-server nfs-common

# 2. Servidor NFS
echo -e "\e[1;33m[2/6] Configurando servidor NFS...\e[0m"
sudo mkdir -p /srv/lft_results
sudo chmod 777 /srv/lft_results
echo "/srv/lft_results *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports > /dev/null
sudo exportfs -a
sudo systemctl enable --now nfs-kernel-server

# 3. Tailscale
echo -e "\e[1;33m[3/6] Instalando Tailscale...\e[0m"
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
echo -e "\e[1;32m-> Autentique o Tailscale se solicitado:\e[0m"
# 2>&1 garante que o link de autenticacao aparece no terminal (vem no stderr)
sudo tailscale up 2>&1

echo -e "\e[1;33mAguardando Tailscale conectar...\e[0m"
until tailscale ip -4 2>/dev/null | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; do sleep 2; done

MASTER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Master (Tailscale): $MASTER_IP\e[0m\n"

# 4. K3s Server
echo -e "\e[1;33m[4/6] Instalando K3s server...\e[0m"
curl -4 -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --node-ip=${MASTER_IP} \
  --advertise-address=${MASTER_IP} \
  --flannel-iface=tailscale0 \
  --disable=traefik" sh -s -

echo -e "\e[1;33mAguardando apiserver ficar pronto...\e[0m"
until sudo k3s kubectl get nodes &>/dev/null; do sleep 2; done
echo -e "\e[1;33mAguardando rede estabilizar...\e[0m"
sleep 10

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 5. Helm + Multus + Whereabouts
echo -e "\e[1;33m[5/6] Instalando Helm, Multus e Whereabouts...\e[0m"

if ! command -v helm &>/dev/null; then
  # -4: forca IPv4 — K3s nao configura rotas IPv6, causando ECONNRESET em dominios que resolvem para IPv6 primeiro
  curl -4 -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

# Multus via rke2-charts — paths de CNI corretos para K3s
helm repo add rke2-charts https://rke2-charts.rancher.io > /dev/null 2>&1
helm repo update > /dev/null 2>&1
helm upgrade --install multus rke2-charts/rke2-multus \
  --namespace kube-system \
  --set config.confDir=/var/lib/rancher/k3s/agent/etc/cni/net.d \
  --set config.binDir=/var/lib/rancher/k3s/data/cni \
  --wait --timeout=120s

# Whereabouts IPAM — nao tem chart Helm, usa manifestos oficiais do repositorio
WHEREABOUTS_VERSION=$(curl -4 -s https://api.github.com/repos/k8snetworkplumbingwg/whereabouts/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
BASE_URL="https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/${WHEREABOUTS_VERSION}/doc/crds"
kubectl apply -f "${BASE_URL}/whereabouts.cni.cncf.io_ippools.yaml"
kubectl apply -f "${BASE_URL}/whereabouts.cni.cncf.io_overlappingrangeipreservations.yaml"
kubectl apply -f "${BASE_URL}/whereabouts.cni.cncf.io_nodeslicepools.yaml"
kubectl apply -f "https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/${WHEREABOUTS_VERSION}/doc/crds/daemonset-install.yaml"
echo -e "\e[1;33mAguardando Whereabouts ficar pronto...\e[0m"
kubectl rollout status daemonset/whereabouts -n kube-system --timeout=120s

TOKEN=$(sudo cat /var/lib/rancher/k3s/server/node-token)

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m  MASTER PRONTO — rode agora o setup-worker.sh   \e[0m"
echo -e "\e[1;32m  no Destroyer com os dados abaixo:              \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "  MASTER_IP : \e[1;37m${MASTER_IP}\e[0m"
echo -e "  TOKEN     : \e[1;37m${TOKEN}\e[0m"
echo -e "\e[1;32m=================================================\e[0m\n"

# 6. Aguarda IP do worker para criar o ConfigMap VXLAN
echo -e "\e[1;33m[6/6] Aguardando IP do Worker...\e[0m"
echo -e "\e[1;33mApos o setup-worker.sh terminar, cole o IP Tailscale do Worker:\e[0m"
read -p "WORKER_IP: " WORKER_IP

echo -e "\e[1;33mAguardando worker entrar no cluster...\e[0m"
until kubectl get node destroyer-vpceg17fb &>/dev/null; do sleep 3; done
echo -e "\e[1;32mWorker detectado no cluster.\e[0m"

kubectl create configmap lft-vxlan-config \
  --from-literal=master-ip="$MASTER_IP" \
  --from-literal=worker-ip="$WORKER_IP" \
  --from-literal=vxlan-id="100" \
  --from-literal=vxlan-port="4789" \
  --from-literal=vxlan-dev="lft-vxlan" \
  --from-literal=bridge-dev="lft-vxbr" \
  --from-literal=vxlan-mtu="1230" \
  --from-literal=pod-subnet="10.20.0.0/24" \
  --dry-run=client -o yaml | kubectl apply -f -

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m  CLUSTER PRONTO. Execute o experimento com:     \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "  kubectl apply -f manifests/          # experimento 1"
echo -e "  kubectl apply -f manifests-vxlan/    # experimento 2 (VXLAN)"
echo -e "\e[1;32m=================================================\e[0m\n"
