import { expect, test, type Page } from '@playwright/test';

const generalLabel = 'Alle berechtigten Mandanten';
const pdfLabel = 'Testdokument · Mandant A <em>Aktuell</em> · Seite 1';
const docxLabel = 'Agenda.docx · Mandant B & Partner · Word-Dokument herunterladen';
const paths = {
  conversations: '/v1/chat/conversations',
  docx: '/api/method/kanzlei_erp.ai_connector.api.download_drive_docx',
};

async function login(page: Page, generalAccess?: unknown) {
  if (generalAccess !== undefined) {
    await page.route('**/v1/auth/me', route => route.fulfill({ json: { general_access: generalAccess } }));
    await page.route('**/v1/auth/browser/exchange', async route => {
      const response = await route.fetch();
      const grant = await response.json();
      grant.identity.general_access = generalAccess;
      await route.fulfill({ response, json: grant });
    });
  }
  await page.goto('/');
  // Sign-in opens the default scope without a Mandant; wait for it before choosing another one.
  const initial = page.waitForResponse(r => new URL(r.url()).pathname === paths.conversations
    && r.request().method() === 'GET');
  await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
  await expect(page.getByLabel('Suchbereich')).toBeVisible();
  await initial;
}

async function selectScope(page: Page, label: string, query: object) {
  await expect(page.getByRole('option', { name: label, exact: true })).toHaveCount(1);
  const response = page.waitForResponse(r => new URL(r.url()).pathname === paths.conversations
    && r.request().method() === 'GET');
  await page.getByLabel('Suchbereich').selectOption({ label });
  const result = await response;
  expect(Object.fromEntries(new URL(result.url()).searchParams)).toEqual(query);
  return (await result.json()).items;
}

async function createChat(page: Page, body: object) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === paths.conversations
    && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Neues Gespräch' }).click();
  const result = await response;
  expect(result.request().postDataJSON()).toEqual(body);
  await expect(page.getByLabel('Ihre Frage')).toBeVisible();
  return result.json();
}

async function ask(page: Page, question: string) {
  await page.getByLabel('Ihre Frage').fill(question);
  await page.getByRole('button', { name: 'Senden' }).click();
}

