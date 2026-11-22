import { defineConfig } from 'vite';
const target = process.env.KANZLEI_AI_PROXY_TARGET;
if (!target) throw new Error('KANZLEI_AI_PROXY_TARGET must be an explicit loopback HTTP origin');
const url = new URL(target);
if (url.protocol !== 'http:' || !['127.0.0.1','localhost','[::1]'].includes(url.hostname) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('Proxy target must be a loopback HTTP origin');
export default defineConfig(({ command, isPreview }) => ({
  base: command === 'build' || isPreview ? '/chat/' : '/',
  server: { proxy: { '/v1': target } },
  preview: { proxy: { '/chat/v1/': { target, rewrite: path => path.slice('/chat'.length) } } },
}));
