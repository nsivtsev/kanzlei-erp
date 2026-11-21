import { expect, test } from '@playwright/test';

test('theme starts from system preference and the explicit choice persists', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  const root = page.locator('html');
  await expect(root).toHaveCSS('color-scheme', 'light');
  const switcher = page.getByRole('group', { name: 'Farbschema' });
  await switcher.getByRole('button', { name: 'Dunkel' }).click();
  await expect(root).toHaveCSS('color-scheme', 'dark');
  await expect(switcher.getByRole('button', { name: 'Dunkel' })).toHaveAttribute('aria-pressed', 'true');
  await page.reload();
  await expect(root).toHaveCSS('color-scheme', 'dark');
  await switcher.getByRole('button', { name: 'Hell' }).press('Enter');
  await expect(root).toHaveCSS('color-scheme', 'light');
});

test('switching the theme leaves the draft, PDF canvas and scope in place', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
  await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant A' });
  await page.getByRole('button', { name: 'Neues Gespräch' }).click();
  const field = page.getByLabel('Ihre Frage');
  await field.fill('Testfrage');
  await page.getByRole('button', { name: 'Senden' }).click();
  await page.getByRole('button', { name: 'Testdokument · Seite 1' }).click();
  const canvas = page.locator('#pdf-canvas');
  await expect(canvas).toBeVisible();
  await canvas.evaluate(element => { element.dataset.sameNode = 'yes'; });
  await field.fill('Noch nicht gesendet');
  await page.getByRole('button', { name: 'Dunkel' }).click();
  await expect(page.getByRole('button', { name: 'Abmelden' })).toHaveCSS('color', 'rgb(242, 245, 247)');
  const lightButton = page.getByRole('button', { name: 'Hell' });
  const buttonColor = await lightButton.evaluate((control: HTMLButtonElement) => {
    const logout = document.querySelector<HTMLElement>('[data-focus="logout"]')!;
    getComputedStyle(logout).color; // Resolve the dark controls before switching.
    control.click();
    return getComputedStyle(logout).color;
  });
  expect(buttonColor).toBe('rgb(21, 29, 40)');
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'light');
  await expect(field).toHaveValue('Noch nicht gesendet');
  await expect(page.getByLabel('Suchbereich').locator('option:checked')).toHaveText('Mandant A');
  await expect(canvas).toHaveAttribute('data-same-node', 'yes');
});

test('the narrow layout keeps controls reachable with reduced motion', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  await page.goto('/');
  const dark = page.getByRole('button', { name: 'Dunkel' });
  await expect(dark).toHaveAttribute('aria-pressed', 'true');
  await expect(dark).toHaveCSS('transition-duration', '0s');
  await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
  await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant A' });
  await page.getByRole('button', { name: 'Neues Gespräch' }).click();
  await expect(page.getByRole('button', { name: 'Senden' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
});
