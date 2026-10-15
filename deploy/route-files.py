"""Patch the pinned Frappe image template so retained originals cannot bypass S3."""
from pathlib import Path

path = Path("/templates/nginx/frappe.conf.template")
template = path.read_text()
anchor = "\tlocation /assets {"
assert template.count(anchor) == 1, "Pinned Nginx template changed; review file routing"
location = '''	# Stable ERP File URLs are resolved and authorized by the application.
	location ^~ /files/ {
		proxy_http_version 1.1;
		proxy_set_header X-Forwarded-For $remote_addr;
		proxy_set_header X-Forwarded-Proto $proxy_x_forwarded_proto;
		proxy_set_header X-Frappe-Site-Name ${FRAPPE_SITE_NAME_HEADER};
		proxy_set_header Host $host;
		proxy_read_timeout ${PROXY_READ_TIMEOUT};
		proxy_redirect off;
		proxy_pass http://backend-server;
	}

'''
path.write_text(template.replace(anchor, location + anchor))
