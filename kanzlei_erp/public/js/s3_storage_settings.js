frappe.ui.form.on("Drive Disk Settings", {
	refresh(frm) {
		// Connection settings can be saved and checked before activating S3.
		for (const field of ["bucket", "aws_key", "aws_secret", "endpoint_url", "signature_version",
			"kanzlei_s3_region", "kanzlei_s3_addressing_style", "kanzlei_s3_public_endpoint", "kanzlei_s3_prefix"]) {
			frm.set_df_property(field, "depends_on", "");
			frm.set_df_property(field, "hidden", 0);
		}
		frm.add_custom_button(__("Test S3 Connection"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the connection settings before testing. Keep Use S3 disabled until migration is complete."));
				return;
			}
			frappe.call({
				method: "kanzlei_erp.object_storage.check_connection",
				freeze: true,
				callback: ({ message }) => {
					if (message?.ok) frappe.msgprint(__("S3 upload, read and delete verification passed."));
				},
			});
		});
	},
});
