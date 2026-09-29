# Local ERPNext development environment

The project uses the official Frappe Docker development stack on an ARM64 Mac. A Frappe bench and ERPNext site run in the development container, backed by MariaDB and Redis containers. The web server is published only on `127.0.0.1:8100`.

The repository stores the development Compose configuration, setup instructions, and later the source of a custom Frappe application. Database files, site files, generated bench dependencies, and credentials remain local and are excluded from Git.

Frappe and ERPNext use the same `version-16` branch. The local site is `development.localhost` with developer mode enabled. Site creation and app installation are repeatable setup steps, while ongoing database data persists in local volumes.

Production will use the official Frappe Docker production Compose stack and a built image containing the same Frappe/ERPNext major version and this project's custom app. The production host, domain, TLS termination, secret storage, and backup destination will be specified before deployment. Local development Compose is not deployed to production.

Validation: confirm the containers are healthy, `bench --site development.localhost list-apps` contains `frappe` and `erpnext`, and the login page responds on the host port.
