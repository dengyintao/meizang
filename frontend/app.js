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

function safeImageUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? escapeHtml(url.href) : '';
  } catch (_error) { return ''; }
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
  grid.innerHTML = assets.map(asset => {
    const metadata = asset.metadata || {};
    const poster = safeImageUrl(metadata.poster_url);
    const details = [asset.year, metadata.genres?.slice(0, 2).join(' / ')].filter(Boolean).join(' · ');
    return `<article class="asset-card"><div class="asset-preview">${poster ? `<img src="${poster}" loading="lazy" alt="">` : icons[asset.media_type] || '◇'}${metadata.provider ? `<span class="provider-badge">${escapeHtml(metadata.provider.toUpperCase())}</span>` : ''}</div><div class="asset-card-body"><strong title="${escapeHtml(asset.filename)}">${escapeHtml(asset.title || asset.filename)}</strong><p title="${escapeHtml(metadata.plot || asset.relative_path)}">${escapeHtml(details || asset.relative_path)}</p><div class="asset-meta"><span>${escapeHtml(asset.extension.replace('.', '').toUpperCase())}${asset.width ? ` · ${asset.width}×${asset.height}` : ''}</span><span>${formatBytes(asset.size)}</span></div></div></article>`;
  }).join('');
}

async function loadDuplicates() {
  const list = $('#duplicate-list');
  const groups = await api('/duplicates');
  const total = groups.reduce((sum, group) => sum + group.reclaimable_bytes, 0);
  $('#reclaimable').textContent = `${formatBytes(total)} 可释放`;
  list.innerHTML = groups.length ? groups.map((group, index) => `
    <article class="duplicate-group"><div class="duplicate-head"><strong>重复组 #${index + 1} · ${group.count} 个完全相同文件</strong><span>可释放 ${formatBytes(group.reclaimable_bytes)}</span></div>${group.files.map(file => `<div class="duplicate-file">${escapeHtml(file.path)}</div>`).join('')}</article>`).join('') : '<div class="panel empty-state">暂未发现完全重复的媒体文件。</div>';
}

async function startScan(rootId, button, forceMetadata = false) {
  const original = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '扫描中…'; }
  try {
    const job = await api('/scans', { method: 'POST', body: JSON.stringify({ root_id: rootId, force_metadata: forceMetadata }) });
    await pollScan(job.id);
    toast('媒体目录扫描完成');
    await Promise.all([loadStats(), loadRoots()]);
    if (location.hash === '#library') await loadAssets();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

async function loadProviderSettings() {
  const settings = await api('/settings/providers');
  renderProviderSettings(settings);
}

function renderProviderSettings(settings) {
  $('#tmdb-enabled').checked = settings.tmdb.enabled;
  $('#tmdb-language').value = settings.tmdb.language;
  $('#tmdb-status').textContent = settings.tmdb.configured ? (settings.tmdb.enabled ? '已启用' : '已配置 · 未启用') : '未配置';
  $('#tmdb-status').classList.toggle('active', settings.tmdb.enabled && settings.tmdb.configured);
  $('#proxy-enabled').checked = settings.proxy.enabled;
  $('#proxy-status').textContent = settings.proxy.configured ? (settings.proxy.enabled ? '使用中' : '已配置 · 未启用') : '未配置';
  $('#proxy-status').classList.toggle('active', settings.proxy.enabled && settings.proxy.configured);
}

function providerPayload() {
  return {
    tmdb: {
      enabled: $('#tmdb-enabled').checked,
      token: $('#tmdb-token').value.trim(),
      language: $('#tmdb-language').value,
    },
    proxy: {
      enabled: $('#proxy-enabled').checked,
      url: $('#proxy-url').value.trim(),
    },
  };
}

$('#provider-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter || $('#provider-form button[type="submit"]');
  submit.disabled = true;
  try {
    const settings = await api('/settings/providers', {
      method: 'PUT',
      body: JSON.stringify(providerPayload()),
    });
    $('#tmdb-token').value = '';
    $('#proxy-url').value = '';
    renderProviderSettings(settings);
    toast('Provider 设置已保存');
  } catch (error) { toast(error.message, true); }
  finally { submit.disabled = false; }
});

$('#test-provider-connection').addEventListener('click', async event => {
  const button = event.currentTarget;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '正在测试…';
  try {
    const result = await api('/settings/providers/test', {
      method: 'POST', body: JSON.stringify(providerPayload()),
    });
    toast(result.message || 'TMDB 连接成功');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = original; }
});

$('#clear-proxy').addEventListener('click', async () => {
  if (!window.confirm('确定清除媒藏中保存的代理地址吗？')) return;
  const payload = providerPayload();
  payload.proxy = { enabled: false, clear_url: true };
  try {
    const settings = await api('/settings/providers', { method: 'PUT', body: JSON.stringify(payload) });
    $('#proxy-url').value = '';
    renderProviderSettings(settings);
    toast('已清除代理设置');
  } catch (error) { toast(error.message, true); }
});

