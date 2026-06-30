"""Tests for payload.classifier.classify_block — Stage 0 content detector."""

from __future__ import annotations

from corp_llm_gateway.payload.classifier import classify_block

# ---------------------------------------------------------------------------
# Positive cases — should be BLOCKED
# ---------------------------------------------------------------------------


def test_env_fenced_block_detected() -> None:
    text = "Here is my config:\n```env\nDATABASE_URL=postgres://user:pass@localhost/db\nSECRET_KEY=abc123\n```"
    assert classify_block(text) == "config:env"


def test_env_dotenv_fenced_tag_detected() -> None:
    text = "```dotenv\nAPI_KEY=sk-abcdef\nREDIS_URL=redis://localhost\nDEBUG=true\n```"
    assert classify_block(text) == "config:env"


def test_env_plain_high_density() -> None:
    """Three or more ALL_CAPS=value lines forming majority of non-blank content."""
    text = (
        "DATABASE_URL=postgres://user:pass@localhost:5432/prod\n"
        "SECRET_KEY=supersecretvalue123\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://localhost:6379/0\n"
        "ALLOWED_HOSTS=corp.internal\n"
    )
    assert classify_block(text) == "config:env"


def test_env_with_comments_still_detected() -> None:
    text = (
        "# Database settings\n"
        "DATABASE_URL=postgres://admin:hunter2@db.corp.lan/prod\n"
        "# Cache\n"
        "REDIS_URL=redis://cache.corp.lan:6379\n"
        "SECRET_KEY=sk-super-secret-key-abc123\n"
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
    )
    assert classify_block(text) == "config:env"


def test_kubeconfig_detected() -> None:
    text = (
        "apiVersion: v1\n"
        "clusters:\n"
        "- cluster:\n"
        "    server: https://k8s.corp.lan:6443\n"
        "  name: corp-prod\n"
        "contexts:\n"
        "- context:\n"
        "    cluster: corp-prod\n"
        "    user: admin\n"
        "  name: corp-prod\n"
        "current-context: corp-prod\n"
        "kind: Config\n"
        "users:\n"
        "- name: admin\n"
        "  user:\n"
        "    token: abc.def.ghi\n"
    )
    assert classify_block(text) == "config:kube"


def test_kubernetes_manifest_deployment_detected() -> None:
    text = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: my-app\n"
        "  namespace: production\n"
        "spec:\n"
        "  replicas: 3\n"
    )
    assert classify_block(text) == "config:kube"


def test_kubernetes_manifest_via_clusters_contexts() -> None:
    """kubeconfig detected via structural sections alone (no kind: Config required)."""
    text = (
        "clusters:\n"
        "- cluster:\n"
        "    certificate-authority-data: LS0tLS1...\n"
        "    server: https://k8s.example.com\n"
        "  name: my-cluster\n"
        "contexts:\n"
        "- context:\n"
        "    cluster: my-cluster\n"
        "    user: dev\n"
        "  name: dev-context\n"
    )
    assert classify_block(text) == "config:kube"


def test_nginx_config_detected() -> None:
    text = (
        "server {\n"
        "    listen 443 ssl;\n"
        "    server_name corp.internal;\n"
        "    location /api {\n"
        "        proxy_pass http://backend:8080;\n"
        "    }\n"
        "    location /static {\n"
        "        root /var/www;\n"
        "    }\n"
        "}\n"
    )
    assert classify_block(text) == "config:nginx"


def test_nginx_fenced_tag_detected() -> None:
    text = "My nginx config:\n```nginx\nserver {\n    listen 80;\n}\n```"
    assert classify_block(text) == "config:nginx"


def test_ini_config_detected() -> None:
    text = (
        "[database]\n"
        "host = db.corp.internal\n"
        "port = 5432\n"
        "name = prod_db\n"
        "user = svc_account\n"
        "\n"
        "[cache]\n"
        "host = redis.corp.internal\n"
        "port = 6379\n"
        "ttl = 3600\n"
    )
    assert classify_block(text) == "config:ini"


def test_toml_fenced_tag_detected() -> None:
    text = "Config file:\n```toml\n[server]\nhost = 'localhost'\nport = 8080\n\n[database]\nurl = 'postgres://localhost/db'\n```"
    assert classify_block(text) == "config:ini"