for (const width of [1100, 390]) {
  test(`general citations and isolated history at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    let catalogueRequests = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname === paths.conversations + '/sources') catalogueRequests++;
    });
    await login(page, true);
    await selectScope(page, generalLabel, { scope: 'general' });
    const catalogue = page.getByRole('button', { name: 'Dokumente', exact: true });
    await expect(catalogue).toHaveCount(0);
    const general = await createChat(page, { scope: { kind: 'general' } });
    expect(general).toMatchObject({ client_id: null, scope: { kind: 'general' } });

    await ask(page, 'Testfrage im allgemeinen Suchbereich');
    const pdf = page.getByRole('button', { name: pdfLabel, exact: true });
    await expect(pdf).toContainText('Mandant A <em>Aktuell</em>');
    await expect(pdf.locator('em')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Beleg 1: Testdokument, Mandant A <em>Aktuell</em> · Seite 1', exact: true })).toBeVisible();
    await ask(page, 'What does the Word document say about Berlin (drive)?');
    await expect(page.getByText('Das Seminar in Berlin beginnt am Montag.', { exact: false })).toBeVisible();
    const docx = page.getByRole('button', { name: docxLabel, exact: true });
    await expect(docx).toContainText('Mandant B & Partner');
    await expect(page.locator('.turn .citations > li')).toHaveCount(2);
    await expect(catalogue).toHaveCount(0);
    expect(catalogueRequests).toBe(0);

    await page.route('**/drive/original', route => route.fulfill({
      json: { source_kind: 'docx', locator: '//other.invalid' + paths.docx },
    }), { times: 1 });
    await docx.click();
    await expect(page.getByRole('status')).toContainText('veraltet oder nicht verfügbar');
    const request = page.waitForRequest(r => new URL(r.url()).pathname === paths.docx);
    const download = page.waitForEvent('download');
    await docx.click();
    const original = await request;
    expect(new URL(original.url()).origin).toBe('http://127.0.0.1:19991');
    expect(new URL(original.url()).searchParams.get('customer')).toBe('b');
    expect(original.headers().authorization).toBeUndefined();
    expect((await download).suggestedFilename()).toBe('Agenda.docx');

    await pdf.click();
    await expect(page.locator('#pdf-canvas')).toBeVisible();
    if (width === 390) {
      await expect(page.getByRole('dialog', { name: 'Dokumentansicht' })).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(pdf).toBeFocused();
    }
    expect(await selectScope(page, 'Mandant A', { client_id: 'a' })).toEqual([]);
    await expect(catalogue).toBeVisible();
    await expect(pdf).toHaveCount(0);
    await expect(page.locator('#pdf-canvas')).toHaveCount(0);
    const single = await createChat(page, { client_id: 'a' });
    expect(single).toHaveProperty('client_id', 'a');
    expect(single).not.toHaveProperty('scope');
    await ask(page, 'Testfrage für Mandant A');
    await expect(page.getByRole('button', { name: 'Testdokument · Seite 1', exact: true })).toBeVisible();
    expect(await selectScope(page, generalLabel, { scope: 'general' })).toEqual([general]);
    if (width === 390) {
      await page.getByRole('combobox', { name: 'Gespräche', exact: true }).selectOption(general.id);
    } else {
      await page.locator('.conversation-list button').click();
    }
    await expect(pdf).toBeVisible();
    await expect(docx).toBeVisible();
    await expect(page.getByText('Testfrage für Mandant A')).toHaveCount(0);
    const renamedClient = 'Mandant A – ' + 'Testgesellschaft'.repeat(16);
    await page.route(`**/${general.id}/history`, async route => {
      const response = await route.fetch();
      const history = await response.json();
      history.items[0].citations[0].client.label = renamedClient;
      await route.fulfill({ json: history });
    }, { times: 1 });
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByRole('button', { name: `Testdokument · ${renamedClient} · Seite 1`, exact: true }))
      .toContainText(renamedClient);
    await expect(pdf).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const access of [undefined, false, 'true']) {
  test(`general option is hidden for grant value ${JSON.stringify(access)}`, async ({ page }) => {
    await login(page, access);
    await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(0);
    await selectScope(page, 'Mandant A', { client_id: 'a' });
    await expect(page.getByRole('button', { name: 'Dokumente', exact: true })).toBeVisible();
  });
}

test('a Mandant whose ID is general still uses the single-client contract', async ({ page }) => {
  await page.route('**/v1/connector/clients', route => route.fulfill({
    json: { items: [{ id: 'general', label: 'Mandant Test' }], next_cursor: null },
  }));
  await login(page, true);
  await selectScope(page, 'Mandant Test', { client_id: 'general' });
  await createChat(page, { client_id: 'general' });
  expect(await selectScope(page, generalLabel, { scope: 'general' })).toEqual([]);
  await expect(page.getByLabel('Ihre Frage')).toHaveCount(0);
});

test('late general conversation list and answer cannot overwrite the selected Mandant', async ({ page }) => {
  let releaseList!: () => void, releaseTurn!: () => void;
  const listGate = new Promise<void>(resolve => { releaseList = resolve; });
  const turnGate = new Promise<void>(resolve => { releaseTurn = resolve; });
  await login(page, true);
  await page.route('**/v1/chat/conversations?scope=general', async route => {
    await listGate;
    await route.fulfill({ json: {
      items: [{ id: 'old', client_id: null, scope: { kind: 'general' }, created_at: '2026-01-01T00:00:00Z' }],
      next_cursor: null,
    } }).catch(() => {});
  }, { times: 1 });
  const listRequest = page.waitForRequest('**/v1/chat/conversations?scope=general');
  const listCancelled = page.waitForEvent('requestfailed', request => request.url().endsWith('?scope=general'));
  await page.getByLabel('Suchbereich').selectOption({ label: generalLabel });
  await listRequest;
  await selectScope(page, 'Mandant A', { client_id: 'a' });
  await listCancelled;
  releaseList();
  await expect(page.locator('.conversation-list button')).toHaveCount(0);
  const single = await createChat(page, { client_id: 'a' });
  await selectScope(page, generalLabel, { scope: 'general' });
  const general = await createChat(page, { scope: { kind: 'general' } });
  const delayedTurn = page.waitForRequest(`**/${general.id}/turns`);
  const turnCancelled = page.waitForEvent('requestfailed', request => request.url().endsWith(`/${general.id}/turns`));
  await page.route(`**/${general.id}/turns`, async route => {
    const response = await route.fetch();
    await turnGate;
    await route.fulfill({ response }).catch(() => {});
  });
  await ask(page, 'Verspätete allgemeine Frage');
  await delayedTurn;
  expect(await selectScope(page, 'Mandant A', { client_id: 'a' })).toEqual([single]);
  await turnCancelled;
  await page.locator('.conversation-list button').click();
  await ask(page, 'Aktuelle Frage für Mandant A');
  await expect(page.getByRole('button', { name: 'Testdokument · Seite 1', exact: true })).toBeVisible();
  releaseTurn();
  await page.unrouteAll({ behavior: 'wait' });
  await expect(page.locator('.conversation-list button')).toHaveCount(1);
  await expect(page.getByText('Aktuelle Frage für Mandant A')).toBeVisible();
  await expect(page.getByText('Verspätete allgemeine Frage')).toHaveCount(0);
  await expect(page.getByRole('button', { name: pdfLabel, exact: true })).toHaveCount(0);
});

test('general history revalidation and citation denial clear revoked content', async ({ page }) => {
  await login(page, true);
  await selectScope(page, generalLabel, { scope: 'general' });
  const general = await createChat(page, { scope: { kind: 'general' } });
  await ask(page, 'Testfrage im allgemeinen Suchbereich');
  const pdf = page.getByRole('button', { name: pdfLabel, exact: true });
  await pdf.click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  await page.route(`**/${general.id}/history`, async route => {
    const response = await route.fetch();
    const { items } = await response.json();
    await route.fulfill({ json: {
      items: items.map(item => ({
        id: item.id, created_at: item.created_at, status: 'unavailable',
        question: null, answer: null, citations: [],
      })),
      next_cursor: null,
    } });
  }, { times: 1 });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByText('Dieser Beitrag ist nicht mehr verfügbar.')).toBeVisible();
  await expect(pdf).toHaveCount(0);
  await expect(page.getByText('Testantwort')).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);

  await ask(page, 'What does the Word document say about Berlin (drive)?');
  await page.route('**/drive/original', route => route.fulfill({ status: 403 }));
  await page.route('**/v1/auth/me', route => route.fulfill({ json: { general_access: false } }));
  await page.getByRole('button', { name: docxLabel, exact: true }).click();
  await expect(page.getByRole('status')).toContainText('nicht mehr verfügbar');
  await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(0);
  await expect(page.locator('.turn')).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);
});

test('a visible idle page checks the general role without a reload', async ({ page }) => {
  await page.clock.install();
  await login(page, true);
  await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(1);
  await page.route('**/v1/auth/me', route => route.fulfill({ json: { general_access: false } }));
  await page.clock.fastForward(31_000);
  await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(0);
  await expect(page.getByRole('option', { name: 'Mandant A', exact: true })).toHaveCount(1);
});

test('live role revocation removes general scope, open PDF, and a pending answer', async ({ page }) => {
  await login(page, true);
  await selectScope(page, generalLabel, { scope: 'general' });
  const general = await createChat(page, { scope: { kind: 'general' } });
  await ask(page, 'Testfrage im allgemeinen Suchbereich');
  await page.getByRole('button', { name: pdfLabel, exact: true }).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/${general.id}/turns`, async route => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  const pending = page.waitForRequest(`**/${general.id}/turns`);
  await ask(page, 'Verspätete Frage vor dem Rollenentzug');
  await pending;

  await page.route('**/v1/auth/me', route => {
    expect(route.request().headers().authorization).toMatch(/^Bearer /);
    return route.fulfill({ json: { general_access: false } });
  });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  try {
    await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(0);
    await expect(page.getByLabel('Suchbereich')).toHaveValue('');
    await expect(page.getByLabel('Ihre Frage')).toHaveCount(0);
    await expect(page.locator('.conversation-list button')).toHaveCount(0);
    await expect(page.locator('#pdf-canvas')).toHaveCount(0);
  } finally {
    release();
  }
  await page.unrouteAll({ behavior: 'wait' });
  await expect(page.getByText('Verspätete Frage vor dem Rollenentzug')).toHaveCount(0);
  await expect(page.getByText('Testantwort')).toHaveCount(0);
});

