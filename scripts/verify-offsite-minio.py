#!/usr/bin/env python3
"""Encrypted backup -> download -> damaged disposable ERP -> DB/S3 restore on MinIO."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import manage
from offsite_backup import OffsiteBackup

SITE = "s3-test.localhost"
BENCH = "/workspace/development/frappe-bench"
COMPOSE = [
	"docker",
	"compose",
	"--env-file",
	str(ROOT / ".env"),
	"-f",
	str(ROOT / ".devcontainer/docker-compose.yml"),
]


def command(args):
	result = subprocess.run(args, capture_output=True, text=True, timeout=1200, check=False)
	if result.returncode:
		raise RuntimeError("Local verification step failed: " + args[0] + "; sensitive output withheld")
	return result.stdout


def python(code):
	return command([*COMPOSE, "exec", "-T", "frappe", BENCH + "/env/bin/python", "-c", code])


def site_python(code):
	return python(
		"import os,json; os.chdir("
		+ repr(BENCH + "/sites")
		+ "); import frappe; "
		+ "frappe.init(site="
		+ repr(SITE)
		+ ",sites_path='.'); frappe.connect(); "
		+ "assert frappe.local.site=='s3-test.localhost' and frappe.conf.allow_tests; "
		+ code
	)


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--restic", required=True)
	args = parser.parse_args()
	os.umask(0o077)
	token = uuid.uuid4().hex
	root = ROOT / ".secrets" / ("offsite-minio-" + token)
	(root / "shared").mkdir(parents=True, mode=0o700)
	password = root / "shared/password"
	password.write_text(secrets.token_urlsafe(48) + "\n")
	values = dict(
		line.split("=", 1)
		for line in (ROOT / ".env").read_text().splitlines()
		if "=" in line and not line.startswith("#")
	)
	repository_prefix = "tests/s3-test.localhost/offsite-repo-" + token
	(root / "shared/offsite-backup.env").write_text(
		"AWS_ACCESS_KEY_ID="
		+ values["LOCAL_MINIO_TEST_ACCESS_KEY"]
		+ "\n"
		+ "AWS_SECRET_ACCESS_KEY="
		+ values["LOCAL_MINIO_TEST_SECRET_KEY"]
		+ "\n"
		+ "AWS_DEFAULT_REGION=us-east-1\nRESTIC_REPOSITORY=s3:http://127.0.0.1:9002/stb-golke-test/"
		+ repository_prefix
		+ "\n"
		+ "RESTIC_PASSWORD_FILE="
		+ str(password)
		+ "\n"
	)
	runner = OffsiteBackup(root, restic=args.restic, allow_local=True)
	container_snapshot = BENCH + "/sites/" + SITE + "/private/kanzlei-snapshots/offsite-" + token
	print("Preparing disposable site snapshot", flush=True)
	print(
		site_python(
			"from kanzlei_erp.tests import s3_acceptance; print(json.dumps(s3_acceptance.seed())); print(json.dumps(s3_acceptance.verify_restored()))"
		).strip()
	)
	command(
		[
			*COMPOSE,
			"exec",
			"-T",
			"--workdir",
			BENCH,
			"frappe",
			"bench",
			"--site",
			SITE,
			"set-config",
			"maintenance_mode",
			"1",
		]
	)
	command(
		[
			*COMPOSE,
			"exec",
			"-T",
			"--workdir",
			BENCH,
			"frappe",
			"bench",
			"--site",
			SITE,
			"backup",
			"--with-files",
			"--compress",
			"--ignore-backup-conf",
			"--backup-path-db",
			container_snapshot + "/database.sql.gz",
			"--backup-path-files",
			container_snapshot + "/public.tgz",
			"--backup-path-private-files",
			container_snapshot + "/private.tgz",
			"--backup-path-conf",
			container_snapshot + "/site_config.json",
		]
	)
	site_python(
		"from kanzlei_erp.s3_backup import export_snapshot; export_snapshot("
		+ repr(container_snapshot + "/s3")
		+ ")"
	)
	container = command([*COMPOSE, "ps", "-q", "frappe"]).strip()
	runner.staging.mkdir()
	command(["docker", "cp", container + ":" + container_snapshot + "/.", str(runner.staging)])
	(runner.staging / "production.env").write_text("LOCAL_TEST_FIXTURE=1\n")
	shutil.copytree(
		ROOT / "deploy", runner.staging / "release/deploy", ignore=shutil.ignore_patterns("__pycache__")
	)
	for path in runner.staging.rglob("*"):
		if path.is_file():
			path.chmod(0o600)
	manage.atomic_json(
		runner.staging / "manifest.json",
		{
			"previous": {"site": SITE},
			"s3": True,
			"checksums": {
				str(p.relative_to(runner.staging)): manage.checksum(p)
				for p in runner.staging.rglob("*")
				if p.is_file()
			},
		},
	)
	saved_key = json.loads((runner.staging / "site_config.json").read_text()).get("encryption_key")
	runner.restic("init")
	snapshot = runner.publish()
	restored = Path(runner.fetch(snapshot, root / "backups/recovered"))
	print("Encrypted upload, full remote readback, retention and explicit fetch passed", flush=True)
	print(
		site_python(
			"from kanzlei_erp.tests import s3_acceptance; print(json.dumps(s3_acceptance.damage()))"
		).strip()
	)
	container_restore = "/workspace/" + str(restored.relative_to(ROOT))
	# Keep the captured configuration/encryption key, and supply only local credentials.
	python(
		"import shutil; shutil.copy2("
		+ repr(container_restore + "/site_config.json")
		+ ","
		+ repr(BENCH + "/sites/" + SITE + "/site_config.json")
		+ ")"
	)
	python(
		"import os; os.chdir("
		+ repr(BENCH + "/sites")
		+ "); import frappe; frappe.init(site="
		+ repr(SITE)
		+ ",sites_path='.'); "
		+ "from frappe.commands.site import _restore; _restore(site="
		+ repr(SITE)
		+ ",sql_file_path="
		+ repr(container_restore + "/database.sql.gz")
		+ ",db_root_username='root',db_root_password=os.environ['LOCAL_DB_ROOT_PASSWORD'],force=True,"
		+ "with_public_files="
		+ repr(container_restore + "/public.tgz")
		+ ",with_private_files="
		+ repr(container_restore + "/private.tgz")
		+ ")"
	)
	# A complete, explicit MinIO target ensures no fallback to another endpoint/key.
	site_python(
		"from pathlib import Path; from kanzlei_erp import object_storage as s; from kanzlei_erp.s3_backup import restore_snapshot; "
		+ "v=dict(l.split('=',1) for l in Path('/workspace/.env').read_text().splitlines() if '=' in l and not l.startswith('#')); "
		+ "target={'bucket':'stb-golke-test','aws_key':v['LOCAL_MINIO_TEST_ACCESS_KEY'],'aws_secret':v['LOCAL_MINIO_TEST_SECRET_KEY'],"
		+ "'endpoint_url':'http://minio:9000','signature_version':'s3v4','kanzlei_s3_region':'us-east-1',"
		+ "'kanzlei_s3_addressing_style':'path','kanzlei_s3_public_endpoint':'http://localhost:9002','kanzlei_s3_prefix':s.prefix()}; "
		+ "restore_snapshot("
		+ repr(container_restore + "/s3")
		+ ", target=target); frappe.db.commit(); "
		+ "from kanzlei_erp.tests import s3_acceptance; print(json.dumps(s3_acceptance.verify_restored()))"
	)
	current_key = json.loads(
		(ROOT / "development/frappe-bench/sites" / SITE / "site_config.json").read_text()
	).get("encryption_key")
	assert current_key == saved_key, "Site encryption key was not restored"
	command(
		[
			*COMPOSE,
			"exec",
			"-T",
			"--workdir",
			BENCH,
			"frappe",
			"bench",
			"--site",
			SITE,
			"set-config",
			"maintenance_mode",
			"0",
		]
	)
	print(
		"PASS: encrypted MinIO backup -> explicit fetch -> damaged DB/objects -> complete local restore; encryption key preserved.",
		flush=True,
	)
	print("Private rehearsal files retained at " + str(root))


if __name__ == "__main__":
	main()
