"use strict";

async function connectChat() {
	const status = document.querySelector("[data-handoff]");
	const { origin, state, challenge, csrfToken } = status.dataset;
	const binding = /^[A-Za-z0-9_-]{43}$/;
	const opener = window.opener;
	try {
		const target = new URL(origin);
		if (
			!opener || opener.closed || target.origin !== origin
			|| !["https:", "http:"].includes(target.protocol)
			|| !binding.test(state) || !binding.test(challenge) || !csrfToken
		) {
			throw new Error();
		}
		const response = await fetch("/api/method/kanzlei_erp.ai_connector.api.issue_browser_ticket", {
			method: "POST",
			credentials: "same-origin",
			cache: "no-store",
			redirect: "error",
			headers: {
				"Content-Type": "application/json",
				"X-Frappe-CSRF-Token": csrfToken,
			},
			body: JSON.stringify({ version: 1, state, challenge }),
		});
		if (!response.ok) throw new Error();
		const { message } = await response.json();
		if (message?.version !== 1 || typeof message.ticket !== "string" || !binding.test(message.ticket)) {
			throw new Error();
		}
		if (opener.closed) throw new Error();
		opener.postMessage({ type: "kanzlei-ai:ticket", state, ticket: message.ticket }, origin);
		status.textContent = "Sie können dieses Fenster schließen.";
		window.close();
	} catch {
		status.textContent = "Die Verbindung zum Kanzlei-Chat konnte nicht hergestellt werden.";
	}
}

void connectChat();
