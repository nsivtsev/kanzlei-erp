import { expect, test, type Page } from '@playwright/test';

async function openChat(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
  await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant A' });
  await page.getByRole('button', { name: 'Neues Gespräch' }).click();
  await expect(page.getByLabel('Ihre Frage')).toBeVisible();
}

test('the chat leaves catalogue pages on demand and sends filters to the server', async ({ page }) => {
  const requests: object[] = [];
  await page.route('**/sources', route => {
    const body = route.request().postDataJSON();
    requests.push(body);
    const title = body.cursor ? 'Archiv.pdf' : body.query ? 'Rechnung.pdf' : 'Testdokument';
    return route.fulfill({ json: {
      items: [{ source_id: title, title, status: 'ready' }],
      next_cursor: body.cursor ? null : 'next',
    } });
  });
  await openChat(page);
  await page.getByLabel('Ihre Frage').fill('Testfrage');
  await page.getByRole('button', { name: 'Senden' }).click();
  await expect(page.getByText('Testantwort')).toBeVisible();
  expect(requests).toHaveLength(0);

  await page.getByRole('button', { name: 'Dokumente', exact: true }).click();
  await expect(page.getByText('Testdokument', { exact: true })).toBeVisible();
  expect(requests).toHaveLength(1);
  await page.getByRole('button', { name: 'Weiter' }).click();
  await expect(page.getByText('Archiv.pdf')).toBeVisible();
  await expect(page.getByText('Testdokument', { exact: true })).toHaveCount(0);
  await page.getByLabel('Dateiname').fill('Rechnung');
  await page.getByLabel('Status', { exact: true }).selectOption('ready');
  await page.getByRole('button', { name: 'Suchen' }).click();
  await expect(page.getByText('Rechnung.pdf')).toBeVisible();
  expect(requests).toEqual([
    { client_id: 'a', cursor: null, query: '', status: 'all' },
    { client_id: 'a', cursor: 'next', query: '', status: 'all' },
    { client_id: 'a', cursor: null, query: 'Rechnung', status: 'ready' },
  ]);
});

test('a delayed catalogue page cannot populate a different scope', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/sources', async route => {
    const body = route.request().postDataJSON();
    if (body.client_id === 'a') await gate;
    await route.fulfill({ json: {
      items: [{ source_id: 's', title: body.client_id === 'a' ? 'Altes Dokument' : 'Dokument B', status: 'ready' }],
      next_cursor: null,
    } }).catch(() => {});
  });
  await openChat(page);
  const requested = page.waitForRequest('**/sources');
  await page.getByRole('button', { name: 'Dokumente', exact: true }).click();
  await requested;
  await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant B' });
  release();
  await page.getByRole('button', { name: 'Dokumente', exact: true }).click();
  await expect(page.getByText('Dokument B')).toBeVisible();
  await expect(page.getByText('Altes Dokument')).toHaveCount(0);
});

test('history revalidation clears an open PDF after revocation without querying the catalogue', async ({ page }) => {
  let revoked = false;
  let catalogueRequests = 0;
  page.on('request', request => { if (new URL(request.url()).pathname.endsWith('/sources')) catalogueRequests++; });
  await page.route('**/history', route => route.fulfill({ json: {
    items: revoked ? [{
      id: 't', created_at: '2026-01-01T00:00:00Z', status: 'unavailable',
      question: null, answer: null, citations: [],
    }] : [], next_cursor: null,
  } }));
  await openChat(page);
  await page.getByLabel('Ihre Frage').fill('Testfrage');
  await page.getByRole('button', { name: 'Senden' }).click();
  await page.getByRole('button', { name: 'Testdokument · Seite 1' }).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  await page.getByLabel('Ihre Frage').fill('Entwurf');
  revoked = true;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByText('Dieser Beitrag ist nicht mehr verfügbar.')).toBeVisible();
  await expect(page.getByText('Testantwort')).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);
  await expect(page.getByLabel('Ihre Frage')).toHaveValue('Entwurf');
  expect(catalogueRequests).toBe(0);
});
