import { test, expect } from '@playwright/test';

test('pending sources become ready without leaving the conversation', async ({page}) => {
  let requests=0;
  await page.route('**/sources', async route => {
    requests++;
    await route.fulfill({json:{items:[{source_id:'s',title:'Testdokument',status:requests===1?'pending':'ready'}],next_cursor:null}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'Mit ERP anmelden'}).click();
  await page.getByLabel('Mandant').selectOption('a');
  await page.getByRole('button',{name:'Neues Gespräch'}).click();
  await expect(page.getByText('In Verarbeitung',{exact:true})).toBeVisible();
  await page.getByLabel('Ihre Frage').fill('Testfrage');
  await page.getByRole('button',{name:'Frage stellen'}).click();
  await page.getByRole('button',{name:'Testdokument · Seite 1'}).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  await page.getByLabel('Ihre Frage').fill('Entwurf');
  await expect(page.getByText('Bereit',{exact:true})).toBeVisible({timeout:8000});
  await expect(page.getByLabel('Ihre Frage')).toHaveValue('Entwurf');
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  expect(requests).toBe(2);
  await page.waitForTimeout(3300);
  expect(requests).toBe(2);
});

test('a delayed refresh cannot update a different client', async ({page}) => {
  let calls=0, release!:()=>void, entered!:()=>void;
  const gate=new Promise<void>(resolve=>release=resolve);
  const waiting=new Promise<void>(resolve=>entered=resolve);
  await page.route('**/sources', async route => {
    calls++;
    if(calls===1) return route.fulfill({json:{items:[{source_id:'s',title:'Altes Dokument',status:'pending'}],next_cursor:null}});
    entered(); await gate;
    await route.fulfill({json:{items:[{source_id:'s',title:'Altes Dokument',status:'ready'}],next_cursor:null}}).catch(()=>{});
  });
  await page.goto('/'); await page.getByRole('button',{name:'Mit ERP anmelden'}).click();
  await page.getByLabel('Mandant').selectOption('a'); await page.getByRole('button',{name:'Neues Gespräch'}).click();
  await expect(page.getByText('In Verarbeitung',{exact:true})).toBeVisible();
  await waiting;
  await page.getByLabel('Mandant').selectOption('b'); release();
  await page.waitForTimeout(250);
  await expect(page.getByText('Altes Dokument')).toHaveCount(0);
  await expect(page.getByLabel('Mandant')).toHaveValue('b');
});

test('a source removed by a successful refresh invalidates history and the open PDF', async ({page}) => {
  let removed=false;
  await page.route('**/sources', route => route.fulfill({json:{
    items:removed?[]:[{source_id:'s',title:'Testdokument',status:'ready'}],next_cursor:null,
  }}));
  await page.route('**/history', route => route.fulfill({json:{items:removed?[{
    id:'t',created_at:'2026-01-01T00:00:00Z',status:'unavailable',question:null,answer:null,citations:[],
  }]:[],next_cursor:null}}));
  await page.goto('/');
  await page.getByRole('button',{name:'Mit ERP anmelden'}).click();
  await page.getByLabel('Mandant').selectOption('a');
  await page.getByRole('button',{name:'Neues Gespräch'}).click();
  await expect(page.getByText('Bereit',{exact:true})).toBeVisible();
  await page.getByLabel('Ihre Frage').fill('Testfrage');
  await page.getByRole('button',{name:'Frage stellen'}).click();
  await page.getByRole('button',{name:'Testdokument · Seite 1'}).click();
  await expect(page.locator('#pdf-canvas')).toBeVisible();
  removed=true;
  await page.getByRole('button',{name:'Quellen aktualisieren'}).click();
  await expect(page.getByText('Dieser Beitrag ist nicht mehr verfügbar.')).toBeVisible();
  await expect(page.getByText('Testantwort')).toHaveCount(0);
  await expect(page.locator('#pdf-canvas')).toHaveCount(0);
});
