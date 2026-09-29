# Mandant work calendar

Use ERPNext Customer as the Mandant and ERPNext Task as the single record for a piece of work. A Task has one due date (`exp_end_date`), status, assignee, and a custom Customer link. One-off work is entered as a Task. A new `Work Schedule` DocType defines recurring work and creates ordinary Tasks for each due date; each occurrence is completed independently.

The first version supports weekly, monthly, quarterly, and yearly schedules with a first due date, optional last due date, Customer, subject, and optional assignee. A daily job generates occurrences through the next twelve months. It does not create historical backlog older than thirty days. The day-of-month is retained where possible; if the first date is the last day of its month, later dates are also month-end. Automatically calculated dates are editable on the generated Task. No statutory calendar or separate internal deadline is implemented.

Generated Tasks carry a reference to their schedule and a unique occurrence key. Generation is idempotent, including after retries or a manual run. Disabling a schedule stops new generation; existing Tasks remain unchanged. A Task validation hook maintains a calendar title containing the Mandant name and work subject. A calendar hook extends ERPNext's existing Task calendar with a Customer filter and this title.

The app owns the Work Schedule DocType, hooks, fixtures for Task custom fields, and tests. The Task calendar is reused rather than replaced. Existing ERPNext Task and Customer records remain usable. A first pilot uses two fictitious Customers, one one-off Task, and one monthly Work Schedule.

Validation covers date generation across short months and leap years, duplicate prevention, Customer linkage, independent Task completion, the Calendar metadata hook, and visible events in the local Task calendar.
