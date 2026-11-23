#!/usr/bin/env python3
"""Exercise the production AI Compose services using locally verified candidates."""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from manage import AI_IMAGE_ROLES, Commands, Compose


def smoke(erp, runtime, documents):
    with tempfile.TemporaryDirectory(prefix="kanzlei-ci-ai-") as directory:
        root = Path(directory)
        (root / "shared").mkdir()
        config = root / "shared/production.env"
        options = {
            "DOMAIN": "ci-ai.localhost",
            "SITE_NAME": "ci-ai.localhost",
            "ACME_EMAIL": "ci@example.invalid",
            "DB_PASSWORD": "disposable-erp-test-password",
            "ADMIN_PASSWORD": "disposable-admin-test-password",
            "PROJECT_NAME": "kanzlei-ci-ai-" + uuid.uuid4().hex[:12],
            "HTTPS_ENABLED": "0",
            "AI_ENABLED": "1",
            "AI_POSTGRES_PASSWORD": uuid.uuid4().hex,
            "AI_ERP_SERVICE_KEY": uuid.uuid4().hex,
            "AI_INGESTION_ENABLED": "false",
            "AI_SEARCH_INDEX_ENABLED": "false",
            "AI_CHAT_ENABLED": "false",
            "AI_MISTRAL_ENABLED": "false",
        }
        config.write_text("".join(f"{key}={value}\n" for key, value in options.items()))
        config.chmod(0o600)
        images = {"runtime": runtime, "documents": documents}
        compose = Compose(root, ROOT, erp, Commands(), ai_images=images)
        compose.call("config", "--quiet")
        try:
            compose.ai_migrate()
            compose.start_ai()
            service_images = {service: images[role] for service, role in AI_IMAGE_ROLES.items()}
            compose.verify_running_images(service_images)
            compose.call("exec", "-T", "ai-worker", "celery", "-A", "kanzlei_ai.worker:app",
                         "inspect", "--destination=worker@ai-worker", "--timeout=20", "ping", timeout=30)
            compose.verify_running_images(service_images)
        finally:
            compose.call("down", "--volumes", "--remove-orphans", timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for role in ("erp", "runtime", "documents"):
        parser.add_argument(role)
    args = parser.parse_args()
    started = time.monotonic()
    smoke(args.erp, args.runtime, args.documents)
    metrics = Path(os.environ["CI_METRICS_DIR"])
    (metrics / "ai-smoke.json").write_text(json.dumps({
        "name": "ai-smoke", "exit_code": 0, "seconds": round(time.monotonic() - started, 3),
    }) + "\n")
    print("AI smoke passed: migration, readiness, worker ping and running image identity")


if __name__ == "__main__":
    main()
