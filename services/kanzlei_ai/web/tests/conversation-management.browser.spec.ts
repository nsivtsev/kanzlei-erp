import { expect, test } from '@playwright/test';

for (const width of [1100, 390]) {
  test(`conversation deletion is explicit and scoped at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/');
    await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
    await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant A' });
    const created = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/v1/chat/conversations'
      && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Neues Gespräch' }).click();
    const thread = (await (await created).json()).id;
    const current = { id: thread, created_at: '2026-01-01T12:00:00Z', scope_kind: 'client' };
    const older = { id: 'previous', created_at: '2020-02-03T12:00:00Z', scope_kind: 'general' };
    let rows = [current, older];
    let deletes = 0;
    await page.route('**/v1/chat/conversations/owned', route =>
      route.fulfill({ json: { items: rows, next_cursor: null } }));
    await page.route(`**/v1/chat/conversations/${thread}`, async route => {
      expect(route.request().method()).toBe('DELETE');
      deletes++;
      rows = [older];
      await route.fulfill({ status: 204 });
    });

    const manage = page.getByRole('button', { name: 'Gespräche verwalten' });
    await manage.click();
    const dialog = page.getByRole('dialog', { name: 'Gespräche verwalten' });
    const confirmation = page.getByRole('dialog', { name: 'Gespräch löschen?' });
    const removeCurrent = dialog.getByRole('listitem').first().getByRole('button', { name: 'Löschen' });
    await removeCurrent.click();
    await expect(confirmation.getByRole('button', { name: 'Abbrechen' })).toBeFocused();
    await confirmation.getByRole('button', { name: 'Abbrechen' }).click();
    expect(deletes).toBe(0);

    await removeCurrent.click();
    await confirmation.getByRole('button', { name: 'Gespräch löschen' }).click();
    await expect(dialog.getByRole('status')).toContainText('Gespräch gelöscht.');
    expect(deletes).toBe(1);
    await expect(dialog.getByRole('listitem')).toHaveCount(1);
    await expect(dialog.getByText('Mandantenübergreifend')).toBeVisible();
    await dialog.getByRole('button', { name: 'Schließen' }).click();
    await expect(manage).toBeFocused();
    await expect(page.getByLabel('Ihre Frage')).toHaveCount(0);
  });
}
