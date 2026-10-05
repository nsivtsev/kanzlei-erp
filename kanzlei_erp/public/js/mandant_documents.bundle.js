// Permission-checked document browser for the Customer (Mandant) form.
(function () {
	const api = "kanzlei_erp.mandant_documents";
	const defaultFilters = () => ({
		query: "",
		source: "all",
		direction: "all",
		file_group: "all",
		date_from: null,
		date_to: null,
		package: null,
		sort: "newest",
	});

	const strings = {
		all: __("All"),
		all_sources: __("All sources"),
		mandant: __("Client"),
		email: __("Email"),
		fibu: __("Accounting"),
		task: __("Task"),
		question: __("Question"),
		received: __("Received"),
		sent: __("Sent"),
		search: __("Search files, mail subjects, or addresses"),
		format: __("File format"),
		date_from: __("Date from"),
		date_to: __("Date to"),
		period: __("Accounting period"),
		sort: __("Sort order"),
		newest: __("Newest first"),
		oldest: __("Oldest first"),
		refresh: __("Refresh"),
		reset: __("Reset filters"),
		incoming: __("Incoming email"),
		select_loaded: __("Select loaded files"),
		selected: __("Selected: {0}"),
		size_total: __("Known total size: {0}"),
		download_zip: __("Download ZIP"),
		file: __("File"),
		date: __("Date"),
		source: __("Source"),
		context: __("Context"),
		size: __("Size"),
		actions: __("Actions"),
		open: __("Open"),
		download: __("Download"),
		open_source: __("Open source"),
		more_contexts: __("{0} more"),
		showing: __("Showing {0} loaded files"),
		load_more: __("Load more"),
		loading: __("Loading documents…"),
		loading_more: __("Loading more documents…"),
		preparing_zip: __("Preparing ZIP download…"),
		empty: __("No documents are available for this client."),
		no_match: __("No documents match these filters."),
		error: __("Could not load documents. Please try again."),
		unsaved: __("Save this client before viewing documents."),
		date_help: __("Email date for email attachments; otherwise the file upload date. This is not an accounting document date."),
		choose_file: __("Choose at least one local file."),
		zip_limit: __("ZIP supports up to 100 local files and 100 MiB."),
		pdf: __("PDF"),
		image: __("Images"),
		spreadsheet: __("Spreadsheets"),
		document: __("Text documents"),
		xml: __("XML"),
		archive: __("Archives"),
		other: __("Other"),
		no_extension: __("No extension"),
	};

	function node(tag, text, className) {
		const element = document.createElement(tag);
		if (text !== undefined && text !== null) element.textContent = text;
		if (className) element.className = className;
		return element;
	}

	function button(label, className, handler) {
		const element = node("button", label, className || "btn btn-default btn-sm");
		element.type = "button";
		element.addEventListener("click", handler);
		return element;
	}

	function addOption(select, value, label) {
		const option = node("option", label);
		option.value = value;
		select.appendChild(option);
	}

	function rpc(method, args) {
		return Promise.resolve(frappe.call({ method: `${api}.${method}`, args })).then((result) => result.message);
	}

	function formatSize(value) {
		if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
		const bytes = Number(value);
		if (bytes === 0) return "0 B";
		const units = ["B", "KB", "MB", "GB"];
		const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
		return `${(bytes / 1024 ** exponent).toFixed(exponent ? 1 : 0)} ${units[exponent]}`;
	}

	function formatDate(value) {
		if (!value) return "—";
		return frappe.datetime.str_to_user(value);
	}

	function sourceLabel(source) {
		return strings[source] || source;
	}

	function directionLabel(direction) {
		if (direction === "received") return strings.received;
		if (direction === "sent") return strings.sent;
		return "";
	}

	class MandantDocuments {
	constructor(wrapper) {
			this.wrapper = wrapper;
			this.customer = null;
			this.isNew = true;
			this.filters = defaultFilters();
			this.items = [];
			this.cursor = null;
			this.hasMore = false;
			this.selected = new Map();
			this.expanded = new Set();
			this.packages = [];
			this.requestId = 0;
			this.loading = false;
			this.loadingMore = false;
			this.error = false;
			this.zipPending = false;
			this.searchTimer = null;
			this.injectStyles();
			this.makeShell();
		}

		injectStyles() {
			if (document.getElementById("kanzlei-mandant-documents-style")) return;
			const style = node("style");
			style.id = "kanzlei-mandant-documents-style";
			style.textContent = `
				.mandant-documents { padding: 8px 0; }
				.md-toolbar, .md-selection-bar, .md-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
				.md-search-wrap { flex: 1 1 260px; min-width: 180px; }
				.md-filter { width: auto; min-width: 130px; max-width: 220px; }
				.md-direction { min-width: 110px; }
				.md-package { min-width: 240px; flex: 1 1 240px; }
				.md-date { display: flex; align-items: center; gap: 6px; margin: 0; font-weight: normal; }
				.md-date .form-control { width: 145px; }
				.md-incoming { white-space: nowrap; }
				.md-selection-bar { justify-content: flex-start; }
				.md-selection-bar > span { margin: 0 auto 0 4px; }
				.md-table-wrap { overflow-x: auto; }
				.md-table { min-width: 920px; margin-bottom: 8px; }
				.md-table th { white-space: nowrap; }
				.md-check-cell { width: 34px; text-align: center; }
				.md-file-cell { max-width: 280px; }
				.md-file-name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
				.md-extension { display: inline-block; margin-top: 3px; }
				.md-context-cell { min-width: 230px; }
				.md-context-line { margin-bottom: 3px; }
				.md-context-line small { display: block; overflow-wrap: anywhere; }
				.md-more { padding: 0 2px; }
				.md-actions-cell { white-space: nowrap; }
				.md-actions-cell .btn + .btn { margin-left: 4px; }
				.md-notice { min-height: 20px; margin: 8px 0; }
				.md-notice .btn { margin-left: 8px; }
				.md-footer { justify-content: space-between; }
				@media (max-width: 767px) {
					.md-toolbar-secondary { align-items: stretch; }
					.md-date { flex: 1 1 220px; justify-content: space-between; }
					.md-date .form-control { flex: 0 1 145px; }
					.md-filter { flex: 1 1 140px; max-width: none; }
					.md-package { flex-basis: 100%; }
				}
			`;
			document.head.appendChild(style);
		}

		makeShell() {
			this.wrapper.replaceChildren();
			const root = node("div", undefined, "mandant-documents");
			const toolbar = node("div", undefined, "md-toolbar");
			const searchWrap = node("div", undefined, "md-search-wrap");
			this.search = node("input", undefined, "form-control input-sm");
			this.search.type = "search";
			this.search.maxLength = 200;
			this.search.placeholder = strings.search;
			this.search.setAttribute("aria-label", strings.search);
			this.search.addEventListener("input", () => {
				this.filters.query = this.search.value.slice(0, 200);
				this.requestId += 1;
				this.items = [];
				this.cursor = null;
				this.hasMore = false;
				this.selected.clear();
				this.loading = true;
				this.loadingMore = false;
				this.renderRows();
				clearTimeout(this.searchTimer);
				this.searchTimer = setTimeout(() => this.reload(), 300);
			});
			searchWrap.appendChild(this.search);
			toolbar.appendChild(searchWrap);

			this.sourceSelect = this.makeSelect("md-filter", strings.source);
			for (const [value, label] of [
				["all", strings.all_sources],
				["mandant", strings.mandant],
				["email", strings.email],
				["fibu", strings.fibu],
				["task", strings.task],
				["question", strings.question],
			]) addOption(this.sourceSelect, value, label);
			this.sourceSelect.addEventListener("change", () => {
				this.filters.source = this.sourceSelect.value;
				if (this.filters.source !== "email") this.filters.direction = "all";
				this.renderDirection();
				this.reload();
			});
			toolbar.appendChild(this.sourceSelect);

			this.directionSelect = this.makeSelect("md-filter md-direction", strings.email);
			addOption(this.directionSelect, "all", strings.all);
			addOption(this.directionSelect, "received", strings.received);
			addOption(this.directionSelect, "sent", strings.sent);
			this.directionSelect.addEventListener("change", () => {
				this.filters.direction = this.directionSelect.value;
				this.reload();
			});
			toolbar.appendChild(this.directionSelect);

			this.groupSelect = this.makeSelect("md-filter", strings.format);
			addOption(this.groupSelect, "all", strings.all);
			for (const value of ["pdf", "image", "spreadsheet", "document", "xml", "archive", "other"])
				addOption(this.groupSelect, value, strings[value]);
			this.groupSelect.addEventListener("change", () => {
				this.filters.file_group = this.groupSelect.value;
				this.reload();
			});
			toolbar.appendChild(this.groupSelect);
			root.appendChild(toolbar);

			const second = node("div", undefined, "md-toolbar md-toolbar-secondary");
			this.dateFrom = this.makeDate(strings.date_from);
			this.dateTo = this.makeDate(strings.date_to);
			for (const control of [this.dateFrom, this.dateTo]) {
				control.input.addEventListener("change", () => {
					this.filters[control === this.dateFrom ? "date_from" : "date_to"] = control.input.value || null;
					this.reload();
				});
				second.appendChild(control.wrap);
			}
			this.packageSelect = this.makeSelect("md-filter md-package", strings.period);
			addOption(this.packageSelect, "", strings.all);
			this.packageSelect.addEventListener("change", () => {
				this.filters.package = this.packageSelect.value || null;
				this.reload();
			});
			second.appendChild(this.packageSelect);

			this.sortSelect = this.makeSelect("md-filter md-sort", strings.sort);
			addOption(this.sortSelect, "newest", strings.newest);
			addOption(this.sortSelect, "oldest", strings.oldest);
			this.sortSelect.addEventListener("change", () => {
				this.filters.sort = this.sortSelect.value;
				this.reload();
			});
			second.appendChild(this.sortSelect);
			second.appendChild(button(strings.incoming, "btn btn-default btn-sm md-incoming", () => {
				this.filters.source = "email";
				this.filters.direction = "received";
				this.syncControls();
				this.reload();
			}));
			second.appendChild(button(strings.reset, "btn btn-default btn-sm", () => this.reset()));
			second.appendChild(button(strings.refresh, "btn btn-default btn-sm", () => this.refresh()));
			root.appendChild(second);

			this.notice = node("div", undefined, "md-notice text-muted");
			this.notice.setAttribute("role", "status");
			root.appendChild(this.notice);

			const actions = node("div", undefined, "md-selection-bar");
			this.selectLoaded = button(strings.select_loaded, "btn btn-default btn-sm", () => this.selectVisible());
			this.selectedText = node("span", "", "text-muted");
			this.zipButton = button(strings.download_zip, "btn btn-primary btn-sm", () => this.downloadZip());
			actions.append(this.selectLoaded, this.selectedText, this.zipButton);
			root.appendChild(actions);

			this.tableWrap = node("div", undefined, "md-table-wrap");
			this.table = node("table", undefined, "table table-bordered table-hover md-table");
			const thead = node("thead");
			const header = node("tr");
			for (const label of ["", strings.file, strings.date, strings.source, strings.context, strings.size, strings.actions])
				header.appendChild(node("th", label));
			thead.appendChild(header);
			this.table.appendChild(thead);
			this.tbody = node("tbody");
			this.table.appendChild(this.tbody);
			this.tableWrap.appendChild(this.table);
			root.appendChild(this.tableWrap);

			const footer = node("div", undefined, "md-footer");
			this.count = node("span", "", "text-muted");
			this.moreButton = button(strings.load_more, "btn btn-default btn-sm", () => this.loadMore());
			footer.append(this.count, this.moreButton);
			root.appendChild(footer);
			this.wrapper.appendChild(root);
			this.renderDirection();
			this.updateSelection();
		}

		makeSelect(className, label) {
			const select = node("select", undefined, `form-control input-sm ${className}`);
			select.setAttribute("aria-label", label);
			return select;
		}

		makeDate(label) {
			const wrap = node("label", undefined, "md-date");
			wrap.appendChild(node("span", label));
			const input = node("input", undefined, "form-control input-sm");
			input.type = "date";
			input.setAttribute("aria-label", label);
			wrap.appendChild(input);
			return { wrap, input };
		}

		setCustomer(customer, isNew) {
			if (this.customer !== customer) {
				this.requestId += 1;
				this.customer = customer;
				this.items = [];
				this.cursor = null;
				this.hasMore = false;
				this.selected.clear();
				this.expanded.clear();
				this.packages = [];
				this.error = false;
				this.renderRows();
			}
			this.isNew = isNew;
			if (isNew) {
				this.items = [];
				this.cursor = null;
				this.selected.clear();
				this.notice.textContent = strings.unsaved;
				this.renderRows();
			}
		}

		activate(forceRefresh = true) {
			if (this.isNew) return;
			if (forceRefresh || !this.items.length) this.refresh();
		}

		refresh() {
			if (this.isNew) return;
			this.loadPackages();
			this.reload();
		}

		loadPackages() {
			const customer = this.customer;
			rpc("get_package_options", { customer }).then((packages) => {
				if (this.customer !== customer) return;
				this.packages = packages || [];
				const selected = this.filters.package || "";
				this.packageSelect.replaceChildren();
				addOption(this.packageSelect, "", strings.all);
				for (const item of this.packages) addOption(this.packageSelect, item.name, item.title || item.name);
				this.packageSelect.value = this.packages.some((item) => item.name === selected) ? selected : "";
			}).catch(() => {
				if (this.customer === customer) this.packages = [];
			});
		}

		renderDirection() {
			this.directionSelect.hidden = this.filters.source !== "email";
			this.directionSelect.value = this.filters.direction;
		}

		syncControls() {
			this.search.value = this.filters.query;
			this.sourceSelect.value = this.filters.source;
			this.groupSelect.value = this.filters.file_group;
			this.dateFrom.input.value = this.filters.date_from || "";
			this.dateTo.input.value = this.filters.date_to || "";
			this.sortSelect.value = this.filters.sort;
			this.packageSelect.value = this.filters.package || "";
			this.renderDirection();
		}

		reset() {
			this.filters = defaultFilters();
			this.syncControls();
			this.reload();
		}

		reload() {
			if (this.isNew) return;
			clearTimeout(this.searchTimer);
			this.requestId += 1;
			this.items = [];
			this.cursor = null;
			this.hasMore = false;
			this.selected.clear();
			this.expanded.clear();
			this.error = false;
			this.loading = true;
			this.loadingMore = false;
			this.notice.textContent = strings.loading;
			this.renderRows();
			this.fetchPage(this.requestId, false);
		}

		loadMore() {
			if (this.loading || this.loadingMore || !this.hasMore || !this.cursor) return;
			this.loadingMore = true;
			this.notice.textContent = strings.loading_more;
			this.fetchPage(this.requestId, true);
		}

		fetchPage(requestId, append) {
			const customer = this.customer;
			const filters = { ...this.filters };
			rpc("get_documents", {
				customer,
				filters: JSON.stringify(filters),
				cursor: append ? this.cursor : null,
				page_length: 50,
			}).then((result) => {
				if (requestId !== this.requestId || customer !== this.customer) return;
				this.items = append ? this.items.concat(result.items || []) : result.items || [];
				this.cursor = result.next_cursor || null;
				this.hasMore = Boolean(result.has_more);
				this.error = false;
			}).catch(() => {
				if (requestId !== this.requestId || customer !== this.customer) return;
				this.error = true;
			}).finally(() => {
				if (requestId !== this.requestId || customer !== this.customer) return;
				this.loading = false;
				this.loadingMore = false;
				this.renderRows();
			});
		}

		selectVisible() {
			for (const item of this.items) {
				if (item.can_select_for_zip) this.selected.set(item.file_id, item);
			}
			this.renderRows();
		}

		updateSelection() {
			const selected = Array.from(this.selected.values());
			const knownSize = selected.reduce((sum, item) => sum + (Number(item.file_size) || 0), 0);
			this.selectedText.textContent = `${__("Selected: {0}", [selected.length])} · ${__("Known total size: {0}", [formatSize(knownSize)])}`;
			this.zipButton.disabled = selected.length === 0 || this.zipPending;
			this.selectLoaded.disabled = this.items.every((item) => !item.can_select_for_zip);
		}

		renderRows() {
			this.tbody.replaceChildren();
			if (this.isNew) {
				this.notice.textContent = strings.unsaved;
			} else if (this.loading) {
				this.notice.textContent = strings.loading;
			} else if (this.error) {
				this.notice.replaceChildren();
				this.notice.append(node("span", strings.error), button(strings.refresh, "btn btn-default btn-xs", () => this.refresh()));
			} else if (!this.items.length) {
				this.notice.textContent = this.filters.query || this.filters.source !== "all" || this.filters.package || this.filters.date_from || this.filters.date_to || this.filters.file_group !== "all"
					? strings.no_match
					: strings.empty;
			} else {
				this.notice.textContent = "";
				for (const item of this.items) this.tbody.appendChild(this.renderRow(item));
			}
			this.count.textContent = __("Showing {0} loaded files", [this.items.length]);
			this.moreButton.hidden = !this.hasMore;
			this.moreButton.disabled = this.loadingMore;
			this.updateSelection();
		}

		renderRow(item) {
			const row = node("tr");
			const choose = node("td", undefined, "md-check-cell");
			if (item.can_select_for_zip) {
				const checkbox = node("input");
				checkbox.type = "checkbox";
				checkbox.checked = this.selected.has(item.file_id);
				checkbox.setAttribute("aria-label", __("Select {0}", [item.file_name]));
				checkbox.addEventListener("change", () => {
					if (checkbox.checked) this.selected.set(item.file_id, item);
					else this.selected.delete(item.file_id);
					this.updateSelection();
				});
				choose.appendChild(checkbox);
			}
			row.appendChild(choose);

			const fileCell = node("td", undefined, "md-file-cell");
			const fileName = node("div", item.file_name || strings.no_extension, "md-file-name");
			fileName.title = item.file_name || strings.no_extension;
			fileCell.appendChild(fileName);
			fileCell.appendChild(node("small", (item.extension || strings.no_extension).toUpperCase(), "text-muted md-extension"));
			row.appendChild(fileCell);

			const dateCell = node("td", formatDate(item.document_date));
			dateCell.title = strings.date_help;
			row.appendChild(dateCell);

			const sources = Array.from(new Set(item.contexts.map((context) => context.source)));
			row.appendChild(node("td", sources.map(sourceLabel).join(", ")));
			row.appendChild(this.renderContexts(item));
			row.appendChild(node("td", formatSize(item.file_size)));

			const actions = node("td", undefined, "md-actions-cell");
			if (item.storage_kind === "remote" && item.external_url) {
				const link = node("a", strings.open, "btn btn-default btn-xs");
				link.href = item.external_url;
				link.target = "_blank";
				link.rel = "noopener noreferrer";
				actions.appendChild(link);
			} else {
				actions.appendChild(button(strings.open, "btn btn-default btn-xs", () => this.openFile(item, "inline")));
				actions.appendChild(button(strings.download, "btn btn-default btn-xs", () => this.openFile(item, "attachment")));
			}
			row.appendChild(actions);
			return row;
		}

		renderContexts(item) {
			const cell = node("td", undefined, "md-context-cell");
			const all = item.contexts || [];
			const isExpanded = this.expanded.has(item.file_id);
			const visible = isExpanded ? all : all.slice(0, 2);
			for (const context of visible) {
				const line = node("div", undefined, "md-context-line");
				const anchor = node("a", context.title || context.name);
				anchor.href = "#";
				anchor.addEventListener("click", (event) => {
					event.preventDefault();
					frappe.set_route("Form", context.doctype, context.name);
				});
				line.appendChild(anchor);
				const details = [sourceLabel(context.source), directionLabel(context.direction), context.sender, context.recipients]
					.filter(Boolean)
					.join(" · ");
				if (details) line.appendChild(node("small", details, "text-muted"));
				cell.appendChild(line);
			}
			if (!isExpanded && all.length > 2) {
				cell.appendChild(button(__("{0} more", [all.length - 2]), "btn btn-link btn-xs md-more", () => {
					this.expanded.add(item.file_id);
					this.renderRows();
				}));
			} else if (isExpanded && all.length > 2) {
				cell.appendChild(button(__("Show less"), "btn btn-link btn-xs md-more", () => {
					this.expanded.delete(item.file_id);
					this.renderRows();
				}));
			}
			return cell;
		}

		openFile(item, disposition) {
			const params = new URLSearchParams({ customer: this.customer, file_id: item.file_id, disposition });
			window.open(`/api/method/${api}.download_document?${params.toString()}`, "_blank", "noopener,noreferrer");
		}

		downloadZip() {
			const ids = Array.from(this.selected.keys());
			if (!ids.length) {
				frappe.msgprint(strings.choose_file);
				return;
			}
			if (ids.length > 100 || Array.from(this.selected.values()).reduce((sum, item) => sum + (Number(item.file_size) || 0), 0) > 100 * 1024 * 1024) {
				frappe.msgprint(strings.zip_limit);
				return;
			}
			this.zipPending = true;
			this.notice.textContent = strings.preparing_zip;
			this.updateSelection();
			const form = node("form");
			form.method = "POST";
			form.action = `/api/method/${api}.download_documents_zip`;
			form.target = "_blank";
			form.rel = "noopener";
			for (const [name, value] of [
				["customer", this.customer],
				["file_ids", JSON.stringify(ids)],
				["csrf_token", frappe.csrf_token],
			]) {
				const input = node("input");
				input.type = "hidden";
				input.name = name;
				input.value = value || "";
				form.appendChild(input);
			}
			document.body.appendChild(form);
			form.submit();
			form.remove();
			setTimeout(() => {
				this.zipPending = false;
				this.notice.textContent = "";
				this.updateSelection();
			}, 2000);
		}
	}

	frappe.provide("kanzlei_erp");
	kanzlei_erp.MandantDocuments = MandantDocuments;
})();
