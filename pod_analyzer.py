from langflow.custom import Component
from langflow.io import Output, MessageTextInput
from langflow.schema import Message
import subprocess
import os

# Pod states that indicate errors
ERROR_STATES = [
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "OOMKilled",
    "Error",
    "CreateContainerConfigError",
    "InvalidImageName",
    "CreateContainerError",
    "ContainerCannotRun",
    "DeadlineExceeded",
    "Evicted",
    "Pending",
    "Terminating",
]

class KubectlPodAnalyzer(Component):
    display_name = "Kubectl Pod Error Analyzer"
    name = "KubectlPodAnalyzer"
    description = (
        "Finds all Kubernetes pods in error states (CrashLoopBackOff, ImagePullBackOff, OOMKilled, etc.) "
        "in a given namespace. Lists them, fetches logs, describe, and events for each, "
        "then provides RCA and fix suggestions."
    )
    icon = "terminal"

    inputs = [
        MessageTextInput(
            name="namespace",
            display_name="Namespace",
            info="Kubernetes namespace to scan for errored pods",
            value="default",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Output",
            name="output",
            method="analyze_pods",
        )
    ]

    KUBECTL = "/opt/homebrew/bin/kubectl"

    def _run_kubectl(self, cmd: list) -> str:
        cmd[0] = self.KUBECTL
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    "KUBECONFIG": os.path.expanduser("~/.kube/config"),
                }
            )
            return r.stdout or r.stderr or "(no output)"
        except subprocess.TimeoutExpired:
            return "(command timed out)"
        except Exception as e:
            return f"(error: {e})"

    def _get_errored_pods(self, namespace: str) -> list[dict]:
        """
        Run kubectl get pods and parse out pods in error states.
        Returns list of dicts with pod_name and error_state.
        """
        output = self._run_kubectl([
            "kubectl", "get", "pods", "-n", namespace,
            "--no-headers",
            "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase,READY:.status.containerStatuses[*].ready,REASON:.status.containerStatuses[*].state.waiting.reason"
        ])

        errored = []
        for line in output.splitlines():
            parts = line.split()
            if not parts:
                continue
            pod_name = parts[0]
            # Check all columns for any error state keyword
            line_upper = line
            for state in ERROR_STATES:
                if state.lower() in line_upper.lower():
                    errored.append({
                        "pod_name": pod_name,
                        "error_state": state,
                    })
                    break

        # Fallback: also check via wide output to catch edge cases
        if not errored:
            wide_output = self._run_kubectl([
                "kubectl", "get", "pods", "-n", namespace,
                "--no-headers",
                "-o", "wide"
            ])
            for line in wide_output.splitlines():
                parts = line.split()
                if not parts:
                    continue
                pod_name = parts[0]
                for state in ERROR_STATES:
                    if state.lower() in line.lower():
                        errored.append({
                            "pod_name": pod_name,
                            "error_state": state,
                        })
                        break

        return errored

    def _get_logs(self, pod_name: str, namespace: str) -> str:
        # Try previous container logs first (crashed containers)
        logs = self._run_kubectl([
            "kubectl", "logs", pod_name, "-n", namespace, "--previous", "--tail=100"
        ])
        if "(error" in logs or "not found" in logs or not logs.strip():
            # Fall back to current container logs
            logs = self._run_kubectl([
                "kubectl", "logs", pod_name, "-n", namespace, "--tail=100"
            ])
        return logs or "(no logs available)"

    def _get_describe(self, pod_name: str, namespace: str) -> str:
        return self._run_kubectl([
            "kubectl", "describe", "pod", pod_name, "-n", namespace
        ])

    def _get_events(self, pod_name: str, namespace: str) -> str:
        return self._run_kubectl([
            "kubectl", "get", "events", "-n", namespace,
            "--field-selector", f"involvedObject.name={pod_name}",
            "--sort-by=.lastTimestamp"
        ])

    def _analyze_single_pod(self, pod_name: str, namespace: str, error_state: str) -> str:
        logs    = self._get_logs(pod_name, namespace)
        desc    = self._get_describe(pod_name, namespace)
        events  = self._get_events(pod_name, namespace)

        return (
            f"\n{'='*60}\n"
            f"POD: {pod_name} | STATE: {error_state}\n"
            f"{'='*60}\n\n"
            f"--- Logs ---\n{logs}\n\n"
            f"--- Describe ---\n{desc}\n\n"
            f"--- Events ---\n{events}\n"
        )

    def analyze_pods(self) -> Message:
        namespace = self.namespace or "default"

        # Step 1: Find all errored pods
        errored_pods = self._get_errored_pods(namespace)

        if not errored_pods:
            result = f"✅ No pods in error state found in namespace '{namespace}'."
            self.status = result
            return Message(text=result)

        # Step 2: Build summary list
        summary = (
            f"⚠️  Found {len(errored_pods)} pod(s) in error state in namespace '{namespace}':\n\n"
        )
        for p in errored_pods:
            summary += f"  • {p['pod_name']} → {p['error_state']}\n"

        # Step 3: Analyze each errored pod
        details = ""
        for p in errored_pods:
            details += self._analyze_single_pod(p["pod_name"], namespace, p["error_state"])

        result = summary + "\n" + details
        self.status = result
        return Message(text=result)
