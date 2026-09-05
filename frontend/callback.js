import { TrimApp } from './vendor/trim-web-app.js';

const sdk = new TrimApp();
try {
  await sdk.ready();
  const result = sdk.parseAppAuthCallback(window.location.href);
  if (window.opener) window.opener.postMessage({ type: 'meizang:auth-result', result }, window.location.origin);
  window.close();
} catch (error) {
  document.body.textContent = `目录授权失败：${error.message}`;
}
