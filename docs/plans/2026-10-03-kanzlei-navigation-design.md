# Kanzlei navigation

The approved interface has one Kanzlei sidebar with six entries: Mandanten, Aufgaben, Kalender, Wiederholungen, Zeiterfassung, and Rechnungen. These open ERPNext Customer, Task, the Task calendar, Work Schedule, Timesheet, and Sales Invoice. A separate Einstellungen sidebar is available to Administrator and users with System Manager, with links needed to configure the company, services, activity types, billing, users, and imports.

Customer remains the single Mandant record and the billing party. User-facing Customer terminology becomes Mandant. Stock, Manufacturing, Buying, Selling, and other standard workspaces are removed from the Desk navigation payload. The underlying ERPNext modules remain installed so Customer, Timesheet, and Sales Invoice retain their dependencies. This is navigation configuration, not an authorization boundary; document permissions continue to be enforced by ERPNext.

The Kanzlei app ships standard Workspace Sidebar and Desktop Icon documents, terminology translations, and a boot-session hook that exposes only the two Kanzlei sidebars and their desktop icons. Settings are omitted for users without System Manager. Default landing is the Mandant list. No existing ERPNext source or user data is changed. Installation and migration activate the interface in both development and production.

Validation checks the six navigation targets, administrator versus staff settings visibility, actual boot output with no standard modules, translations, and browser navigation through Mandants, time tracking, billing, and the calendar. Existing work-calendar tests remain green. Billing configuration and automated conversion of time entries into invoices are outside this navigation change.
