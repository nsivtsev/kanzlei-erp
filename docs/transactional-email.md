# Transactional email with Resend

Target configuration: production `https://erp.stb-golke.de`, sender
`notifications@stb-golke.de`. This runbook describes the intended setup; it is
not a record of a completed production configuration or delivery test.

Use Frappe's existing password-reset and welcome-email flows with a standard
outgoing Email Account. No custom authentication endpoint or Resend API adapter
is required.

## Prepare Resend

1. Add **stb-golke.de** in the [Resend domain dashboard](https://resend.com/domains).
   Choose the Europe region (Ireland) and enable sending. Copy the DNS records
   displayed for this domain into its DNS provider; wait for **Verified**.
   Use the actual record names and values from Resend rather than examples.
   Preserve the domain's existing mailbox MX records. Receiving through Resend
   is not needed for this setup.
2. Disable open and click tracking for this sending domain so recovery links
   are not rewritten for analytics.
3. Create an API key named `kanzlei-erp-production`, with **Sending access**
   restricted to `stb-golke.de`. Enter it directly in the ERP Email Account's
   Password field. Keep it out of Git, chat, command arguments, and CI logs.

The sender address does not need a separate mailbox for Resend to send from a
verified domain. Receiving replies would require a mailbox or a monitored
Reply-To address configured separately.

## Configure ERPNext

As System Manager, open **Einstellungen → Email Account** and create a dedicated
account with these values. Inspect existing outgoing accounts first: selecting
Default Outgoing changes the site-wide fallback sender, including notifications.
Staff correspondence should use its explicitly selected shared-mailbox account.

| Field | Value |
| --- | --- |
| Email Account Name | Kanzlei ERP Notifications |
| Email Address | `notifications@stb-golke.de` |
| Service / Domain | Leave empty; enter SMTP settings manually |
| Method | Basic |
| Use different Email ID | Enabled (`login_id_is_different`) |
| Alternative Email ID | `resend` |
| Password | Resend API key |
| Awaiting password | Disabled |
| Enable Incoming | Disabled |
| Enable Outgoing | Enabled |
| Default Outgoing | Enabled |
| Outgoing Server | `smtp.resend.com` |
| Port | `587` |
| Use TLS | Enabled (STARTTLS) |
| Use SSL | Disabled (implicit TLS is not used on port 587) |
| Disable SMTP server authentication | Disabled |
| Always use this email address as sender address | Enabled |
| Always use this name as sender name | Enabled |
| Track Email Status | Disabled |
| Send unsubscribe message in email | Disabled |
| Add Signature | Disabled |
| Enable Automatic Linking in Documents | Disabled |
| Create Contacts from Incoming Emails | Disabled (`create_contact`) |

Resend is not a built-in Service preset in the checked Frappe v16 source. Do not
select Sendgrid: that preset forces a different SMTP login. Keep any Always BCC
field empty on this account so password recovery messages are not copied.

Set the production site's `host_name` to `https://erp.stb-golke.de` through Bench
or the existing site configuration mechanism. Confirm `mute_emails` is disabled,
outgoing delivery is not suspended, and scheduler/workers are running. These
are production settings; local development should not send real mail by default.
Account settings persist in the site database across application deployments.

## Verify delivery

Use an explicitly designated test recipient and ordinary enabled ERP user owned
by the tester. Frappe deliberately skips self-service reset for `Administrator`.

1. Save the Email Account and resolve any SMTP authentication or connection error.
2. Request a reset through the production login page for the designated user.
3. Check **Email Queue**, **Error Log**, and Resend's email log for that request.
   A generic success message on the login page does not prove a message was sent.
4. Verify actual inbox delivery, the From address, DKIM/SPF results, and that the
   recovery link begins with `https://erp.stb-golke.de/update-password`.
5. Have the tester complete the password change and sign in. Confirm the used
   recovery link cannot be reused. Do not store recovery URLs or tokens in logs
   or the repository.
6. If welcome mail is required, explicitly designate a new test user and test
   that flow as well. Check queued delivery, not only SMTP connectivity.

If port 587 is blocked, Resend also supports STARTTLS on 2587. Diagnose the
connection from the production host before changing settings. A successful SMTP
submission is distinct from delivery to the recipient's inbox.

## References

- [Resend SMTP settings](https://resend.com/docs/send-with-smtp)
- [Resend verified domains](https://resend.com/docs/dashboard/domains/introduction)
- [Resend API keys](https://resend.com/docs/dashboard/api-keys/introduction)
- [ERPNext Email Account](https://docs.frappe.io/erpnext/email-account)
