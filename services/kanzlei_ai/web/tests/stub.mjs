import http from 'node:http';
const port=Number(process.env.STUB_PORT||19991);
const json=(res,data)=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
function pdf(){const objects=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>','<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>','<< /Length 42 >>\nstream\nBT /F1 12 Tf 20 100 Td (Synthetic) Tj ET\nendstream','<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];let s='%PDF-1.4\n';const offsets=[0];for(let i=0;i<objects.length;i++){offsets.push(Buffer.byteLength(s));s+=`${i+1} 0 obj\n${objects[i]}\nendobj\n`;}const x=Buffer.byteLength(s);s+=`xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(n=>String(n).padStart(10,'0')+' 00000 n ').join('\n')}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${x}\n%%EOF`;return s;}
http.createServer((req,res)=>{const u=new URL(req.url,`http://127.0.0.1:${port}`);let body='';req.on('data',x=>body+=x);req.on('end',()=>{if(u.pathname==='/kanzlei-ai-connect'){res.writeHead(200,{'Content-Type':'text/html'});res.end(`<script>opener.postMessage({type:'kanzlei-ai:ticket',state:new URL(location.href).searchParams.get('state'),ticket:'fixture'},'http://127.0.0.1:19992')</script>`);return;}
if(u.pathname==='/v1/auth/browser/config')return json(res,{version:1,erp_origin:`http://127.0.0.1:${port}`});
if(u.pathname==='/v1/auth/browser/exchange')return json(res,{access_token:'fixture',token_type:'bearer',identity:{expires_at:Math.floor(Date.now()/1000)+300}});
if(u.pathname==='/v1/auth/logout')return json(res,{});
if(u.pathname==='/v1/connector/clients')return json(res,{items:[{id:'a',label:'Mandant A'},{id:'b',label:'Mandant B'}],next_cursor:null});
if(u.pathname==='/v1/chat/conversations'&&req.method==='POST')return json(res,{id:'c',client_id:JSON.parse(body).client_id,created_at:'2026-01-01T00:00:00Z'});
if(u.pathname==='/v1/chat/conversations')return json(res,{items:[],next_cursor:null});
if(u.pathname.endsWith('/history'))return json(res,{items:[],next_cursor:null});
if(u.pathname.endsWith('/sources'))return json(res,{items:[{source_id:'s',title:'Testdokument',status:'ready'}],next_cursor:null});
if(u.pathname.endsWith('/turns')){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('event: status\ndata: {"phase":"retrieving"}\n\n');res.end('event: answer\ndata: '+JSON.stringify({id:'t',created_at:'2026-01-01T00:00:00Z',status:'answered',question:'Testfrage',answer:'Testantwort',citations:[{id:'x',title:'Testdokument',page:1}]})+'\n\n');return;}
if(u.pathname.endsWith('/pdf')){res.writeHead(200,{'Content-Type':'application/pdf'});res.end(pdf());return;}
res.writeHead(404);res.end();});}).listen(port,'127.0.0.1');
