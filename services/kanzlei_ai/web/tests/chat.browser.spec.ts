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
