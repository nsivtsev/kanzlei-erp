#!/usr/bin/env python3
"""Exercise real local MinIO using isolated test credentials and temporary objects.

Does not initialize Frappe, change a site, enable S3, or access Hetzner. Secret
credentials and signed URLs are carried through captured stdin/stdout only.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_test_settings(path):
	values = {}
	for line in path.read_text().splitlines():
		key, separator, value = line.partition("=")
		if separator and key.strip() in ("LOCAL_MINIO_TEST_ACCESS_KEY", "LOCAL_MINIO_TEST_SECRET_KEY"):
			if key.strip() in values:
				raise ValueError("Duplicate MinIO test credentials")
			values[key.strip()] = value.strip().strip("\"'")
	if len(values) != 2 or not all(values.values()):
		raise ValueError("Missing MinIO test credentials; run bash scripts/setup-minio.sh --skip-site-config")
	return {
		"access_key": values["LOCAL_MINIO_TEST_ACCESS_KEY"],
		"secret_key": values["LOCAL_MINIO_TEST_SECRET_KEY"],
		"bucket": "stb-golke-test",
		"prefix": "tests/s3-test.localhost",
		"endpoint": "http://minio:9000",
		"public_endpoint": "http://localhost:9002",
	}


REMOTE = r"""
import hashlib, json, sys
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
s = json.load(sys.stdin)
config = Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30)
def client(endpoint, secret=None):
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=s["access_key"],
        aws_secret_access_key=secret or s["secret_key"], region_name="us-east-1", config=config)
c = client(s["endpoint"])
b, k = s["bucket"], s["probe"]
if s["action"] == "cleanup":
    for suffix in ("", ".copy", ".multipart"):
        c.delete_object(Bucket=b, Key=k + suffix)
    for upload in c.list_multipart_uploads(Bucket=b, Prefix=k).get("Uploads", []):
        c.abort_multipart_upload(Bucket=b, Key=upload["Key"], UploadId=upload["UploadId"])
else:
    payload = b"local S3: byte integrity, range, and browser signature verification"
    c.head_bucket(Bucket=b)
    c.put_object(Bucket=b, Key=k, Body=payload)
    assert c.head_object(Bucket=b, Key=k)["ContentLength"] == len(payload)
    assert c.get_object(Bucket=b, Key=k)["Body"].read() == payload
    assert c.get_object(Bucket=b, Key=k, Range="bytes=3-10")["Body"].read() == payload[3:11]
    c.copy_object(Bucket=b, Key=k + ".copy", CopySource={"Bucket": b, "Key": k})
    assert c.get_object(Bucket=b, Key=k + ".copy")["Body"].read() == payload
    multipart = c.create_multipart_upload(Bucket=b, Key=k + ".multipart")
    c.upload_part(Bucket=b, Key=k + ".multipart", UploadId=multipart["UploadId"],
        PartNumber=1, Body=b"x" * (6 * 1024 * 1024))
    c.abort_multipart_upload(Bucket=b, Key=k + ".multipart", UploadId=multipart["UploadId"])
    assert not c.list_multipart_uploads(Bucket=b, Prefix=k).get("Uploads")
    try:
        c.head_bucket(Bucket="stb-golke-local")
        raise AssertionError("Test user can access the application bucket")
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    try:
        c.get_object(Bucket=b, Key="outside-test-prefix/" + k.rsplit("/", 1)[-1])
        raise AssertionError("Test user escaped its namespace")
    except ClientError as error:
        assert error.response["Error"]["Code"] == "AccessDenied"
    try:
        client(s["endpoint"], "incorrect-test-secret").head_bucket(Bucket=b)
        raise AssertionError("Incorrect secret was accepted")
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    url = client(s["public_endpoint"]).generate_presigned_url("get_object",
        Params={"Bucket": b, "Key": k}, ExpiresIn=60)
    print(json.dumps({"url": url, "sha256": hashlib.sha256(payload).hexdigest()}))
"""


def remote(compose, settings, action):
	request = dict(settings, action=action)
	result = subprocess.run(
		[
			*compose,
			"exec",
			"-T",
			"frappe",
			"/workspace/development/frappe-bench/env/bin/python",
			"-c",
			REMOTE,
		],
		input=json.dumps(request),
		text=True,
		capture_output=True,
		timeout=120,
	)
	if result.returncode:
		# Never echo stderr: an HTTP library could include a signed URL or key.
		raise RuntimeError("MinIO " + action + " failed inside the local container (credentials hidden)")
	return json.loads(result.stdout) if result.stdout.strip() else None


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
	args = parser.parse_args(argv)
	settings = read_test_settings(args.env_file)
	settings["probe"] = settings["prefix"] + "/.verification/" + uuid.uuid4().hex
	compose = [
		"docker",
		"compose",
		"--env-file",
		str(args.env_file),
		"-f",
		str(ROOT / ".devcontainer/docker-compose.yml"),
	]
	try:
		result = remote(compose, settings, "verify")
		import hashlib

		opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
		with opener.open(result["url"], timeout=15) as response:
			if hashlib.sha256(response.read()).hexdigest() != result["sha256"]:
				raise RuntimeError("Signed browser download checksum mismatch")
		anonymous = settings["public_endpoint"] + "/" + settings["bucket"] + "/" + settings["probe"]
		try:
			with opener.open(anonymous, timeout=15):
				raise RuntimeError("Anonymous object download unexpectedly succeeded")
		except urllib.error.HTTPError as error:
			if error.code != 403:
				raise RuntimeError("Anonymous download returned an unexpected status") from None
	finally:
		remote(compose, settings, "cleanup")
	print("PASS: real MinIO HEAD/PUT/GET/copy/Range, multipart abort, wrong-secret rejection,")
	print(
		"      namespace/cross-bucket isolation, anonymous denial, signed browser download on localhost:9002."
	)
	print("Temporary test objects removed; ERP site data and settings were unchanged.")


if __name__ == "__main__":
	try:
		main()
	except Exception as error:
		# Avoid printing library exceptions containing signed URLs or credentials.
		print(
			"MinIO verification failed (" + type(error).__name__ + "); credentials and signed URLs hidden.",
			file=sys.stderr,
		)
		raise SystemExit(1)
