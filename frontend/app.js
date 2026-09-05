import { TrimApp } from './vendor/trim-web-app.js';

const PREFIX = location.pathname.startsWith('/app/meizang') ? '/app/meizang' : '';
const state = { type: 'all', query: '', roots: [], stats: null };
const trimSdk = new TrimApp();
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(`${PREFIX}/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function toast(message, error = false) {
  const element = $('#toast');
  element.textContent = message;
  element.className = error ? 'show error' : 'show';
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.className = '', 3200);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}

async function loadHealth() {
  try {
    const health = await api('/health');
    $('#version').textContent = `VERSION ${health.version}`;
  } catch (error) {
    $('#version').textContent = '服务连接失败';
  }
}

async function loadStats() {
  state.stats = await api('/stats');
  for (const type of ['image', 'video', 'audio']) $(`#stat-${type}`).textContent = state.stats.types[type].toLocaleString();
  $('#stat-duplicates').textContent = state.stats.duplicate_groups.toLocaleString();
  $('#stat-total').textContent = state.stats.total.toLocaleString();
  $('#stat-bytes').textContent = formatBytes(state.stats.bytes);
}

async function loadRoots() {
  state.roots = await api('/roots');
  const overview = $('#overview-roots');
  const settings = $('#settings-roots');
  if (!state.roots.length) {
    overview.innerHTML = '<div class="empty-state">还没有媒体目录，点击右上角开始建立你的媒体库。</div>';
    settings.innerHTML = '<div class="panel empty-state">还没有添加媒体目录。</div>';
    return;
  }
  overview.innerHTML = state.roots.slice(0, 4).map(root => `
    <div class="root-row"><span class="root-icon">▣</span><div><strong>${escapeHtml(root.label || root.path.split('/').pop())}</strong><small>${escapeHtml(root.path)}</small></div><span>${root.asset_count} 项</span></div>`).join('');
  settings.innerHTML = state.roots.map(root => `
    <article class="settings-root"><div><h3>${escapeHtml(root.label || root.path.split('/').pop())}</h3><p>${escapeHtml(root.path)} · ${root.asset_count} 个文件</p></div><div class="scan-info"><button class="button scan-button" data-root-id="${root.id}">立即扫描</button><span>${escapeHtml(root.last_scan_at || '尚未扫描')} · ${escapeHtml(root.last_scan_status)}</span></div></article>`).join('');
  $$('.scan-button').forEach(button => button.addEventListener('click', () => startScan(Number(button.dataset.rootId), button)));
}

async function loadAssets() {
  const grid = $('#asset-grid');
  grid.innerHTML = '<div class="empty-state">正在载入媒体库…</div>';
  const params = new URLSearchParams({ type: state.type, q: state.query, limit: '300' });
  const assets = await api(`/assets?${params}`);
  if (!assets.length) {
    grid.innerHTML = '<div class="panel empty-state">没有找到匹配的媒体文件。</div>';
    return;
  }
  const icons = { image: '▧', video: '▶', audio: '♫', other: '◇' };
  grid.innerHTML = assets.map(asset => `
    <article class="asset-card"><div class="asset-preview">${icons[asset.media_type] || '◇'}</div><div class="asset-card-body"><strong title="${escapeHtml(asset.filename)}">${escapeHtml(asset.title || asset.filename)}</strong><p title="${escapeHtml(asset.relative_path)}">${escapeHtml(asset.relative_path)}</p><div class="asset-meta"><span>${escapeHtml(asset.extension.replace('.', '').toUpperCase())}${asset.width ? ` · ${asset.width}×${asset.height}` : ''}</span><span>${formatBytes(asset.size)}</span></div></div></article>`).join('');
}

async function loadDuplicates() {
  const list = $('#duplicate-list');
  const groups = await api('/duplicates');
  const total = groups.reduce((sum, group) => sum + group.reclaimable_bytes, 0);
  $('#reclaimable').textContent = `${formatBytes(total)} 可释放`;
  list.innerHTML = groups.length ? groups.map((group, index) => `
    <article class="duplicate-group"><div class="duplicate-head"><strong>重复组 #${index + 1} · ${group.count} 个完全相同文件</strong><span>可释放 ${formatBytes(group.reclaimable_bytes)}</span></div>${group.files.map(file => `<div class="duplicate-file">${escapeHtml(file.path)}</div>`).join('')}</article>`).join('') : '<div class="panel empty-state">暂未发现完全重复的媒体文件。</div>';
}

