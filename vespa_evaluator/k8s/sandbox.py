"""
K8s Sandbox - manages Pod lifecycle for isolated test execution.

Each test case runs in its own Pod, providing:
    - Process isolation (no interference between tests)
    - Resource limits (CPU, memory per test)
    - Clean environment (no state leakage)
    - Automatic cleanup on completion/failure
    - Log collection from Pod stdout/stderr

The Pod runs the Vespa system test framework's AutoRunner against a specific
test file and method.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class PodPhase(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


@dataclass
class SandboxConfig:
    """Configuration for the K8s sandbox environment."""
    namespace: str = "vespa-eval"
    image: str = "vespaengine/vespa-systemtest-runner:latest"
    cpu_request: str = "500m"
    cpu_limit: str = "2"
    memory_request: str = "1Gi"
    memory_limit: str = "4Gi"
    timeout_seconds: int = 1200  # 20 minutes
    service_account: str = "vespa-eval-runner"
    node_selector: dict = field(default_factory=dict)
    # Vespa cluster config - tests need a running Vespa to talk to
    vespa_config_server: str = ""
    # Mount path for system-test repo
    test_repo_pvc: str = "system-test-pvc"
    # --- Safety guardrails ---
    max_pods: int = 10            # Hard cap, matches ResourceQuota
    pod_ttl_seconds: int = 7200   # Force-kill pods older than 2h
    test_repo_mount: str = "/system-test"


@dataclass
class PodResult:
    """Result from a completed Pod execution."""
    pod_name: str
    phase: PodPhase
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


class SandboxManager:
    """
    Manages K8s Pods as sandboxes for test execution.

    Uses the kubernetes Python client to create, monitor, and clean up Pods.
    Each Pod:
        1. Mounts the system-test repo (via PVC or git-sync init container)
        2. Runs a specific test: ruby tests/category/area/test.rb --run test_method
        3. Reports results via exit code and stdout/stderr
        4. Gets cleaned up after completion

    Usage:
        manager = SandboxManager(config=SandboxConfig(
            namespace="vespa-eval",
            vespa_config_server="vespa-configserver.vespa.svc:19071",
        ))

        pod_name = await manager.launch(
            test_file="tests/search/basicsearch/basic_search.rb",
            test_method="test_basicsearch",
            vespa_hosts=["vespa-node-0.vespa.svc"],
        )

        result = await manager.wait_for_completion(pod_name)
        await manager.cleanup(pod_name)
    """

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._active_pods: dict[str, float] = {}  # pod_name -> start_time
        self._k8s_client = None

    async def _ensure_client(self):
        """Lazy-init K8s client."""
        if self._k8s_client is not None:
            return
        try:
            from kubernetes import client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            self._k8s_client = client.CoreV1Api()
        except ImportError:
            logger.warning(
                "kubernetes package not installed. "
                "Using dry-run mode (no actual Pods will be created)."
            )

    def _pod_name(self, test_file: str, test_method: str) -> str:
        """Generate a unique Pod name from test identifiers."""
        # e.g., "vespa-eval-basicsearch-test-basicsearch-a1b2c3"
        area = test_file.split("/")[-2] if "/" in test_file else "unknown"
        short_method = test_method.replace("test_", "")[:20]
        suffix = uuid.uuid4().hex[:6]
        name = f"vespa-eval-{area}-{short_method}-{suffix}"
        # K8s name constraints: lowercase, alphanumeric and hyphens, max 63 chars
        name = name.lower().replace("_", "-")[:63]
        return name

    def _build_pod_manifest(
        self,
        pod_name: str,
        test_file: str,
        test_method: str,
        vespa_hosts: list[str],
    ) -> dict:
        """Build K8s Pod manifest for running a test."""
        host_args = []
        for host in vespa_hosts:
            host_args.extend(["--host", host])

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.config.namespace,
                "labels": {
                    "app": "vespa-evaluator",
                    "test-file": test_file.split("/")[-1].replace(".rb", ""),
                    "test-method": test_method[:63],
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "serviceAccountName": self.config.service_account,
                **(
                    {"nodeSelector": self.config.node_selector}
                    if self.config.node_selector
                    else {}
                ),
                "containers": [
                    {
                        "name": "test-runner",
                        "image": self.config.image,
                        "command": [
                            "ruby",
                            f"{self.config.test_repo_mount}/{test_file}",
                            *host_args,
                            "--run", test_method,
                        ],
                        "resources": {
                            "requests": {
                                "cpu": self.config.cpu_request,
                                "memory": self.config.memory_request,
                            },
                            "limits": {
                                "cpu": self.config.cpu_limit,
                                "memory": self.config.memory_limit,
                            },
                        },
                        "volumeMounts": [
                            {
                                "name": "test-repo",
                                "mountPath": self.config.test_repo_mount,
                                "readOnly": True,
                            }
                        ],
                        "env": [
                            {
                                "name": "VESPA_CONFIGSERVERS",
                                "value": self.config.vespa_config_server,
                            }
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "test-repo",
                        "persistentVolumeClaim": {
                            "claimName": self.config.test_repo_pvc,
                        },
                    }
                ],
                "activeDeadlineSeconds": self.config.timeout_seconds,
            },
        }

    async def launch(
        self,
        test_file: str,
        test_method: str,
        vespa_hosts: list[str] | None = None,
    ) -> str:
        """
        Launch a Pod to run a specific test case.

        Returns the Pod name for tracking.
        Raises RuntimeError if max_pods limit would be exceeded.
        """
        await self._ensure_client()

        # --- Safety: enforce pod cap ---
        await self._reap_expired_pods()
        if len(self._active_pods) >= self.config.max_pods:
            raise RuntimeError(
                f"Pod limit reached ({self.config.max_pods}). "
                f"Wait for running tests to complete or increase max_pods."
            )

        vespa_hosts = vespa_hosts or []
        pod_name = self._pod_name(test_file, test_method)
        manifest = self._build_pod_manifest(
            pod_name, test_file, test_method, vespa_hosts
        )

        if self._k8s_client:
            from kubernetes import client
            pod = client.V1Pod()
            # In production, deserialize manifest properly
            self._k8s_client.create_namespaced_pod(
                namespace=self.config.namespace,
                body=manifest,
            )
            logger.info(f"Launched pod {pod_name} for {test_file}::{test_method}")
        else:
            logger.info(
                f"[DRY-RUN] Would launch pod {pod_name} "
                f"for {test_file}::{test_method}"
            )

        self._active_pods[pod_name] = time.time()
        return pod_name

    async def wait_for_completion(
        self,
        pod_name: str,
        poll_interval: float = 5.0,
    ) -> PodResult:
        """Wait for a Pod to complete and return its result."""
        await self._ensure_client()
        start_time = self._active_pods.get(pod_name, time.time())

        if not self._k8s_client:
            # Dry-run mode: simulate completion
            await asyncio.sleep(0.1)
            return PodResult(
                pod_name=pod_name,
                phase=PodPhase.SUCCEEDED,
                exit_code=0,
                stdout="[DRY-RUN] Test passed",
                duration_seconds=0.1,
                started_at=start_time,
                finished_at=time.time(),
            )

        deadline = start_time + self.config.timeout_seconds
        while time.time() < deadline:
            pod = self._k8s_client.read_namespaced_pod_status(
                name=pod_name, namespace=self.config.namespace
            )
            phase = PodPhase(pod.status.phase)

            if phase in (PodPhase.SUCCEEDED, PodPhase.FAILED):
                # Collect logs
                try:
                    logs = self._k8s_client.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=self.config.namespace,
                        container="test-runner",
                    )
                except Exception:
                    logs = ""

                exit_code = 0
                if pod.status.container_statuses:
                    cs = pod.status.container_statuses[0]
                    if cs.state.terminated:
                        exit_code = cs.state.terminated.exit_code

                return PodResult(
                    pod_name=pod_name,
                    phase=phase,
                    exit_code=exit_code,
                    stdout=logs,
                    duration_seconds=time.time() - start_time,
                    started_at=start_time,
                    finished_at=time.time(),
                )

            await asyncio.sleep(poll_interval)

        # Timeout
        return PodResult(
            pod_name=pod_name,
            phase=PodPhase.Unknown,
            exit_code=-1,
            stderr="Pod execution timed out",
            duration_seconds=time.time() - start_time,
            started_at=start_time,
            finished_at=time.time(),
        )

    async def cleanup(self, pod_name: str):
        """Delete a completed Pod."""
        if self._k8s_client:
            try:
                self._k8s_client.delete_namespaced_pod(
                    name=pod_name,
                    namespace=self.config.namespace,
                )
                logger.info(f"Cleaned up pod {pod_name}")
            except Exception as e:
                logger.warning(f"Failed to cleanup pod {pod_name}: {e}")
        self._active_pods.pop(pod_name, None)

    async def cleanup_all(self):
        """Clean up all active Pods."""
        for pod_name in list(self._active_pods):
            await self.cleanup(pod_name)

    async def _reap_expired_pods(self):
        """Force-cleanup pods that exceed the TTL. Prevents resource leaks."""
        now = time.time()
        expired = [
            name for name, start in self._active_pods.items()
            if (now - start) > self.config.pod_ttl_seconds
        ]
        for pod_name in expired:
            logger.warning(
                f"Pod {pod_name} exceeded TTL ({self.config.pod_ttl_seconds}s), "
                f"force cleaning up"
            )
            await self.cleanup(pod_name)
