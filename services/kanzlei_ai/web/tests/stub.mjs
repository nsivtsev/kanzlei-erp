import http from 'node:http';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
const port=Number(process.env.STUB_PORT||19991);
const conversations = new Map();
const downloadPaths = {
  attachment: '/api/method/kanzlei_erp.ai_connector.api.download_mail_attachment',
  drive: '/api/method/kanzlei_erp.ai_connector.api.download_drive_docx',
};
const downloadQuery = new URLSearchParams({
  customer: 'a',
  file_id: 'Agenda + #?.docx',
  revision: 'a'.repeat(64),
}).toString();
const generalDownloadQuery = new URLSearchParams(downloadQuery);
generalDownloadQuery.set('customer', 'b');
const generalClients = {
  pdf: { id: 'a', label: 'Mandant A <em>Aktuell</em>' },
  docx: { id: 'b', label: 'Mandant B & Partner' },
};
const examplePdf = Buffer.from(
  readFileSync(new URL('../../tests/fixtures/documents/synthetic-german-native.pdf.b64', import.meta.url), 'utf8'),
  'base64',
);
const json=(res,data)=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
function pdf(){const objects=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>','<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>','<< /Length 42 >>\nstream\nBT /F1 12 Tf 20 100 Td (Synthetic) Tj ET\nendstream','<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];let s='%PDF-1.4\n';const offsets=[0];for(let i=0;i<objects.length;i++){offsets.push(Buffer.byteLength(s));s+=`${i+1} 0 obj\n${objects[i]}\nendobj\n`;}const x=Buffer.byteLength(s);s+=`xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n=>String(n).padStart(10,'0')+' 00000 n ').join('\n')}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${x}\n%%EOF`;return s;}
http.createServer((req,res)=>{const u=new URL(req.url,`http://127.0.0.1:${port}`);let body='';req.on('data',x=>body+=x);req.on('end',()=>{if(u.pathname==='/kanzlei-ai-connect'){res.writeHead(200,{'Content-Type':'text/html','Set-Cookie':'erp_fixture=active; Path=/; HttpOnly; SameSite=Lax'});res.end(`<script>opener.postMessage({type:'kanzlei-ai:ticket',state:new URL(location.href).searchParams.get('state'),ticket:'fixture'},'http://127.0.0.1:19992')</script>`);return;}
if(u.pathname==='/v1/auth/browser/config')return json(res,{version:1,erp_origin:`http://127.0.0.1:${port}`});
if(u.pathname==='/v1/auth/browser/exchange')return json(res,{access_token:randomUUID(),token_type:'bearer',identity:{expires_at:Math.floor(Date.now()/1000)+300}});
if(u.pathname==='/v1/auth/logout')return json(res,{});
if(u.pathname==='/v1/connector/clients')return json(res,{items:[{id:'a',label:'Mandant A'},{id:'b',label:'Mandant B'}],next_cursor:null});
const owner = req.headers.authorization;
const client = u.searchParams.get('client_id');
if (u.pathname === '/v1/chat/conversations' && req.method === 'POST') {
  const selection = JSON.parse(body);
  const general = selection.scope?.kind === 'general';
  const conversation = {
    id: randomUUID(), client_id: general ? null : selection.client_id, created_at: new Date().toISOString(),
    ...(general ? { scope: { kind: 'general' } } : {}),
  };
  conversations.set(conversation.id, { owner, conversation, turns: [] });
  return json(res, conversation);
}
if (u.pathname === '/v1/chat/conversations') {
  const clientId = u.searchParams.get('scope') === 'general' ? null : client;
  const items = [...conversations.values()]
    .filter(row => row.owner === owner && row.conversation.client_id === clientId)
    .map(row => row.conversation);
  return json(res, { items, next_cursor: null });
}
if (u.pathname === '/v1/chat/conversations/sources') {
  const { client_id, query, status } = JSON.parse(body);
  const items = [{ source_id: 's', title: 'Testdokument', status: 'ready' }];
  return json(res, { items: items.filter(item => client_id === 'a'
    && item.title.toLowerCase().includes(query.toLowerCase()) && (status === 'all' || status === item.status)),
    next_cursor: null,
  });
}
const thread = conversations.get(u.pathname.split('/')[4]);
if (u.pathname.endsWith('/history')) return json(res, { items: thread?.turns ?? [], next_cursor: null });
if (u.pathname.endsWith('/turns')) {
  const { question } = JSON.parse(body);
  const invoice = /RE-2026-0042/iu.test(question);
  const email = question === 'What does the email say about Berlin?';
  const docx = question.includes('Word document') ? (question.includes('(attachment)') ? 'attachment' : 'drive') : null;
  const russian = question === 'Когда встреча?';
  const general = thread?.conversation.scope?.kind === 'general';
  const answered = thread?.conversation.client_id !== 'b' && !russian;
  const answer = {
    id: thread?.turns.length ? randomUUID() : 't',
    created_at: new Date().toISOString(),
    status: answered ? 'answered' : 'no_evidence',
    question,
    answer: russian ? 'Не найдено достаточных подтверждений.' : !answered ? null
      : email ? 'The email says:\n\nDie Besprechung in Berlin ist am Montag.'
      : docx ? 'The document says:\n\nDas Seminar in Berlin beginnt am Montag.'
      : invoice ? 'Der Rechnungsbetrag beträgt 1.249,50 EUR.' : 'Testantwort',
    citations: email ? [{ id: 'email', title: 'Termin', page: null, source_kind: 'email' }]
      : docx ? [{ id: docx, title: 'Agenda.docx', page: null, source_kind: 'docx' }]
      : answered ? [{ id: invoice ? 'invoice' : 'x', title: invoice ? 'Rechnung RE-2026-0042.pdf' : 'Testdokument', page: invoice ? 2 : 1 }] : [],
  };
  if (general && answer.citations.length === 1) {
    answer.citations[0].client = generalClients[docx ? 'docx' : 'pdf'];
  }
  thread?.turns.push(answer);
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  res.write('event: status\ndata: {"phase":"retrieving"}\n\n');
  res.end('event: answer\ndata: ' + JSON.stringify(answer) + '\n\n');
  return;
}
const citationKind = u.pathname.split('/').at(-2);
if (u.pathname.endsWith('/original') && Object.hasOwn(downloadPaths, citationKind)) {
  if (!owner || owner !== thread?.owner) {
    res.writeHead(403);
    res.end();
    return;
  }
  const query = thread.conversation.scope?.kind === 'general' ? generalDownloadQuery : downloadQuery;
  return json(res, { source_kind: 'docx', locator: downloadPaths[citationKind] + '?' + query });
}
const downloadKind = Object.keys(downloadPaths).find(kind => downloadPaths[kind] === u.pathname);
if (downloadKind) {
  const hasSession = req.headers.cookie?.split('; ').includes('erp_fixture=active');
  if (!hasSession || owner || ![downloadQuery, generalDownloadQuery.toString()].includes(u.searchParams.toString())) {
    res.writeHead(403);
    res.end();
    return;
  }
  res.writeHead(200, {
    'Content-Disposition': 'attachment; filename="Agenda.docx"',
    'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
  res.end(Buffer.from('Download fixture bytes'));
  return;
}
if (u.pathname.endsWith('/email/original')) {
  if (owner !== thread?.owner) {
    res.writeHead(403);
    res.end();
    return;
  }
  return json(res, { source_kind: 'email', locator: '/desk/communication/Test%20%23%3F' });
}
if (u.pathname === '/desk/communication/Test%20%23%3F') {
  const hasSession = req.headers.cookie?.split('; ').includes('erp_fixture=active');
  res.writeHead(hasSession ? 200 : 403, { 'Content-Type': 'text/plain' });
  res.end(hasSession ? 'ERP communication fixture' : 'ERP session missing');
  return;
}
if (u.pathname.endsWith('/pdf')) {
  res.writeHead(200, { 'Content-Type': 'application/pdf' });
  res.end(u.pathname.endsWith('/invoice/pdf') ? examplePdf : pdf());
  return;
}
res.writeHead(404);res.end();});}).listen(port,'127.0.0.1');
