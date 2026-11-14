(() => {
	const active = (state) => state && ["Queued", "Running"].includes(state.status);
	const e = (value) => frappe.utils.escape_html(String(value ?? ""));
	const count = (value) => Math.max(0, Number(value) || 0);
	const errors = {
		QUEUE_UNAVAILABLE: "The import could not be queued. Try again later.",
		RECEIVER_BUSY: "The mailbox is still receiving email. Try again shortly.",
		IMPORT_FAILED: "The import failed. Check the Error Log and mailbox settings, then retry.",
		WORKER_INTERRUPTED: "The import was interrupted. Start it again to check all messages and continue safely.",
	};
	const stages = {
		queued: "Waiting for an import worker", waiting: "Waiting for the email receiver",
		scanning: "Counting messages in mailbox folders", importing: "Importing messages",
		linking: "Linking email replies", finished: "Finished",
	};
	const statuses = { Queued: "Import queued", Running: "Import in progress", Completed: "Import completed" };

	class ImportClient {
		constructor(account, transport, options = {}) {
			this.account = account;
			this.transport = transport;
			this.onstate = options.onstate || (() => {});
			this.onerror = options.onerror || (() => {});
			this.schedule = options.schedule || ((fn) => setTimeout(fn, 2000));
			this.cancel = options.cancel || ((timer) => clearTimeout(timer));
			this.visible = false;
			this.revision = 0;
			this.busy = false;
		}
		open() { this.visible = true; return this.refresh(); }
		close() { this.visible = false; this.revision++; this.cancel(this.timer); }
		apply(state, revision) {
			if (!this.visible || revision !== this.revision) return;
			this.onstate(state);
			if (active(state)) this.timer = this.schedule(() => this.refresh());
		}
		async refresh() {
			if (!this.visible || this.busy) return;
			this.cancel(this.timer);
			const revision = ++this.revision;
			try {
				this.apply(await this.transport("get_import_state", { email_account: this.account }), revision);
			} catch (error) {
				if (this.visible && revision === this.revision) this.onerror(error);
			}
		}
		async start(aliases) {
			if (this.busy || !this.visible) return;
			this.busy = true;
			this.cancel(this.timer);
			const revision = ++this.revision;
			try {
				this.apply(await this.transport("start_import", { email_account: this.account, sender_aliases: aliases }), revision);
			} catch (error) {
				if (this.visible && revision === this.revision) this.onerror(error);
			} finally { this.busy = false; }
		}
	}

	function progressHTML(state) {
		if (!state) return `<p>${e(__("No archive import has been started for this account."))}</p>`;
		const total = count(state.total), processed = count(state.processed);
		const scanning = ["queued", "waiting", "scanning"].includes(state.stage);
		const percent = total ? Math.min(100, Math.floor(processed / total * 100)) : (state.status === "Completed" ? 100 : 0);
		const stage = state.stage === "finished" ? "" : e(__(stages[state.stage] || "Finished"));
		const status = `<div role="status" aria-live="polite" aria-atomic="true"><strong>${e(__(statuses[state.status] || state.status))}</strong>
			<p>${stage}${scanning ? "" : `${stage ? " · " : ""}${e(__("{0} of {1} messages checked", [processed, total]))} (${percent}%)`}</p></div>`;
		const value = total ? Math.min(processed, total) : (state.status === "Completed" ? 1 : 0);
		const bar = `<progress style="width:100%;height:16px" max="${Math.max(1, total)}" ${scanning ? "" : `value="${value}"`} aria-label="${e(__("Email import progress"))}"></progress>`;
		const summary = `<p class="mt-3">${e(__("New messages"))}: <strong>${count(state.imported)}</strong> ·
			${e(__("Existing messages"))}: <strong>${count(state.existing)}</strong> · ${e(__("Failed messages"))}: <strong>${count(state.failed)}</strong></p>`;
		const problem = state.error_code ? `<p class="text-danger">${e(__(errors[state.error_code] || errors.IMPORT_FAILED))}</p>` : "";
		const partial = state.status === "Completed with errors" ? `<p class="text-danger">${e(__("Some messages or attachments could not be imported. Check the Error Log and file limits, then run the import again."))}</p>` : "";
		const attachments = count(state.unsaved_attachments) ? `<p>${e(__("Unsaved attachments"))}: <strong>${count(state.unsaved_attachments)}</strong></p>` : "";
		const table = state.folders?.length ? `<div class="table-responsive"><table class="table table-bordered">
			<caption class="sr-only">${e(__("Folder progress"))}</caption><thead><tr>${["Folder", "Checked", "Messages in folder", "New messages", "Existing messages", "Failed messages"].map((label) => `<th scope="col">${e(__(label))}</th>`).join("")}</tr></thead>
			<tbody>${state.folders.map((folder) => `<tr><th scope="row">${e(folder.name)}</th><td>${count(folder.processed)}</td><td>${count(folder.total)}</td><td>${count(folder.imported)}</td><td>${count(folder.existing)}</td><td>${count(folder.failed)}</td></tr>`).join("")}</tbody></table></div>` : "";
		return status + bar + summary + problem + partial + attachments + table;
	}

	function showImport(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the Email Account before reimporting its archive."));
			return;
		}
		const account = frm.doc.name;
		let ready = false, current = null, signature = null;
		const dialog = new frappe.ui.Dialog({
			title: __("Email import progress"), size: "large",
			fields: [
				{ fieldtype: "HTML", fieldname: "introduction", options: `<p><strong>${e(account)}</strong></p><p>${e(__("Recheck all incoming, sent and archived emails. Drafts, spam and trash are excluded. Existing messages are reused and missing attachments are restored."))}</p><p class="text-muted">${e(__("Automatic receiving pauses during the import and resumes afterwards. You can close this window and reopen it to see progress."))}</p>` },
				{ fieldtype: "Small Text", fieldname: "aliases", label: __("Old sender addresses (optional)"), description: __("One address per line or separated by commas. These addresses identify historical sent mail.") },
				{ fieldtype: "HTML", fieldname: "progress", options: `<p role="status">${e(__("Loading import progress…"))}</p>` },
			],
			primary_action_label: __("Start reimport"),
			async primary_action() {
				if (!ready || active(current) || client.busy) return;
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the Email Account before reimporting its archive."));
					return;
				}
				dialog.get_primary_btn().prop("disabled", true);
				const aliases = (dialog.get_value("aliases") || "").split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
				await client.start(aliases);
			},
			secondary_action_label: __("Refresh progress"),
			secondary_action() { client.refresh(); },
		});
		const client = new ImportClient(account, async (method, args) => {
			const response = await frappe.call({ method: `kanzlei_erp.email_archive_jobs.${method}`, args });
			return response.message;
		}, {
			onstate(state) {
				current = state; ready = true;
				const next = JSON.stringify(state);
				if (next !== signature) {
					signature = next;
					dialog.fields_dict.progress.$wrapper.html(progressHTML(state));
				}
				dialog.set_df_property("aliases", "hidden", Boolean(active(state)));
				dialog.get_primary_btn().prop("disabled", Boolean(active(state)));
			},
			onerror() {
				ready = false; signature = null;
				dialog.fields_dict.progress.$wrapper.html(`<p role="alert" class="text-danger">${e(__("Could not load import progress. Check your connection and access, then click Refresh progress before retrying."))}</p>`);
				dialog.get_primary_btn().prop("disabled", true);
			},
		});
		dialog.onhide = () => client.close();
		dialog.show();
		dialog.get_primary_btn().prop("disabled", true);
		client.open();
		return dialog;
	}

	frappe.email_archive = { ImportClient, progressHTML, showImport };
	frappe.ui.form.on("Email Account", {
		refresh(frm) {
			const label = __("Reimport all emails");
			frm.remove_custom_button(label);
			if (frm.is_new() || !frm.doc.use_imap || frm.doc.service === "Frappe Mail" ||
				!frm.perm?.some((permission) => permission.write) ||
				!frappe.user_roles?.some((role) => ["System Manager", "Super Email User"].includes(role))) return;
			frm.add_custom_button(label, () => showImport(frm));
		},
	});
})();