test('client denial clears the revoked selection and reloads current names through client pages', async ({ page }) => {
  await login(page, true);
  await selectScope(page, 'Mandant A', { client_id: 'a' });
  const single = await createChat(page, { client_id: 'a' });
  await ask(page, 'Testfrage für Mandant A');
  await page.getByRole('button', { name: 'Testdokument · Seite 1', exact: true }).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();

  await page.route(`**/${single.id}/history`, route => route.fulfill({ status: 403 }));
  const cursors: (string | null)[] = [];
  await page.route('**/v1/connector/clients', route => {
    expect(route.request().headers().authorization).toMatch(/^Bearer /);
    const { cursor } = route.request().postDataJSON();
    cursors.push(cursor);
    return route.fulfill({ json: cursor === null
      ? { items: [{ id: 'b', label: 'Mandant B aktuell' }], next_cursor: 'weiter' }
      : { items: [{ id: 'c', label: 'Mandant C' }], next_cursor: null } });
  });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('option', { name: 'Mandant A', exact: true })).toHaveCount(0);
  await expect(page.getByRole('option', { name: 'Mandant B', exact: true })).toHaveCount(0);
  await expect(page.getByRole('option', { name: 'Mandant B aktuell', exact: true })).toHaveCount(1);
  await expect(page.getByRole('option', { name: 'Mandant C', exact: true })).toHaveCount(1);
  expect(cursors).toContain('weiter');
  await expect(page.getByLabel('Suchbereich')).toHaveValue('');
  await expect(page.locator('.conversation-list button')).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);
  await expect(page.getByText('Testantwort')).toHaveCount(0);
  await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(1);
});

