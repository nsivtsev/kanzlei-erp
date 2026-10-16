# Support request: inconsistent S3 AccessDenied in a new private NBG1 bucket

Hello Hetzner Support,

We cannot initialize an encrypted Restic backup repository in a newly created
private Object Storage bucket. Please investigate authorization/key propagation
and bucket metadata consistency across the gateways serving this bucket.

- Project: `16338956` (owner ID `p16338956`)
- Bucket: `stb-golke-backups`
- Endpoint: `https://nbg1.your-objectstorage.com`
- Signing: AWS Signature V4, region `nbg1`
- Object Lock: disabled; GetObjectLockConfiguration returns
  `ObjectLockConfigurationNotFoundError`
- Versioning: disabled
- Date observed: 2026-10-16, approximately 09:25–09:55 UTC

Both the original S3 key and a newly generated replacement key have been tested.
Signed ListBuckets and GetBucketAcl confirm that the replacement key and bucket
belong to the same project. The key and secret have valid formatting, without
quotes or surrounding whitespace.

Some signed object operations succeed, while others return HTTP 403 AccessDenied
using the same credentials. For example, an independent curl SigV4 test from our
production server returned PUT 200, GET 403, DELETE 204 for the same newly created
test object, consecutively. Boto3 independently produced LIST/PUT/GET/DELETE
AccessDenied responses mixed with successful calls. Virtual and path addressing
were tested. The documented region `nbg1` is used; optional SDK checksum headers
were also disabled in a probe without resolving the problem.

Restic 0.19.1 reports:

```text
Stat(<config/>) failed: Stat: Access Denied.
Fatal: create key in repository at s3:https://nbg1.your-objectstorage.com/stb-golke-backups/erp failed: Stat: Access Denied.
```

Example failed DeleteObject request (region `nbg1`):

```text
HTTP 403 AccessDenied
tx00000dbd7d7f67feb02be-006ad1f1e3-2b7979a0-nbg1-prod1-ceph5
```

The bucket initially had no policy and only its owning project's FULL_CONTROL
ACL. We also tried an explicit private Allow for the replacement key covering
ListBucket/GetBucketLocation and GetObject/PutObject/DeleteObject under `erp/*`.
There is no public principal and no Deny statement. Restic still fails.

Only tiny, non-sensitive access probes have been written. No ERP backup has
been uploaded and scheduling remains disabled pending reliable access. Please
identify why the owning project's valid S3 key receives these AccessDenied
responses and advise how to restore consistent object access.

Credentials are intentionally omitted; please use the project, bucket and
request ID for investigation.
