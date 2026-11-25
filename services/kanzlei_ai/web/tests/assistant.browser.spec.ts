import { expect, test } from '@playwright/test';

for (const width of [1100, 390]) {
  test(`starts without a Mandant and renders general and document answers at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/');
    await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
    const scope = page.getByLabel('Suchbereich');
    await expect(scope).toHaveValue('assistant');
    await expect(page.getByRole('option', { name: 'Ohne Mandant' })).toHaveCount(1);

    const created = page.waitForRequest(r => new URL(r.url()).pathname === '/v1/chat/conversations' && r.method() === 'POST');
    await page.getByRole('button', { name: 'Neues Gespräch' }).click();
    expect((await created).postDataJSON()).toEqual({ scope: { kind: 'assistant' } });
    await expect(page.getByLabel('Nur wörtliche Belege')).toHaveCount(0);

    const turn = page.waitForRequest(r => r.url().endsWith('/turns'));
    await page.getByLabel('Ihre Frage').fill('Was ist der Vorsteuerabzug?');
    await page.getByRole('button', { name: 'Senden' }).click();
    expect((await turn).postDataJSON()).toEqual({ question: 'Was ist der Vorsteuerabzug?', strict: false });
    await expect(page.locator('#streaming-answer')).toContainText('Vorsteuerabzug');
    const answer = page.locator('.answer.markdown').last();
    await expect(answer.locator('strong', { hasText: 'Vorsteuerabzug' })).toBeVisible();
    await expect(answer.locator('ol > li')).toHaveCount(2);
    await expect(answer.locator('code', { hasText: 'Leistung' })).toBeVisible();
    // Model output is text, never HTML.
    await expect(answer.locator('img')).toHaveCount(0);
    await expect(answer).toContainText('<img src=x');
    expect(await page.evaluate(() => (window as unknown as { injected?: number }).injected)).toBeUndefined();
    await expect(page.getByText('Allgemeine Antwort · nicht aus Kanzlei-Dokumenten')).toBeVisible();
    await expect(page.locator('#streaming-answer')).toHaveCount(0);

    await scope.selectOption({ label: 'Mandant A' });
    await page.getByRole('button', { name: 'Neues Gespräch' }).click();
    const strict = page.getByLabel('Nur wörtliche Belege');
    await expect(strict).not.toBeChecked();
    const grounded = page.waitForRequest(r => r.url().endsWith('/turns'));
    await page.getByLabel('Ihre Frage').fill('Assistent: Wie hoch ist der Betrag?');
    await page.getByRole('button', { name: 'Senden' }).click();
    expect((await grounded).postDataJSON().strict).toBe(false);
    await expect(page.getByText('Antwort aus Kanzlei-Dokumenten')).toBeVisible();
    const marker = page.getByRole('button', { name: 'Beleg 1: Testdokument, Seite 1' });
    await expect(marker).toBeVisible();
    await expect(page.locator('.answer.markdown').last()).not.toContainText('[7]');
    await marker.click();
    await expect(page.locator('#pdf-canvas')).toBeVisible();
    if (width === 390) await page.keyboard.press('Escape');
    else await page.getByRole('button', { name: 'Dokument schließen' }).click();

    await strict.check();
    const exact = page.waitForRequest(r => r.url().endsWith('/turns'));
    await page.getByLabel('Ihre Frage').fill('Testfrage');
    await page.getByRole('button', { name: 'Senden' }).click();
    expect((await exact).postDataJSON()).toEqual({ question: 'Testfrage', strict: true });
    await expect(page.getByText('Testantwort')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const width of [1100, 390]) {
  test(`attaches documents from the catalogue and labels ERP answers at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/');
    await page.getByRole('button', { name: 'Mit ERP anmelden' }).click();
    await page.getByLabel('Suchbereich').selectOption({ label: 'Mandant A' });
    await page.getByRole('button', { name: 'Neues Gespräch' }).click();

    const open = page.getByRole('button', { name: 'Dokument anhängen' });
    await open.click();
    await expect(open).toHaveAttribute('aria-expanded', 'true');
    const catalogue = page.waitForRequest(r => r.url().endsWith('/v1/chat/conversations/sources'));
    await page.getByLabel('Dokumente suchen').fill('test');
    expect((await catalogue).postDataJSON()).toEqual({ client_id: 'a', query: 'test', status: 'ready' });
    await page.getByRole('button', { name: 'Testdokument', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Anhang entfernen: Testdokument' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Testdokument (angehängt)' })).toBeDisabled();
    await page.keyboard.press('Escape');
    await expect(open).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByLabel('Dokumente suchen')).toHaveCount(0);

    // Strict mode cannot use attachments; the chips stay for the next generative turn.
    const strict = page.getByLabel('Nur wörtliche Belege');
    await strict.check();
    await expect(open).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Anhang entfernen: Testdokument' })).toHaveCount(0);
    await strict.uncheck();

    const turn = page.waitForRequest(r => r.url().endsWith('/turns'));
    await page.getByLabel('Ihre Frage').fill('Fasse den Anhang zusammen.');
    await page.getByRole('button', { name: 'Senden' }).click();
    expect((await turn).postDataJSON()).toEqual({
      question: 'Fasse den Anhang zusammen.', strict: false, attachments: [{ source_id: 's', client_id: 'a' }],
    });
    await expect(page.locator('.answer.markdown').last()).toContainText('Zusammenfassung von Testdokument');
    await expect(page.getByText('Antwort aus Kanzlei-Dokumenten')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Anhang entfernen: Testdokument' })).toHaveCount(0);

    const next = page.waitForRequest(r => r.url().endsWith('/turns'));
    await page.getByLabel('Ihre Frage').fill('Fristen in den nächsten 14 Tagen?');
    await page.getByRole('button', { name: 'Senden' }).click();
    expect((await next).postDataJSON()).toEqual({ question: 'Fristen in den nächsten 14 Tagen?', strict: false });
    await expect(page.getByText('Antwort aus ERP-Daten')).toBeVisible();
    await expect(page.locator('.answer.markdown').last().locator('strong', { hasText: 'USt-Voranmeldung' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
