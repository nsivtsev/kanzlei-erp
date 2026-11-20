import { test, expect } from '@playwright/test';
// Run against an isolated synthetic fixture with an authenticated ERP browser session.
test.skip(process.env.KANZLEI_AI_REAL_MILESTONE !== '1', 'Opt-in isolated fixture required');
test('isolated synthetic question and citation', async ({page}) => {
  const {KANZLEI_AI_REAL_URL:url,KANZLEI_AI_REAL_CLIENT:client,KANZLEI_AI_REAL_QUESTION:question,KANZLEI_AI_REAL_ANSWER:answer} = process.env;
  if(!url||!client||!question||!answer) throw new Error('Isolated fixture configuration is incomplete');
  await page.goto(url); await page.getByRole('button',{name:'Mit ERP anmelden'}).click();
  await page.getByLabel('Suchbereich').selectOption({label:client});
  await page.getByRole('button',{name:'Neues Gespräch'}).click();
  await page.getByRole('button',{name:'Dokumente',exact:true}).click();
  await expect(page.getByText('Bereit',{exact:true}).first()).toBeVisible();
  await page.getByRole('button',{name:'Zum Gespräch'}).click();
  await page.getByLabel('Ihre Frage').fill(question); await page.getByRole('button',{name:'Senden'}).click();
  await expect.poll(async()=>page.locator('.turn').last().evaluate((e,expected)=>e.textContent?.includes(expected) ?? false,answer)).toBe(true);
  await page.locator('.turn .citation').first().click(); await expect(page.locator('#pdf-canvas')).toBeVisible();
  await page.getByRole('button',{name:'Dokument schließen'}).click(); await page.getByRole('button',{name:'Abmelden'}).click();
});