$('#rescrape-videos').addEventListener('click', async event => {
  const button = event.currentTarget;
  if (!state.roots.length) {
    toast('请先添加媒体目录', true);
    return;
  }
  button.disabled = true;
  button.textContent = '重新刮削中…';
  try {
    for (const root of state.roots) {
      if (!await startScan(root.id, null, true)) throw new Error('部分目录重新刮削失败');
    }
    toast('全部视频元数据已重新刮削');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = '重新刮削全部视频'; }
});

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
  sessionStorage.removeItem('meizang-picker-target');
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
  const pickerTarget = sessionStorage.getItem('meizang-picker-target');
  sessionStorage.removeItem('meizang-picker-target');
  const paths = result?.path || result?.data || [];
  if (['qb-library-root', 'qb-local-prefix'].includes(pickerTarget)) {
    const path = Array.isArray(paths) ? paths[0] : paths;
    if (path) $(`#${pickerTarget}`).value = path;
    return;
  }
  addSelectedRoots(paths).catch(error => toast(error.message, true));
});

function qbPayload() {
  const payload = {};
  for (const key of ['url', 'username', 'password', 'category', 'library_root', 'remote_prefix', 'local_prefix', 'poll_seconds']) {
    payload[key] = $(`#qb-${key.replaceAll('_', '-')}`).value;
  }
  payload.enabled = $('#qb-enabled').checked;
  payload.auto_cleanup = $('#qb-auto-cleanup').checked;
  return payload;
}

async function loadQBSettings() {
  const data = await api('/settings/qbittorrent');
  for (const key of ['url', 'username', 'category', 'library_root', 'remote_prefix', 'local_prefix', 'poll_seconds']) {
    $(`#qb-${key.replaceAll('_', '-')}`).value = data[key];
  }
  $('#qb-enabled').checked = data.enabled;
  $('#qb-auto-cleanup').checked = data.auto_cleanup;
  $('#qb-password').placeholder = data.password_configured ? '已保存，留空保持' : '填写 WebUI 密码';
  $('#qb-status').textContent = data.enabled ? '已启用' : '未启用';
  $('#qb-links-active').textContent = data.links.active;
  $('#qb-links-conflict').textContent = data.links.conflict;
  $('#qb-last-sync').textContent = data.last_sync_at ? `${data.last_sync_at} · ${data.last_sync_message}` : '尚未同步';
  const links = await api('/qbittorrent/links');
  $('#qb-link-list').innerHTML = links.length ? links.map(link => `<p style="overflow-wrap:anywhere">${escapeHtml(link.source_path)} → ${escapeHtml(link.library_path)}<br>${escapeHtml(link.status)} · ${escapeHtml(link.message)}</p>`).join('') : '<p>尚无整理记录。</p>';
}

$('#qb-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api('/settings/qbittorrent', {method: 'PUT', body: JSON.stringify(qbPayload())});
    $('#qb-password').value = '';
    await loadQBSettings();
    toast('qB 设置已保存');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

for (const action of ['test', 'sync']) {
  $(`#qb-${action}`).addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/qbittorrent/${action}`, {method: 'POST', body: JSON.stringify(action === 'test' ? qbPayload() : {})});
      if (action === 'test') toast(`连接成功 · ${result.version} · ${result.torrents} 个任务`);
      else {
        await loadQBSettings();
        toast(`整理 ${result.organized.created} 个文件，失败 ${result.organized.failed} 个，清理 ${result.cleanup.removed} 个链接`, result.organized.failed > 0);
      }
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
}

$$('.path-picker').forEach(button => button.addEventListener('click', async () => {
  const target = button.dataset.target;
  try {
    if (isLocalDevelopment) {
      const paths = await api('/roots');
      const picker = document.createElement('dialog');
      picker.innerHTML = `<h3>选择已添加的目录</h3><p>也可关闭后直接填写路径。</p>${paths.map(root => `<p><button class="button" data-path="${escapeHtml(root.path)}">${escapeHtml(root.path)}</button></p>`).join('')}<button class="button" data-close>关闭</button>`;
      document.body.append(picker);
      picker.addEventListener('click', event => {
        if (event.target.dataset.path) { $(`#${target}`).value = event.target.dataset.path; picker.close(); }
        if (event.target.hasAttribute('data-close')) picker.close();
      });
      picker.addEventListener('close', () => picker.remove());
      picker.showModal();
      return;
    }
    await trimSdk.ready();
    if (!trimSdk.isStandaloneWeb) {
      const result = await trimSdk.pickSharedFile({title:'选择整理目录', okText:'授权并选择', sidebarGroup:['myFiles','otherShare','external']});
      if (result?.code !== 0) throw new Error(result?.msg || '选择已取消');
      if (result.data?.length) $(`#${target}`).value = result.data[0];
    } else {
      sessionStorage.setItem('meizang-picker-target', target);
      const authState = crypto.randomUUID();
      sessionStorage.setItem('meizang-auth-state', authState);
      await trimSdk.openAppAuth('pickSharedFile', {appName:'meizang', redirectUri:`${location.origin}${PREFIX}/callback.html`, state:authState}, {target:'_blank'});
    }
  } catch (error) { toast(error.message, true); }
}));

Promise.all([loadHealth(), loadStats(), loadRoots(), loadProviderSettings(), loadQBSettings()]).catch(error => toast(error.message, true));
route();