async function startScan(rootId, button) {
  const original = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '扫描中…'; }
  try {
    const job = await api('/scans', { method: 'POST', body: JSON.stringify({ root_id: rootId }) });
    await pollScan(job.id);
    toast('媒体目录扫描完成');
    await Promise.all([loadStats(), loadRoots()]);
    if (location.hash === '#library') await loadAssets();
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

async function pollScan(jobId) {
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 650));
    const job = await api(`/scans/${jobId}`);
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw new Error(job.error || '扫描失败');
  }
}

function route() {
  const name = location.hash.slice(1) || 'overview';
  const titles = { overview: '媒体概览', library: '媒体库', duplicates: '重复文件', settings: '目录设置' };
  const target = titles[name] ? name : 'overview';
  $$('.view').forEach(view => view.classList.toggle('active', view.dataset.view === target));
  $$('.nav a').forEach(link => link.classList.toggle('active', link.dataset.route === target));
  $('#page-title').textContent = titles[target];
  if (target === 'library') loadAssets().catch(error => toast(error.message, true));
  if (target === 'duplicates') loadDuplicates().catch(error => toast(error.message, true));
}

const dialog = $('#add-root-dialog');
const isLocalDevelopment = ['127.0.0.1', 'localhost'].includes(location.hostname);

async function addSelectedRoots(paths) {
  const selected = [...new Set((paths || []).filter(Boolean))];
  if (!selected.length) return;
  let added = 0;
  for (const path of selected) {
    try {
      const label = path.replace(/\/$/, '').split('/').pop() || path;
      const root = await api('/roots', { method: 'POST', body: JSON.stringify({ path, label }) });
      added += 1;
      startScan(root.id).catch(error => toast(error.message, true));
    } catch (error) {
      if (!error.message.includes('已添加')) throw error;
    }
  }
  await loadRoots();
  toast(`已添加 ${added || selected.length} 个媒体目录，正在建立索引`);
}

async function chooseRoot() {
  const button = $('#open-add-root');
  if (isLocalDevelopment) {
    dialog.showModal();
    return;
  }
  button.disabled = true;
  try {
    await trimSdk.ready();
    if (!trimSdk.isStandaloneWeb) {
      const result = await trimSdk.pickSharedFile({
        title: '选择媒藏媒体目录',
        okText: '授权并添加',
        sidebarGroup: ['myFiles', 'otherShare', 'external', 'remote', 'favorites'],
      });
      if (result?.code !== 0) throw new Error(result?.msg || '目录选择已取消');
      await addSelectedRoots(result.data);
      return;
    }
    const authState = crypto.randomUUID();
    sessionStorage.setItem('meizang-auth-state', authState);
    await trimSdk.openAppAuth('pickSharedFile', {
      appName: 'meizang',
      sidebarGroup: ['myFiles', 'otherShare', 'external', 'remote', 'favorites'],
      redirectUri: `${location.origin}${PREFIX}/callback.html`,
      state: authState,
    }, { target: '_blank', features: 'width=750,height=630' });
  } catch (error) {
    toast(error.message || '无法打开飞牛目录选择器', true);
  } finally {
    button.disabled = false;
  }
}

$('#open-add-root').addEventListener('click', chooseRoot);
$('#open-manual-root').addEventListener('click', () => dialog.showModal());
$('.dialog-close').addEventListener('click', () => dialog.close());
$('.dialog-actions .cancel').addEventListener('click', () => dialog.close());
$('#add-root-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(event.target));
    const root = await api('/roots', { method: 'POST', body: JSON.stringify(values) });
    dialog.close(); event.target.reset();
    toast('目录已添加，开始建立索引');
    await loadRoots();
    await startScan(root.id);
  } catch (error) { toast(error.message, true); }
  finally { submit.disabled = false; }
});

$$('.type-tabs button').forEach(button => button.addEventListener('click', () => {
  $$('.type-tabs button').forEach(item => item.classList.remove('active'));
  button.classList.add('active'); state.type = button.dataset.type; loadAssets();
}));
let searchTimer;
$('#asset-search').addEventListener('input', event => {
  clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.query = event.target.value.trim(); loadAssets(); }, 250);
});
window.addEventListener('hashchange', route);
window.addEventListener('message', event => {
  if (event.origin !== location.origin || event.data?.type !== 'meizang:auth-result') return;
  const result = event.data.result;
  const expectedState = sessionStorage.getItem('meizang-auth-state');
  sessionStorage.removeItem('meizang-auth-state');
  if (result?.state !== expectedState || result?.status === 'error') {
    toast(result?.error || '目录授权未完成', true);
    return;
  }
  addSelectedRoots(result?.path || result?.data || []).catch(error => toast(error.message, true));
});

Promise.all([loadHealth(), loadStats(), loadRoots()]).catch(error => toast(error.message, true));
route();