def test_log_dump_structured_app_log() -> None:
    """Structured app log: many timestamp + log-level lines."""
    lines = [
        "2024-01-15 10:00:01 INFO  Starting application server",
        "2024-01-15 10:00:02 INFO  Loaded configuration from /etc/app.conf",
        "2024-01-15 10:00:03 DEBUG Database pool initialized connections=10",
        "2024-01-15 10:00:15 INFO  Listening on 0.0.0.0:8080",
        "2024-01-15 10:01:22 ERROR Failed to connect to cache host=redis.corp port=6379",
        "2024-01-15 10:01:23 WARN  Retrying connection attempt=1",
        "2024-01-15 10:01:25 WARN  Retrying connection attempt=2",
        "2024-01-15 10:01:30 ERROR Max retries exceeded giving up",
    ]
    assert classify_block("\n".join(lines)) == "log:dump"


def test_log_dump_access_log() -> None:
    """HTTP access log: high timestamp density (CLF format)."""
    lines = [
        '192.168.1.10 - alice [15/Jan/2024:10:00:01 +0000] "GET /api/v1/users HTTP/1.1" 200 1234',
        '192.168.1.11 - bob [15/Jan/2024:10:00:02 +0000] "POST /api/v1/data HTTP/1.1" 201 567',
        '192.168.1.10 - alice [15/Jan/2024:10:00:05 +0000] "GET /api/v1/config HTTP/1.1" 403 89',
        '10.0.0.5 - - [15/Jan/2024:10:00:08 +0000] "GET /health HTTP/1.1" 200 15',
        '192.168.1.12 - carol [15/Jan/2024:10:00:10 +0000] "DELETE /api/v1/token HTTP/1.1" 204 0',
        '192.168.1.10 - alice [15/Jan/2024:10:00:12 +0000] "GET /api/v1/secret HTTP/1.1" 200 2048',
        '10.0.0.5 - - [15/Jan/2024:10:00:15 +0000] "GET /health HTTP/1.1" 200 15',
        '192.168.1.13 - dave [15/Jan/2024:10:00:18 +0000] "PUT /api/v1/profile HTTP/1.1" 200 340',
        '192.168.1.11 - bob [15/Jan/2024:10:00:20 +0000] "GET /api/v1/admin HTTP/1.1" 403 89',
        '192.168.1.10 - alice [15/Jan/2024:10:00:22 +0000] "GET /api/v1/export HTTP/1.1" 200 9876',
    ]
    assert classify_block("\n".join(lines)) == "log:dump"


def test_log_dump_python_stack_trace() -> None:
    """Python exception traceback: many File/line frames."""
    text = (
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 42, in handle_request\n'
        "    result = process(data)\n"
        '  File "/app/processor.py", line 18, in process\n'
        "    return transformer.run(data)\n"
        '  File "/app/transform.py", line 77, in run\n'
        "    validated = schema.validate(input)\n"
        '  File "/app/schema.py", line 31, in validate\n'
        "    raise ValidationError(msg)\n"
        '  File "/app/schema.py", line 15, in __init__\n'
        "    super().__init__(msg)\n"
        "ValidationError: invalid payload structure\n"
    )
    assert classify_block(text) == "log:dump"


def test_log_dump_fenced_log_tag() -> None:
    text = "Output:\n```log\n2024-01-01 INFO start\n2024-01-01 ERROR crash\n```"
    assert classify_block(text) == "log:dump"


# ---------------------------------------------------------------------------
# Negative cases — should NOT be blocked (false-positive prevention)
# ---------------------------------------------------------------------------


def test_clean_russian_prose_passes() -> None:
    text = (
        "Privyet! Mne nuzhna pomoshch s napisaniem koda na Python.\n"
        "Ya khochu realizovat funktsiu dlya chteniya konfiguracii iz fajla.\n"
        "Kak luchshe vsego eto sdelat? Ispolzuem li my TOML ili INI format?\n"
        "Podscazhite, pozhaluysta, kakoj podkhod predpochtitelnee v nashej komande."
    )
    assert classify_block(text) is None


