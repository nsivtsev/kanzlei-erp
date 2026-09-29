const taskCalendar = frappe.views.calendar["Task"];
taskCalendar.field_map.title = "kanzlei_calendar_title";

// Frappe hides the standard filter bar in Calendar View. Show it for Task so
// the Mandant custom field can be selected without editing the route by hand.
if (!frappe.views.CalendarView.prototype.kanzleiTaskFilterBar) {
	const originalSetupPage = frappe.views.CalendarView.prototype.setup_page;
	frappe.views.CalendarView.prototype.setup_page = function () {
		if (this.doctype === "Task") {
			this.hide_page_form = false;
			frappe.views.ListView.prototype.setup_page.call(this);
		} else {
			originalSetupPage.call(this);
		}
	};
	frappe.views.CalendarView.prototype.kanzleiTaskFilterBar = true;
}
