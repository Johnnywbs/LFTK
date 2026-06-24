#!/bin/bash
set -e

MANIFESTS_DIR="$(dirname "$0")/manifests"

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m     Executando Experimento LFT no K8s           \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# --- Pré-checks ---

echo -e "\e[1;33m[PRE-CHECK] Verificando nodes do cluster...\e[0m"
if ! kubectl get nodes -l lft-role=worker --no-headers 2>/dev/null | grep -q .; then
  echo -e "\e[1;31m[ERRO] Nenhum nó com label 'lft-role=worker' encontrado!\e[0m"
  echo -e "\e[1;33mExecute no master:\e[0m"
  echo -e "\e[1;37m  kubectl get nodes\e[0m"
  echo -e "\e[1;37m  kubectl label node <nome-do-destroyer> lft-role=worker\e[0m"
  exit 1
fi
WORKER_NAME=$(kubectl get nodes -l lft-role=worker --no-headers | awk '{print $1}')
echo -e "\e[1;32m-> Worker: $WORKER_NAME\e[0m"

MASTER_NAME=$(kubectl get nodes -l node-role=master --no-headers 2>/dev/null | awk '{print $1}')
if [ -z "$MASTER_NAME" ]; then
  echo -e "\e[1;31m[ERRO] Nenhum nó com label 'node-role=master' encontrado!\e[0m"
  echo -e "\e[1;37m  kubectl label node $(kubectl get nodes --no-headers | grep -v $WORKER_NAME | awk '{print $1}') node-role=master\e[0m"
  exit 1
fi
echo -e "\e[1;32m-> Master: $MASTER_NAME\e[0m"

echo -e "\e[1;33m[PRE-CHECK] Verificando Multus...\e[0m"
if ! kubectl get daemonset kube-multus-ds -n kube-system > /dev/null 2>&1; then
  echo -e "\e[1;31m[ERRO] Multus não encontrado. Execute setup-master.sh primeiro.\e[0m"
  exit 1
fi
echo -e "\e[1;32m-> Multus OK\e[0m"

echo -e "\e[1;33m[PRE-CHECK] Verificando Whereabouts...\e[0m"
if ! kubectl get daemonset whereabouts -n kube-system > /dev/null 2>&1; then
  echo -e "\e[1;31m[ERRO] Whereabouts não encontrado. Execute setup-master.sh primeiro.\e[0m"
  exit 1
fi
echo -e "\e[1;32m-> Whereabouts OK\e[0m\n"

# --- Limpeza ---

echo -e "\e[1;33m[0/5] Limpando recursos de execuções anteriores...\e[0m"
kubectl delete job lft-experiment -n lft --ignore-not-found > /dev/null 2>&1
kubectl delete pod lft-iperf-server -n lft --ignore-not-found > /dev/null 2>&1
# ClusterRoleBinding não permite alteração de roleRef via apply — deletar antes
kubectl delete clusterrolebinding whereabouts --ignore-not-found > /dev/null 2>&1
# Aguardar remoção para liberação do IP Whereabouts
sleep 5
echo -e "\e[1;32m-> Limpeza concluída\e[0m\n"

# --- Aplicar manifests ---

echo -e "\e[1;33m[1/5] Aplicando namespace, armazenamento e Whereabouts...\e[0m"
kubectl apply -f "$MANIFESTS_DIR/1-namespace.yaml"
kubectl apply -f "$MANIFESTS_DIR/2-storage.yaml"
kubectl apply -f "$MANIFESTS_DIR/0-whereabouts.yaml"

echo -e "\e[1;33m[2/5] Aplicando rede VXLAN (NAD)...\e[0m"
kubectl apply -f "$MANIFESTS_DIR/3-network.yaml"

echo -e "\e[1;33m[3/5] Aplicando configurações do experimento...\e[0m"
kubectl apply -f "$MANIFESTS_DIR/4-config.yaml"

echo -e "\e[1;33m[4/5] Iniciando servidor iperf no worker ($WORKER_NAME)...\e[0m"
kubectl apply -f "$MANIFESTS_DIR/5-server.yaml"

echo -e "\e[1;33m   Aguardando servidor ficar pronto...\e[0m"
kubectl wait --for=condition=Ready pod/lft-iperf-server -n lft --timeout=120s

SERVER_NET1=$(kubectl exec -n lft lft-iperf-server -- ip addr show net1 2>/dev/null | grep "inet " | awk '{print $2}')
echo -e "\e[1;32m-> Servidor pronto no $WORKER_NAME. IP VXLAN (net1): $SERVER_NET1\e[0m\n"

echo -e "\e[1;33m[5/5] Lançando Job do experimento no master ($MASTER_NAME)...\e[0m"
kubectl apply -f "$MANIFESTS_DIR/6-experiment-job.yaml"

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m EXPERIMENTO INICIADO!                           \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e " Servidor VXLAN: $WORKER_NAME ($SERVER_NET1)"
echo -e " Cliente (Job):  $MASTER_NAME (10.20.0.11)"
echo -e "\nAcompanhe os logs:"
echo -e "\e[1;37m  kubectl logs -n lft -l job-name=lft-experiment -f\e[0m"
echo -e "\nVerifique os resultados:"
echo -e "\e[1;37m  ls /srv/lft_results/\e[0m"
echo -e "\e[1;37m  grep rtt /srv/lft_results/snapshot_2/ping.txt\e[0m\n"