def test_normal_python_code_passes() -> None:
    text = (
        "```python\n"
        "def load_config(path: str) -> dict:\n"
        '    """Load config from a TOML file."""\n'
        "    with open(path, 'rb') as f:\n"
        "        return tomllib.load(f)\n"
        "\n"
        "config = load_config('settings.toml')\n"
        "host = config.get('host', 'localhost')\n"
        "```\n"
    )
    assert classify_block(text) is None


def test_short_config_like_message_passes() -> None:
    """A brief mention of KEY=VALUE in prose — not enough to block."""
    text = (
        "I set DATABASE_URL=localhost and DEBUG=True in my local env,\n"
        "but I'm not sure how to pass them to Docker."
    )
    assert classify_block(text) is None


def test_two_env_lines_below_threshold_passes() -> None:
    """Two ALL_CAPS assignments: below the ≥3 threshold."""
    text = "MAX_SIZE=1024\nMIN_SIZE=1\n"
    assert classify_block(text) is None


def test_python_constants_minority_in_file_passes() -> None:
    """ALL_CAPS constants are a minority among normal Python code lines."""
    text = (
        "# constants.py\n"
        "MAX_RETRIES = 3\n"
        "TIMEOUT_SEC = 30\n"
        "BASE_URL = 'https://api.example.com'\n"
        "\n"
        "def get_client(url: str = BASE_URL) -> httpx.Client:\n"
        "    return httpx.Client(base_url=url, timeout=TIMEOUT_SEC)\n"
        "\n"
        "def retry(fn, max_retries: int = MAX_RETRIES):\n"
        "    for i in range(max_retries):\n"
        "        try:\n"
        "            return fn()\n"
        "        except Exception:\n"
        "            pass\n"
    )
    assert classify_block(text) is None


def test_yaml_without_kube_keywords_passes() -> None:
    """Generic YAML (no apiVersion/kind/clusters/contexts) is not blocked."""
    text = (
        "```yaml\n"
        "name: my-service\n"
        "version: '1.0'\n"
        "dependencies:\n"
        "  - requests>=2.28\n"
        "  - pydantic>=2.0\n"
        "```\n"
    )
    assert classify_block(text) is None


def test_server_in_code_without_location_passes() -> None:
    """Mention of 'server {' in a Go struct or comment is not nginx."""
    text = (
        "Here is a simple HTTP server snippet:\n"
        "```go\n"
        "type server struct {\n"
        "    addr string\n"
        "}\n"
        "```\n"
    )
    assert classify_block(text) is None


def test_single_ini_section_passes() -> None:
    """Only one [section] header — below the ≥2 threshold for ini detection."""
    text = "[settings]\nhost = localhost\nport = 8080\n"
    assert classify_block(text) is None


def test_few_log_lines_passes() -> None:
    """Three log lines with timestamps — below the ≥5 timestamp threshold."""
    text = (
        "2024-01-15 10:00:01 INFO  app started\n"
        "2024-01-15 10:00:02 ERROR connection failed\n"
        "2024-01-15 10:00:03 INFO  retrying\n"
    )
    assert classify_block(text) is None


def test_empty_text_passes() -> None:
    assert classify_block("") is None


def test_single_line_passes() -> None:
    assert classify_block("DATABASE_URL=postgres://localhost/db") is None


def test_prose_mentioning_kubernetes_concepts_passes() -> None:
    """Prose that MENTIONS Kubernetes terms without actual config content."""
    text = (
        "In Kubernetes, the apiVersion field specifies the API group and version.\n"
        "The kind field identifies the resource type, such as Pod or Service.\n"
        "These are documented at kubernetes.io/docs.\n"
    )
    # "kind" here is not followed by a PascalCase resource AND "apiVersion:" must
    # have an actual value — prose like "apiVersion field" doesn't match.
    assert classify_block(text) is None


def test_block_reason_contains_no_raw_content() -> None:
    """The returned reason code is always a short token, never user content."""
    payloads = [
        "SECRET_KEY=hunter2\nDATABASE_URL=postgres://admin:pass@db/prod\nDEBUG=0\nFEATURE_X=on\n",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db-creds\n",
    ]
    sensitive_tokens = ["hunter2", "admin:pass", "db-creds"]
    for payload in payloads:
        reason = classify_block(payload)
        assert reason is not None
        for token in sensitive_tokens:
            assert token not in reason
