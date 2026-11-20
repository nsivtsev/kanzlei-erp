// Do not print test artifacts, fixture text, credentials, or local paths on failure.
import { spawnSync } from 'node:child_process';
const suite=process.argv[2];
if(!['unit','browser'].includes(suite)) process.exit(2);
const bin=suite==='unit'?'vitest':'playwright';
const args=suite==='unit'?['run','tests/protocol.test.ts']:['test'];
const result=spawnSync(process.execPath,[suite==='unit'?'node_modules/vitest/vitest.mjs':'node_modules/playwright/cli.js',...args],{stdio:'ignore',env:process.env});
console.log(`${suite} tests: ${result.status===0?'passed':'failed'}`);
process.exit(result.status===0?0:1);
