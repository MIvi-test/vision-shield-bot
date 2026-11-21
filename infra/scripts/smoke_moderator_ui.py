"""Build and check the panel container with a disposable backend stub."""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT / "apps/moderator-ui"
BACKEND_STUB = """
const http = require('node:http');
http.createServer((req, res) => {
  let body = '';
  req.on('data', chunk => { body += chunk; });
  req.on('end', () => {
    res.statusCode = req.url.startsWith('/api/unauthorized') ? 401 : 200;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'public, max-age=600');
    res.end(JSON.stringify({
      path: req.url, method: req.method, headers: req.headers, body,
    }));
  });
}).listen(8000, '0.0.0.0');
"""


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=600
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed:\n{result.stderr}")
    if args[0] == "logs":
        return (result.stdout + result.stderr).strip()
    return result.stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(
    port: int,
    path: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def wait_ready(port: int, path: str = "/") -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if request(port, path)[0] == 200:
                return
        except OSError, http.client.HTTPException:
            pass
        time.sleep(0.2)
    raise RuntimeError("Container did not become ready in 30 seconds")


def security_headers(headers: dict[str, str]) -> None:
    require("content-security-policy" in headers, "Missing CSP")
    require(headers.get("x-content-type-options") == "nosniff", "Missing nosniff")
    require(headers.get("referrer-policy") == "same-origin", "Wrong referrer policy")


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    network = f"vision-shield-panel-smoke-{suffix}"
    panel = f"{network}-panel"
    backend = f"{network}-backend"
    image = f"{network}:runtime"
    builder = f"{network}:build"
    try:
        print("Building panel and backend-stub images", flush=True)
        docker("build", "--target", "build", "--tag", builder, str(CONTEXT))
        docker("build", "--tag", image, str(CONTEXT))
        docker("network", "create", network)
        docker(
            "run",
            "--detach",
            "--name",
            panel,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "96m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m,mode=1777",
            "--tmpfs",
            "/data:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
            "--tmpfs",
            "/config:rw,noexec,nosuid,size=1m,uid=10001,gid=10001",
            "--publish",
            "127.0.0.1::8080",
            image,
        )
        port = int(docker("port", panel, "8080/tcp").rsplit(":", 1)[1])
        wait_ready(port)
        require(docker("exec", panel, "id", "-u") == "10001", "Container runs as root")
        docker(
            "exec",
            panel,
            "wget",
            "-q",
            "-O",
            "/dev/null",
            "http://127.0.0.1:8081/health/live",
        )
        status, headers, index = request(port, "/")
        require(status == 200, "Index unavailable")
        require(headers.get("cache-control") == "no-store", "Index is cacheable")
        security_headers(headers)
        status, headers, fallback = request(port, "/submissions/smoke-example")
        require(status == 200 and fallback == index, "SPA fallback failed")
        require(headers.get("cache-control") == "no-store", "SPA fallback is cacheable")
        asset = re.search(r'src="(/assets/[^\"]+\.js)"', index.decode())
        if asset is None:
            raise RuntimeError("Built JavaScript asset not found")
        status, headers, _ = request(port, asset[1])
        require(status == 200, "Built asset unavailable")
        require("immutable" in headers.get("cache-control", ""), "Asset cache missing")
        security_headers(headers)
        status, headers, _ = request(port, "/assets/missing.js")
        require(status == 404, "Missing asset was replaced with SPA HTML")
        require(
            headers.get("cache-control") == "no-store", "Missing asset is cacheable"
        )
        security_headers(headers)
        secret = uuid.uuid4().hex
        status, headers, _ = request(
            port, "/api/offline?marker=smoke", headers={"X-Max-Bot-Api-Secret": secret}
        )
        require(status == 502, "Offline API was replaced with SPA HTML")
        require(headers.get("cache-control") == "no-store", "502 is cacheable")
        print(
            "Static files, hardening, caching and offline Backend: passed", flush=True
        )

        docker(
            "run",
            "--detach",
            "--name",
            backend,
            "--network",
            network,
            "--network-alias",
            "backend",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--entrypoint",
            "node",
            builder,
            "-e",
            BACKEND_STUB,
        )
        wait_ready(port, "/api/ready")
        for path in ("/api", "/api/submissions?cursor=a%2Fb&limit=2", "/webhook"):
            request_headers = {
                "Host": f"panel.example.test:{port}",
                "Origin": f"http://panel.example.test:{port}",
                "Cookie": f"session={secret}",
                "X-CSRF-Token": secret,
                "X-Max-Bot-Api-Secret": secret,
                "Content-Type": "application/json",
            }
            status, headers, response = request(
                port, path, "POST", request_headers, '{"type":"bot_started"}'
            )
            require(status == 200, "Proxy request failed")
            observed = json.loads(response)
            require(observed["path"] == path, "Proxy changed path or query string")
            require(observed["method"] == "POST", "Proxy changed method")
            require(observed["body"] == '{"type":"bot_started"}', "Proxy changed body")
            for key, value in request_headers.items():
                require(
                    observed["headers"][key.lower()] == value, f"Proxy changed {key}"
                )
            require(headers.get("cache-control") == "no-store", "API is cacheable")
            security_headers(headers)
        status, headers, _ = request(port, "/api/unauthorized")
        require(status == 401, "Proxy replaced Backend status")
        require(headers.get("cache-control") == "no-store", "401 is cacheable")
        require(secret not in docker("logs", panel), "Request secret found in logs")

        adapted = json.loads(
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--env",
                "PANEL_SITE_ADDRESS=panel.example.org",
                image,
                "caddy",
                "adapt",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
                "--validate",
            )
        )
        servers = adapted["apps"]["http"]["servers"].values()
        require(
            any(":8443" in server["listen"] for server in servers),
            "Public site is not configured for the HTTPS listener",
        )
        print("API/webhook proxy, headers and HTTPS configuration: passed", flush=True)
        print("Public ACME issuance needs a real domain; it is not exercised here.")
    finally:
        docker("rm", "--force", "--volumes", panel, backend, check=False)
        docker("network", "rm", network, check=False)
        docker("image", "rm", image, builder, check=False)


if __name__ == "__main__":
    main()