test('permission checks preserve a still-authorized client conversation and ignore stale checks from another selection', async ({ page }) => {
  await login(page, true);
  await selectScope(page, 'Mandant B', { client_id: 'b' });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/v1/auth/me', async route => {
    await gate;
    await route.fulfill({ status: 401 }).catch(() => {});
  }, { times: 1 });
  const pending = page.waitForRequest('**/v1/auth/me');
  await createChat(page, { client_id: 'b' });
  await pending;
  const cancelled = page.waitForEvent('requestfailed', request => request.url().endsWith('/v1/auth/me'));
  try {
    await selectScope(page, 'Mandant A', { client_id: 'a' });
    await cancelled;
    await createChat(page, { client_id: 'a' });
    await ask(page, 'Testfrage für Mandant A');
    await page.getByRole('button', { name: 'Testdokument · Seite 1', exact: true }).click();
    await expect(page.locator('#pdf-canvas')).toBeVisible();
    await page.getByLabel('Ihre Frage').fill('Gültiger Entwurf');
  } finally {
    release();
  }
  await page.route('**/v1/auth/me', route => route.fulfill({ json: { general_access: false } }));
  await page.route('**/v1/connector/clients', route => route.fulfill({
    json: { items: [{ id: 'a', label: 'Mandant A aktuell' }], next_cursor: null },
  }));
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('option', { name: generalLabel })).toHaveCount(0);
  await expect(page.getByLabel('Suchbereich').locator('option:checked')).toHaveText('Mandant A aktuell');
  await expect(page.getByRole('option', { name: 'Mandant B' })).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  await expect(page.getByText('Testantwort')).toBeVisible();
  await expect(page.getByLabel('Ihre Frage')).toHaveValue('Gültiger Entwurf');
});
