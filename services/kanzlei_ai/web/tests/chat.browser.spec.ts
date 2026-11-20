import { test, expect } from '@playwright/test';
for (const width of [1100, 390]) test(`German chat flow at ${width}px`,async ({page})=>{
  await page.setViewportSize({width,height:800}); await page.goto('/');
  await page.getByRole('button',{name:'Mit ERP anmelden'}).click();
  await expect(page.getByLabel('Suchbereich')).toBeVisible();
  await page.getByLabel('Suchbereich').selectOption('a');
  await page.getByRole('button',{name:'Neues Gespräch'}).click();
  await page.getByLabel('Ihre Frage').fill('Testfrage'); await page.getByRole('button',{name:'Senden'}).click();
  await expect(page.getByText('Testantwort')).toBeVisible();
  await page.getByRole('button',{name:'Testdokument · Seite 1'}).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  await expect.poll(()=>page.locator('#pdf-canvas').evaluate((canvas:HTMLCanvasElement)=>{const data=canvas.getContext('2d')!.getImageData(0,0,canvas.width,canvas.height).data; for(let i=0;i<data.length;i+=4) if(data[i+3]>0 && (data[i]<240 || data[i+1]<240 || data[i+2]<240)) return true; return false;})).toBe(true);
  if (width === 390) {
    await expect(page.getByRole('dialog',{name:'Dokumentansicht'})).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button',{name:'Testdokument · Seite 1'})).toBeFocused();
  } else {
    await page.getByRole('button',{name:'Dokument schließen'}).click();
  }
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);
  await page.getByLabel('Suchbereich').selectOption('b'); await expect(page.getByText('Testantwort')).toHaveCount(0);
  await page.getByRole('button',{name:'Abmelden'}).click(); await expect(page.getByRole('button',{name:'Mit ERP anmelden'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('opens an email citation via fresh authorization and keeps German controls', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
  await page.getByLabel('Suchbereich').selectOption('a');
  await page.getByRole('button', { name: 'Neues Gespräch' }).click();
  const question = page.getByLabel('Ihre Frage');
  await question.fill('Когда встреча?');
  await page.getByRole('button', { name: 'Senden' }).click();
  await expect(page.getByText('Не найдено достаточных подтверждений.')).toBeVisible();

  await question.fill('What does the email say about Berlin?');
  await page.getByRole('button', { name: 'Senden' }).click();
  await expect(page.getByText('The email says:', { exact: false })).toBeVisible();
  await expect(page.getByText('Die Besprechung in Berlin ist am Montag.', { exact: false })).toBeVisible();
  const citation = page.getByRole('button', { name: 'Termin · E-Mail im ERP öffnen', exact: true });
  await expect(citation).toBeVisible();
  const request = page.waitForRequest(req => req.url().endsWith('/email/original'));
  const destination = page.waitForRequest(req => new URL(req.url()).pathname.startsWith('/desk/communication/'));
  await citation.click();
  expect((await request).headers().authorization).toMatch(/^Bearer /);
  const navigation = await destination;
  expect(navigation.url()).toBe('http://127.0.0.1:19991/desk/communication/Test%20%23%3F');
  expect(navigation.headers().authorization).toBeUndefined();
  await expect(page.getByText('ERP communication fixture')).toBeVisible();
});

for (const rejected of ['permission', 'locator']) {
  test(`email citation does not navigate after rejected ${rejected}`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto('/');
    await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
    await page.getByLabel('Suchbereich').selectOption('a');
    await page.getByRole('button', { name: 'Neues Gespräch' }).click();
    await page.getByLabel('Ihre Frage').fill('What does the email say about Berlin?');
    await page.getByRole('button', { name: 'Senden' }).click();
    await page.route('**/email/original', route => rejected === 'permission'
      ? route.fulfill({ status: 404 })
      : route.fulfill({ json: { source_kind: 'email', locator: '/desk/communication/%2E%2E' } }));
    await page.getByRole('button', { name: 'Termin · E-Mail im ERP öffnen', exact: true }).click();
    await expect(page.locator('#chat-status')).toContainText(rejected === 'permission'
      ? 'nicht mehr verfügbar' : 'veraltet oder nicht verfügbar');
    await expect(page).toHaveURL('http://127.0.0.1:19992/');
  });
}
