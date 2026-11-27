const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const source = fs.readFileSync("kanzlei_erp/kanzlei_erp/page/kanzlei_mail/kanzlei_mail.js", "utf8");

function client() {
	const composers = [];
	const context = {
		frappe: {
			pages: { "kanzlei-mail": {} },
			boot: { lang: "de" },
			utils: {
				escape_html: (s) =>
					String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
				get_form_link: (doctype, name) => `/desk/${doctype}/${name}`,
			},
			views: {
				CommunicationComposer: function (args) {
					composers.push(args);
				},
			},
		},
		__: (s, args) => (args ? s.replace(/\{(\d+)\}/g, (_, i) => args[i]) : s),
		Intl,
		Date,
	};
	vm.createContext(context);
	vm.runInContext(source + "\nthis.Mail = KanzleiMail;", context);
	const mail = Object.create(context.Mail.prototype);
	mail.state = { folder: "inbox", account: "" };
	mail.accounts = [{ name: "Shared", email_id: "office@example.invalid", can_send: true }];
	return { mail, composers };
}

test("message rows escape mail content and show the first Mandant", () => {
	const { mail } = client();
	const html = mail.rowHtml({
		name: 'x"><img src=x>',
		subject: "<script>alert(1)</script>",
		sender: "a@example.invalid",
		sender_full_name: "<b>Eve</b>",
		preview: "<i>hi</i>",
		communication_date: "2026-01-02 09:00:00",
		sent_or_received: "Received",
		seen: false,
		mandants: [
			{ name: "C1", customer_name: "Müller & Co" },
			{ name: "C2", customer_name: "Other" },
		],
	});
	assert.doesNotMatch(html, /<script>|<b>Eve|<i>hi|"><img/);
	assert.match(html, /is-unread/);
	assert.match(html, /Müller &amp; Co/);
	assert.match(html, /\+1/);
});

test("message body blocks remote images and scripts until images are allowed", () => {
	const { mail } = client();
	const content = '<p>Hi</p><img src="https://tracker.example/p.gif"><script>steal()</script>';
	assert.ok(mail.hasRemoteImages(content));
	assert.ok(!mail.hasRemoteImages('<img src="/private/files/a.png">'));
	const blocked = mail.bodyDocument(content, false);
	assert.match(blocked, /default-src 'none'/);
	assert.match(blocked, /img-src 'self' data:;?"/);
	assert.doesNotMatch(blocked, /script-src/);
	assert.match(mail.bodyDocument(content, true), /img-src 'self' data: https: http:/);
	assert.match(mail.bodyDocument("plain <text> & more", false), /<pre class="km-plain">plain &lt;text&gt; &amp; more<\/pre>/);
});

test("replies keep a single prefix and address the right people", () => {
	const { mail, composers } = client();
	mail.message = {
		name: "COMM-1",
		subject: "AW: Belege",
		sender: "client@example.invalid",
		recipients: "office@example.invalid",
		cc: "tax@example.invalid",
		content: "<p>x</p>",
		sent_or_received: "Received",
		account_email_id: "office@example.invalid",
		attachments: [{ name: "F1" }],
	};
	mail.compose("reply");
	mail.compose("reply_all");
	mail.compose("forward");
	assert.equal(composers[0].subject, "AW: Belege");
	assert.equal(composers[0].recipients, "client@example.invalid");
	assert.equal(composers[0].cc, undefined);
	assert.equal(composers[0].in_reply_to, undefined);
	assert.equal(composers[0].last_email.name, "COMM-1");
	assert.equal(composers[1].cc, "tax@example.invalid");
	assert.equal(composers[2].subject, "Fw: AW: Belege");
	assert.equal(composers[2].forward, true);
	assert.equal(composers[2].attachments.length, 1);
	mail.message = null;
	mail.compose();
	assert.equal(composers[3].sender, "office@example.invalid");
	assert.equal(composers[3].is_a_reply, undefined);
});

test("list groups follow the calendar, not a rolling 24 hours", () => {
	const { mail } = client();
	const at = (days, hour) => {
		const d = new Date();
		d.setDate(d.getDate() - days);
		d.setHours(hour, 0, 0, 0);
		const p = (n) => String(n).padStart(2, "0");
		return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(hour)}:00:00`;
	};
	assert.equal(mail.groupLabel(at(0, 0)), "Today");
	assert.equal(mail.groupLabel(at(1, 23)), "Yesterday");
	assert.equal(mail.groupLabel(null), "Undated");
	assert.match(mail.groupLabel("2020-03-05 10:00:00"), /2020/);
});
