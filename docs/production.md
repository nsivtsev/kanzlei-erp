# Production deployment path

Production will use the [official Frappe Docker Compose stack](https://github.com/frappe/frappe_docker/blob/main/docs/01-getting-started/01-choosing-a-deployment-method.md), not the development Compose file. This repository itself is a Frappe app (the Python package and `pyproject.toml` are at its root). The production image will include Frappe, ERPNext, and a reviewed revision of this repository. The [official image build guide](https://github.com/frappe/frappe_docker/blob/main/docs/02-setup/02-build-setup.md) describes `apps.json` and the `compose.yaml` overrides.

Before deployment, decide the hosting provider and region, domain and TLS termination, secret store, backup destination and restore test, monitoring, and update window. Prepare a persistent database and site volumes. Build the image in CI, verify it in a staging environment, then apply migrations and deploy the image to production. Keep production credentials outside Git and container image layers.

The local `development/frappe-bench` directory and MariaDB volume are development data. They are not deployment artifacts. Production receives versioned code and configuration; any real data migration requires a separate, tested backup and restore procedure.

Installing and migrating the Kanzlei app also activates its focused Desk navigation, Mandant terminology, and form customizations. No manual workspace edits are required on production. The application exposes standard Customer, Task, Timesheet, and Sales Invoice workflows through its own sidebar. Set staff document permissions during onboarding; the navigation allowlist is not a permission boundary. The administrator's Einstellungen section contains the company and billing setup required before issuing real invoices.
