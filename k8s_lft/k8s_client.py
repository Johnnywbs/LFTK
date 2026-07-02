"""Thin wrapper around the `kubernetes` client library that plays the same
role profissa_lft/node.py's subprocess+`docker` CLI calls play today:

    docker run ...          -> apply(manifest)
    docker inspect (wait)   -> wait_ready(kind, name, namespace)
    docker exec <n> <cmd>   -> exec_in_pod(label_selector, namespace, command)
    docker cp <n>:<p> <dst> -> copy_from_pod(label_selector, namespace, path, dst)
    docker kill && docker rm -> delete_by_label(namespace, label_selector)

Kept as plain functions (not a class) on purpose -- see the migration plan,
section 2: a Node-like class can wrap these later for the final library,
but a PoC scoped to dash/iperf doesn't need the extra layer.
"""
from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

from kubernetes import client, config
from kubernetes.stream import stream

from .manifests import LABEL_APP


def load_kube_config(kube_config_path: str | None = None) -> None:
    """Call once at process start. Mirrors there being no equivalent step
    in the Docker world (the `docker` CLI just talks to the local daemon
    socket) -- the kubernetes client needs an explicit kubeconfig load."""
    if kube_config_path:
        config.load_kube_config(config_file=kube_config_path)
    else:
        config.load_kube_config()


# ---------------------------------------------------------------------------
# apply() -- direct replacement for Node.instantiate()'s `docker run`
# ---------------------------------------------------------------------------

def apply(manifest):
    """Creates the given typed object in the cluster. Dispatches on the
    concrete kubernetes.client type. Returns the API response object."""
    namespace = manifest.metadata.namespace or "default"
    if isinstance(manifest, client.V1Deployment):
        return client.AppsV1Api().create_namespaced_deployment(namespace, manifest)
    if isinstance(manifest, client.V1DaemonSet):
        return client.AppsV1Api().create_namespaced_daemon_set(namespace, manifest)
    if isinstance(manifest, client.V1Job):
        return client.BatchV1Api().create_namespaced_job(namespace, manifest)
    if isinstance(manifest, client.V1Service):
        return client.CoreV1Api().create_namespaced_service(namespace, manifest)
    if isinstance(manifest, client.V1Secret):
        return client.CoreV1Api().create_namespaced_secret(namespace, manifest)
    if isinstance(manifest, client.V1PersistentVolumeClaim):
        return client.CoreV1Api().create_namespaced_persistent_volume_claim(namespace, manifest)
    raise TypeError(f"apply(): unsupported manifest type {type(manifest)!r}")


# ---------------------------------------------------------------------------
# wait_ready() -- direct replacement for the implicit "container is up"
# assumption the Docker code makes right after `docker run -d`
# ---------------------------------------------------------------------------

def wait_ready(kind: str, name: str, namespace: str, timeout_s: int = 120) -> None:
    """kind is "deployment" or "job". Polls every 2s until ready/succeeded,
    raises TimeoutError or RuntimeError (job failed) otherwise."""
    apps = client.AppsV1Api()
    batch = client.BatchV1Api()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if kind == "deployment":
            dep = apps.read_namespaced_deployment_status(name, namespace)
            if (dep.status.ready_replicas or 0) >= (dep.spec.replicas or 1):
                return
        elif kind == "job":
            job = batch.read_namespaced_job_status(name, namespace)
            if job.status.succeeded:
                return
            if job.status.failed:
                raise RuntimeError(f"Job {name!r} in namespace {namespace!r} failed")
        else:
            raise ValueError(f"unknown kind {kind!r}, expected 'deployment' or 'job'")
        time.sleep(2)
    raise TimeoutError(f"{kind} {name!r} not ready after {timeout_s}s")


def _find_pod(label_selector: str, namespace: str) -> str:
    pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=label_selector).items
    if not pods:
        raise LookupError(f"no pod found for selector {label_selector!r} in namespace {namespace!r}")
    return pods[0].metadata.name


def label_selector_for(name: str) -> str:
    """Selector matching the labels every build_* function in manifests.py
    stamps on its pods (LABEL_APP=<name>)."""
    return f"{LABEL_APP}={name}"


def read_pod_log(label_selector: str, namespace: str) -> str:
    """For Jobs (dash-client/iperf-client): the container already ran to
    completion, so exec_in_pod() can't reach it -- its stdout (e.g. the
    iperf3 `-J` JSON output) is only available as the pod's log."""
    pod_name = _find_pod(label_selector, namespace)
    return client.CoreV1Api().read_namespaced_pod_log(pod_name, namespace)


def get_pod_ip(label_selector: str, namespace: str) -> str:
    """Used to ping the dash-server pod directly from the client's
    physical host OS -- see migration plan section 3.1 for why this
    exercises the exact cross-host Flannel/Tailscale packet path."""
    pod_name = _find_pod(label_selector, namespace)
    pod = client.CoreV1Api().read_namespaced_pod(pod_name, namespace)
    return pod.status.pod_ip


# ---------------------------------------------------------------------------
# exec_in_pod() -- direct replacement for Node.run()'s `docker exec`
# ---------------------------------------------------------------------------

def exec_in_pod(label_selector: str, namespace: str, command: list[str]) -> str:
    pod_name = _find_pod(label_selector, namespace)
    return stream(
        client.CoreV1Api().connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


# ---------------------------------------------------------------------------
# copy_from_pod() -- direct replacement for Node.copyContainerToLocal()'s
# `docker cp`. Uses the same "tar over exec" trick `kubectl cp` uses
# internally, since the K8s API has no native file-copy verb.
# ---------------------------------------------------------------------------

def copy_from_pod(label_selector: str, namespace: str, container_path: str, local_dest: Path) -> None:
    pod_name = _find_pod(label_selector, namespace)
    resp = stream(
        client.CoreV1Api().connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=["tar", "cf", "-", container_path],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )

    buf = io.BytesIO()
    try:
        while resp.is_open():
            resp.update(timeout=5)
            if resp.peek_stdout():
                chunk = resp.read_stdout()
                buf.write(chunk.encode("utf-8", errors="surrogateescape") if isinstance(chunk, str) else chunk)
            if resp.peek_stderr():
                # tar/exec errors surface here; drain so the loop can terminate
                resp.read_stderr()
    finally:
        resp.close()

    buf.seek(0)
    local_dest = Path(local_dest)
    local_dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=buf, mode="r:") as tf:
        tf.extractall(path=local_dest)


# ---------------------------------------------------------------------------
# delete_by_label() -- best-effort cleanup, mirrors Node.delete()'s
# try/except-wrapped `docker kill && docker rm`
# ---------------------------------------------------------------------------

def delete_by_label(namespace: str, label_selector: str) -> None:
    apps = client.AppsV1Api()
    batch = client.BatchV1Api()
    core = client.CoreV1Api()
    cleanup_calls = (
        lambda: apps.delete_collection_namespaced_deployment(namespace, label_selector=label_selector),
        lambda: batch.delete_collection_namespaced_job(
            namespace, label_selector=label_selector, propagation_policy="Background"
        ),
        lambda: core.delete_collection_namespaced_service(namespace, label_selector=label_selector),
        lambda: core.delete_collection_namespaced_pod(namespace, label_selector=label_selector),
    )
    for call in cleanup_calls:
        try:
            call()
        except Exception:
            pass
