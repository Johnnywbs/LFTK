"""2x2 factorial experiment: Docker (control) vs K3s (treatment) x light
(DASH only) vs heavy (DASH + concurrent iperf) load, r repetitions per
cell, exported as one long-format CSV ready for a two-way ANOVA (see
anova.py).

Scope (see migration plan): only dash_client/dash_server/iperf are
exercised. The Docker condition uses plain `docker run --network host`
addressed by Tailscale IP -- no OVS/ONOS SDN topology, since that model
is single-host by construction (see profissa_lft/node.py) and replicating
it across 2 physical hosts is neither practical nor necessary to compare
throughput/latency/DASH-bitrate/CPU between the two orchestration
abstractions.

Run as (from the `lft/` directory):

    python3 k8s_lft/experiment/factorial_experiment.py \\
        --server-tailscale-ip 100.x.x.1 --client-tailscale-ip 100.x.x.2 \\
        --server-hostname host-a --client-hostname host-b \\
        --ssh-user ubuntu --ssh-key ~/.ssh/id_ed25519 \\
        --kube-config ~/.kube/config --r 30
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import random
import re
import statistics
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import paramiko

# Allow running both as `python3 k8s_lft/experiment/factorial_experiment.py`
# (direct script) and as `python3 -m k8s_lft.experiment.factorial_experiment`
# (module) -- mirrors the sys.path bootstrap already used by
# onos_topologies/dash_topology/dash_experiment.py.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from k8s_lft import k8s_client, manifests  # noqa: E402

# ---------------------------------------------------------------------------
# Constants / schema
# ---------------------------------------------------------------------------

FACTOR_A_LEVELS = ("docker", "k3s")
FACTOR_B_LEVELS = ("light", "heavy")  # light = dash only, heavy = dash + concurrent iperf
DEFAULT_REPETITIONS = 30

RESULT_FIELDS = [
    "run_id", "order_idx", "seed", "timestamp_start", "timestamp_end",
    "factor_a", "factor_b", "repetition",
    "host_server", "host_client",
    "throughput_mbps", "iperf_retransmits",
    "latency_ms_mean", "latency_ms_p95", "packet_loss_pct",
    "dash_bitrate_kbps_mean", "dash_stall_rate", "dash_n_iterations",
    "cpu_pct_server", "cpu_pct_client",
    "status", "notes",
]

# Reused verbatim from onos_topologies/dash_topology/utils.py -- matches
# both plain and `ping -D` (timestamped) output lines.
_PING_RE = re.compile(
    r"^(?:\[([\d\.]+)\]\s+)?(\d+)\s+bytes\s+from\s+([a-fA-F0-9\.:]+):\s+"
    r"icmp_seq=(\d+)\s+ttl=(\d+)\s+time=([\d\.]+)\s*ms"
)


# ---------------------------------------------------------------------------
# 1. Run plan (randomized order, fixed/logged seed -- required for ANOVA
#    validity, avoids systematic order/warm-up effects)
# ---------------------------------------------------------------------------

@dataclass
class RunSpec:
    order_idx: int
    factor_a: str
    factor_b: str
    repetition: int
    seed: int


def build_run_plan(r: int = DEFAULT_REPETITIONS, seed: int = 20260702) -> list[RunSpec]:
    combos = [
        (factor_a, factor_b, rep)
        for factor_a in FACTOR_A_LEVELS
        for factor_b in FACTOR_B_LEVELS
        for rep in range(1, r + 1)
    ]
    rng = random.Random(seed)
    rng.shuffle(combos)
    return [
        RunSpec(order_idx=i, factor_a=a, factor_b=b, repetition=rep, seed=seed)
        for i, (a, b, rep) in enumerate(combos)
    ]


def write_run_plan(plan: list[RunSpec], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(s) for s in plan], indent=2))


# ---------------------------------------------------------------------------
# Experiment configuration
# ---------------------------------------------------------------------------

@dataclass
class ExperimentConfig:
    server_tailscale_ip: str
    client_tailscale_ip: str
    server_hostname: str  # k8s node name (kubernetes.io/hostname) for server-side pods
    client_hostname: str  # k8s node name for client Jobs
    ssh_user: str = "ubuntu"
    ssh_key_path: Optional[str] = None
    namespace: str = "lft"
    iperf_port: int = 5201
    iperf_duration_s: int = 30
    remote_datadir_path: str = "/home/ubuntu/lft_datadir"
    remote_certs_path: str = "/home/ubuntu/lft_certs"
    dash_server_image: str = manifests.DASH_SERVER_IMAGE
    dash_client_image: str = manifests.DASH_CLIENT_IMAGE
    iperf_image: str = manifests.IPERF_IMAGE
    cert_secret_name: str = "dash-certs"


# ---------------------------------------------------------------------------
# SSH helpers (deliberately plain functions, no RemoteHost class -- one
# connection per call is simple/robust and cheap enough at this scale;
# see migration plan section 4 for why this stays a flat script)
# ---------------------------------------------------------------------------

def _ssh_connect(ip: str, user: str, key_path: Optional[str]) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, username=user, key_filename=key_path, timeout=15)
    return ssh


def _ssh_run(ip: str, user: str, key_path: Optional[str], command: str,
             timeout: Optional[int] = None) -> tuple[int, str, str]:
    ssh = _ssh_connect(ip, user, key_path)
    try:
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        ssh.close()


def _ssh_pull_dir_as_tar(ip: str, user: str, key_path: Optional[str], remote_dir: str, local_dest: Path) -> None:
    """Same "tar over exec" approach as k8s_client.copy_from_pod(), so both
    backends land results on disk the same way for collect_metrics()."""
    remote_path = Path(remote_dir)
    ssh = _ssh_connect(ip, user, key_path)
    try:
        cmd = f"tar cf - -C {remote_path.parent} {remote_path.name} 2>/dev/null"
        _, stdout, _ = ssh.exec_command(cmd)
        data = stdout.read()
        stdout.channel.recv_exit_status()
    finally:
        ssh.close()

    local_dest = Path(local_dest)
    local_dest.mkdir(parents=True, exist_ok=True)
    if not data:
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        tf.extractall(path=local_dest)


def _cpu_stat_from_text(proc_stat_text: str) -> tuple[int, int]:
    """Ported from onos_topologies/dash_topology/utils.py::_cpu_stat()."""
    for line in proc_stat_text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = (v[3] if len(v) > 3 else 0) + (v[4] if len(v) > 4 else 0)
            total = sum(v[:8]) if len(v) >= 8 else sum(v)
            return idle, total
    return 0, 0


def cpu_sample_start(ip: str, user: str, key_path: Optional[str]) -> tuple[int, int]:
    """CPU is always measured on the physical host's own OS (via SSH),
    for both Factor-A levels -- containers/pods without lxcfs see the
    host-wide /proc/stat anyway, so this keeps the comparison fair
    (see migration plan section 4, step 5)."""
    _, text, _ = _ssh_run(ip, user, key_path, "cat /proc/stat")
    return _cpu_stat_from_text(text)


def cpu_sample_end(ip: str, user: str, key_path: Optional[str], start: tuple[int, int]) -> Optional[float]:
    _, text, _ = _ssh_run(ip, user, key_path, "cat /proc/stat")
    idle1, total1 = _cpu_stat_from_text(text)
    idle0, total0 = start
    idle_d = max(0, idle1 - idle0)
    total_d = max(0, total1 - total0)
    if total_d <= 0:
        return None
    return 100.0 * (1.0 - (idle_d / total_d))


# ---------------------------------------------------------------------------
# 2. Docker condition (control) -- plain `docker run --network host`
# ---------------------------------------------------------------------------

def _docker_teardown(ip: str, cfg: ExperimentConfig, names: list[str]) -> None:
    joined = " ".join(names)
    _ssh_run(ip, cfg.ssh_user, cfg.ssh_key_path, f"docker rm -f {joined} >/dev/null 2>&1 || true")


def _run_dash_client_docker(client_ip: str, cfg: ExperimentConfig, server_ip: str) -> None:
    _ssh_run(
        client_ip, cfg.ssh_user, cfg.ssh_key_path,
        "docker exec dash-client bash -lc "
        f"'/usr/local/bin/dash-client -y -hostname {server_ip} -scheme http'",
        timeout=180,
    )


def _run_iperf_client_docker(client_ip: str, cfg: ExperimentConfig, server_ip: str, out_path: Path) -> None:
    _, out, _ = _ssh_run(
        client_ip, cfg.ssh_user, cfg.ssh_key_path,
        "docker exec iperf-client bash -lc "
        f"'iperf3 -c {server_ip} -p {cfg.iperf_port} -t {cfg.iperf_duration_s} -J'",
        timeout=cfg.iperf_duration_s + 30,
    )
    out_path.write_text(out)


def _run_ping_docker(client_ip: str, cfg: ExperimentConfig, server_ip: str) -> str:
    _, out, _ = _ssh_run(
        client_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"docker exec dash-client bash -lc 'ping -c 20 {server_ip}'",
        timeout=60,
    )
    return out


def run_docker_condition(spec: RunSpec, cfg: ExperimentConfig, run_dir: Path) -> dict:
    server_ip, client_ip = cfg.server_tailscale_ip, cfg.client_tailscale_ip

    _docker_teardown(server_ip, cfg, ["dash-server", "iperf-server"])
    _docker_teardown(client_ip, cfg, ["dash-client", "iperf-client"])
    _ssh_run(
        server_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"rm -rf {cfg.remote_datadir_path}/dash && mkdir -p {cfg.remote_datadir_path}",
    )

    _ssh_run(
        server_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"docker run -d --name=dash-server --network host "
        f"-v {cfg.remote_certs_path}:/certs:ro -v {cfg.remote_datadir_path}:/datadir "
        f"{cfg.dash_server_image} -datadir /datadir -http-listen-address :80 "
        f"-https-listen-address '' -prometheusx.listen-address :9999 "
        f"-tls-cert /certs/cert.pem -tls-key /certs/key.pem",
    )
    _ssh_run(
        server_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"docker run -d --name=iperf-server --network host --entrypoint bash "
        f"{cfg.iperf_image} -lc 'iperf3 -s -p {cfg.iperf_port}'",
    )
    _ssh_run(
        client_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"docker run -d --name=dash-client --network host --entrypoint sleep "
        f"{cfg.dash_client_image} infinity",
    )
    _ssh_run(
        client_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"docker run -d --name=iperf-client --network host --entrypoint sleep "
        f"{cfg.iperf_image} infinity",
    )
    time.sleep(3)  # let dash-server/iperf3 -s finish binding their ports

    cpu0_server = cpu_sample_start(server_ip, cfg.ssh_user, cfg.ssh_key_path)
    cpu0_client = cpu_sample_start(client_ip, cfg.ssh_user, cfg.ssh_key_path)

    iperf_json_local = run_dir / "iperf.json"
    if spec.factor_b == "light":
        _run_dash_client_docker(client_ip, cfg, server_ip)
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_run_dash_client_docker, client_ip, cfg, server_ip)
            f2 = ex.submit(_run_iperf_client_docker, client_ip, cfg, server_ip, iperf_json_local)
            f1.result()
            f2.result()

    cpu_pct_server = cpu_sample_end(server_ip, cfg.ssh_user, cfg.ssh_key_path, cpu0_server)
    cpu_pct_client = cpu_sample_end(client_ip, cfg.ssh_user, cfg.ssh_key_path, cpu0_client)

    ping_text = _run_ping_docker(client_ip, cfg, server_ip)

    dash_result_root = run_dir / "datadir"
    _ssh_pull_dir_as_tar(server_ip, cfg.ssh_user, cfg.ssh_key_path,
                          f"{cfg.remote_datadir_path}/dash", dash_result_root)

    _docker_teardown(server_ip, cfg, ["dash-server", "iperf-server"])
    _docker_teardown(client_ip, cfg, ["dash-client", "iperf-client"])

    return {
        "cpu_pct_server": cpu_pct_server,
        "cpu_pct_client": cpu_pct_client,
        "ping_text": ping_text,
        "iperf_json_path": iperf_json_local if spec.factor_b == "heavy" else None,
        "dash_result_root": dash_result_root,
    }


# ---------------------------------------------------------------------------
# 3. K3s condition (treatment) -- manifests.py + k8s_client.py
# ---------------------------------------------------------------------------

def _k8s_teardown(cfg: ExperimentConfig, extra_names: list[str]) -> None:
    for name in ["dash-server", "iperf-server", *extra_names]:
        try:
            k8s_client.delete_by_label(cfg.namespace, k8s_client.label_selector_for(name))
        except Exception:
            pass


def run_k8s_condition(spec: RunSpec, cfg: ExperimentConfig, run_dir: Path) -> dict:
    _k8s_teardown(cfg, [])  # best-effort clean slate from any previous failed run

    server_dep = manifests.build_dash_server_deployment(
        namespace=cfg.namespace, image=cfg.dash_server_image,
        cert_secret_name=cfg.cert_secret_name, node_selector_hostname=cfg.server_hostname,
    )
    server_svc = manifests.build_dash_server_service(namespace=cfg.namespace)
    iperf_dep = manifests.build_iperf_server_deployment(
        namespace=cfg.namespace, image=cfg.iperf_image,
        port=cfg.iperf_port, node_selector_hostname=cfg.server_hostname,
    )
    iperf_svc = manifests.build_iperf_server_service(namespace=cfg.namespace, port=cfg.iperf_port)

    for m in (server_dep, server_svc, iperf_dep, iperf_svc):
        k8s_client.apply(m)
    k8s_client.wait_ready("deployment", "dash-server", cfg.namespace)
    k8s_client.wait_ready("deployment", "iperf-server", cfg.namespace)

    cpu0_server = cpu_sample_start(cfg.server_tailscale_ip, cfg.ssh_user, cfg.ssh_key_path)
    cpu0_client = cpu_sample_start(cfg.client_tailscale_ip, cfg.ssh_user, cfg.ssh_key_path)

    dash_job_name = f"dash-client-{spec.order_idx}"
    dash_server_url = f"http://dash-server.{cfg.namespace}.svc.cluster.local"
    dash_job = manifests.build_dash_client_job(
        dash_job_name, dash_server_url, namespace=cfg.namespace,
        image=cfg.dash_client_image, node_selector_hostname=cfg.client_hostname,
    )

    iperf_job_name = None
    iperf_json_local = run_dir / "iperf.json"
    if spec.factor_b == "light":
        k8s_client.apply(dash_job)
        k8s_client.wait_ready("job", dash_job_name, cfg.namespace)
    else:
        iperf_job_name = f"iperf-client-{spec.order_idx}"
        iperf_server_host = f"iperf-server.{cfg.namespace}.svc.cluster.local"
        iperf_job = manifests.build_iperf_client_job(
            iperf_job_name, iperf_server_host, namespace=cfg.namespace,
            image=cfg.iperf_image, port=cfg.iperf_port, duration_s=cfg.iperf_duration_s,
            node_selector_hostname=cfg.client_hostname,
        )
        k8s_client.apply(dash_job)
        k8s_client.apply(iperf_job)  # both Jobs run concurrently once created
        k8s_client.wait_ready("job", dash_job_name, cfg.namespace)
        k8s_client.wait_ready("job", iperf_job_name, cfg.namespace, timeout_s=cfg.iperf_duration_s + 60)
        iperf_log = k8s_client.read_pod_log(k8s_client.label_selector_for(iperf_job_name), cfg.namespace)
        iperf_json_local.write_text(iperf_log)

    cpu_pct_server = cpu_sample_end(cfg.server_tailscale_ip, cfg.ssh_user, cfg.ssh_key_path, cpu0_server)
    cpu_pct_client = cpu_sample_end(cfg.client_tailscale_ip, cfg.ssh_user, cfg.ssh_key_path, cpu0_client)

    # Ping the dash-server POD directly from the client's physical host OS --
    # this exercises exactly the cross-host Flannel/Tailscale path described
    # in the migration plan (section 3.1), not just a same-host loopback.
    server_pod_ip = k8s_client.get_pod_ip(k8s_client.label_selector_for("dash-server"), cfg.namespace)
    _, ping_text, _ = _ssh_run(
        cfg.client_tailscale_ip, cfg.ssh_user, cfg.ssh_key_path,
        f"ping -c 20 {server_pod_ip}", timeout=60,
    )

    dash_result_root = run_dir / "datadir"
    k8s_client.copy_from_pod(
        k8s_client.label_selector_for("dash-server"), cfg.namespace, "/datadir", dash_result_root,
    )

    _k8s_teardown(cfg, [dash_job_name] + ([iperf_job_name] if iperf_job_name else []))

    return {
        "cpu_pct_server": cpu_pct_server,
        "cpu_pct_client": cpu_pct_client,
        "ping_text": ping_text,
        "iperf_json_path": iperf_json_local if spec.factor_b == "heavy" else None,
        "dash_result_root": dash_result_root,
    }


# ---------------------------------------------------------------------------
# 4. Metric parsing (dash_parser is new code, no equivalent exists today)
# ---------------------------------------------------------------------------

def find_latest_dash_result(root: Path) -> Optional[Path]:
    candidates = sorted(root.rglob("*.json.gz"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def parse_dash_result(json_gz_path: Path) -> dict:
    """Confirmed schema (inspected a real neubot/dash-server result file):
    {"client": [{"iteration", "rate" (kbps), "elapsed" (s),
                 "elapsed_target" (s), "received" (bytes), ...}, ...]}
    Bitrate = mean(rate). Stall proxy = fraction of iterations where the
    segment took longer to download than its playback budget."""
    with gzip.open(json_gz_path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("client", [])
    if not entries:
        return {"dash_bitrate_kbps_mean": None, "dash_stall_rate": None, "dash_n_iterations": 0}
    rates = [e["rate"] for e in entries if "rate" in e]
    stalls = sum(1 for e in entries if e.get("elapsed", 0) > e.get("elapsed_target", 0))
    return {
        "dash_bitrate_kbps_mean": statistics.mean(rates) if rates else None,
        "dash_stall_rate": stalls / len(entries),
        "dash_n_iterations": len(entries),
    }


def parse_ping_output(text: str) -> dict:
    times = []
    for line in text.splitlines():
        m = _PING_RE.match(line.strip())
        if m:
            times.append(float(m.group(6)))
    loss_match = re.search(r"(\d+)% packet loss", text)
    row = {
        "latency_ms_mean": None,
        "latency_ms_p95": None,
        "packet_loss_pct": float(loss_match.group(1)) if loss_match else None,
    }
    if times:
        times_sorted = sorted(times)
        p95_idx = max(0, int(len(times_sorted) * 0.95) - 1)
        row["latency_ms_mean"] = statistics.mean(times)
        row["latency_ms_p95"] = times_sorted[p95_idx]
    return row


def parse_iperf_json(json_path: Path) -> dict:
    try:
        data = json.loads(Path(json_path).read_text())
    except (json.JSONDecodeError, OSError):
        return {"throughput_mbps": None, "iperf_retransmits": None}
    end = data.get("end", {})
    received = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    bps = received.get("bits_per_second")
    return {
        "throughput_mbps": (bps / 1_000_000) if bps is not None else None,
        "iperf_retransmits": sent.get("retransmits"),
    }


def collect_metrics(spec: RunSpec, backend_result: dict) -> dict:
    row = {field: None for field in RESULT_FIELDS}
    row.update({
        "run_id": f"{spec.factor_a}-{spec.factor_b}-{spec.repetition}-{spec.order_idx}",
        "order_idx": spec.order_idx,
        "seed": spec.seed,
        "factor_a": spec.factor_a,
        "factor_b": spec.factor_b,
        "repetition": spec.repetition,
        "cpu_pct_server": backend_result.get("cpu_pct_server"),
        "cpu_pct_client": backend_result.get("cpu_pct_client"),
        "dash_n_iterations": 0,
        "status": "ok",
        "notes": "",
    })

    ping_text = backend_result.get("ping_text")
    if ping_text:
        row.update(parse_ping_output(ping_text))

    iperf_json_path = backend_result.get("iperf_json_path")
    if iperf_json_path and Path(iperf_json_path).exists():
        row.update(parse_iperf_json(iperf_json_path))

    dash_root = backend_result.get("dash_result_root")
    if dash_root and Path(dash_root).exists():
        latest = find_latest_dash_result(Path(dash_root))
        if latest:
            row.update(parse_dash_result(latest))
        else:
            row["status"] = "warning"
            row["notes"] = "no dash *.json.gz result found under datadir"
    else:
        row["status"] = "warning"
        row["notes"] = (row["notes"] + "; " if row["notes"] else "") + "datadir not retrieved"

    return row


def write_csv_row(csv_path: Path, row: dict) -> None:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------------------
# 5. CLI entrypoint
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="LFT 2x2 factorial experiment: Docker vs K3s x light vs heavy load",
    )
    parser.add_argument("--r", type=int, default=DEFAULT_REPETITIONS, help="repetitions per cell")
    parser.add_argument("--seed", type=int, default=20260702)
    parser.add_argument("--out", default="results/factorial")
    parser.add_argument("--server-tailscale-ip", required=True)
    parser.add_argument("--client-tailscale-ip", required=True)
    parser.add_argument("--server-hostname", required=True, help="k8s node name for server-side pods")
    parser.add_argument("--client-hostname", required=True, help="k8s node name for client Jobs")
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--ssh-key", default=None)
    parser.add_argument("--kube-config", default=None)
    parser.add_argument("--namespace", default="lft")
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--iperf-duration", type=int, default=30)
    parser.add_argument("--remote-datadir", default="/home/ubuntu/lft_datadir",
                         help="pre-existing dir on the server host, bind-mounted into dash-server (Docker condition)")
    parser.add_argument("--remote-certs", default="/home/ubuntu/lft_certs",
                         help="pre-existing dir on the server host with cert.pem/key.pem (Docker condition)")
    args = parser.parse_args(argv)

    cfg = ExperimentConfig(
        server_tailscale_ip=args.server_tailscale_ip,
        client_tailscale_ip=args.client_tailscale_ip,
        server_hostname=args.server_hostname,
        client_hostname=args.client_hostname,
        ssh_user=args.ssh_user,
        ssh_key_path=args.ssh_key,
        namespace=args.namespace,
        iperf_port=args.iperf_port,
        iperf_duration_s=args.iperf_duration,
        remote_datadir_path=args.remote_datadir,
        remote_certs_path=args.remote_certs,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "results.csv"

    plan = build_run_plan(r=args.r, seed=args.seed)
    write_run_plan(plan, out_dir / "run_plan.json")

    if any(s.factor_a == "k3s" for s in plan):
        k8s_client.load_kube_config(args.kube_config)

    for spec in plan:
        run_dir = out_dir / "runs" / f"run-{spec.order_idx:04d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            if spec.factor_a == "docker":
                backend_result = run_docker_condition(spec, cfg, run_dir)
            else:
                backend_result = run_k8s_condition(spec, cfg, run_dir)
            row = collect_metrics(spec, backend_result)
        except Exception as exc:  # keep the plan moving; one failed run != a lost dataset
            row = {field: None for field in RESULT_FIELDS}
            row.update({
                "run_id": f"{spec.factor_a}-{spec.factor_b}-{spec.repetition}-{spec.order_idx}",
                "order_idx": spec.order_idx, "seed": spec.seed,
                "factor_a": spec.factor_a, "factor_b": spec.factor_b,
                "repetition": spec.repetition, "status": "failed", "notes": str(exc),
            })
        ended = time.time()
        row["timestamp_start"] = started
        row["timestamp_end"] = ended
        row["host_server"] = cfg.server_tailscale_ip
        row["host_client"] = cfg.client_tailscale_ip
        write_csv_row(csv_path, row)
        print(f"[{spec.order_idx + 1}/{len(plan)}] {spec.factor_a}/{spec.factor_b} "
              f"rep={spec.repetition} -> status={row.get('status')}")

    print(f"Done. Results at {csv_path}")


if __name__ == "__main__":
    main()
