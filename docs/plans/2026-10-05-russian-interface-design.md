# Russian interface design

## Goal

Give Kanzlei ERP users a complete Russian version of the app's custom interface while keeping German available for other users.

## Approved approach

Use Frappe's existing translation dictionaries and complete `kanzlei_erp/translations/ru.csv` so every source string in `de.csv` has a Russian translation. Preserve the existing user-level language preference and the German site default configured during local setup. Frappe will continue to use upstream translations for ERPNext's own strings where its installed locale provides them; this change will not fork or overwrite ERPNext translations.

## Scope

Translate all German-localized Kanzlei ERP terms, workspace and form labels, actions, workflow states, validation messages, and document-browser text represented by the app's German dictionary. Russian values should use natural Russian equivalents rather than German loan terms: `Mandant` becomes `клиент`, and `FiBu` becomes `бухгалтерский учёт` or a context-appropriate form. Keep source strings and formatting placeholders intact.

## Compatibility

No language-selection UI or migration behavior is added. Existing users retain their language settings; Russian-speaking users select Russian in their ERPNext user profile. The setup's German default remains the fallback for users without a personal preference.
