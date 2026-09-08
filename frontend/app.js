import { TrimApp } from './vendor/trim-web-app.js';

const PREFIX = location.pathname.startsWith('/app/meizang') ? '/app/meizang' : '';
const state = { type: 'all', query: '', roots: [], stats: null, duplicateGroups: [] };
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

function assetFileUrl(asset) {
  return `${PREFIX}/api/assets/${asset.id}/file`;
}

function assetThumbnailUrl(asset) {
  const metadata = asset.metadata || {};
  const poster = safeImageUrl(metadata.poster_url);
  if (poster) return poster;
  if (asset.media_type === 'image' || asset.media_type === 'video') return `${PREFIX}/api/assets/${asset.id}/thumbnail`;
  return '';
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
    const poster = assetThumbnailUrl(asset);
    const details = [asset.year, metadata.genres?.slice(0, 2).join(' / ')].filter(Boolean).join(' · ');
    return `<article class="asset-card" tabindex="0" role="button" data-asset-id="${asset.id}"><div class="asset-preview">${poster ? `<img src="${poster}" loading="lazy" alt="" onerror="this.remove()">` : ''}<span class="preview-glyph">${icons[asset.media_type] || '◇'}</span>${metadata.provider ? `<span class="provider-badge">${escapeHtml(metadata.provider.toUpperCase())}</span>` : ''}</div><div class="asset-card-body"><strong title="${escapeHtml(asset.filename)}">${escapeHtml(asset.title || asset.filename)}</strong><p title="${escapeHtml(metadata.plot || asset.relative_path)}">${escapeHtml(details || asset.relative_path)}</p><div class="asset-meta"><span>${escapeHtml(asset.extension.replace('.', '').toUpperCase())}${asset.width ? ` · ${asset.width}×${asset.height}` : ''}</span><span>${formatBytes(asset.size)}</span></div></div></article>`;
  }).join('');
  const byId = new Map(assets.map(asset => [String(asset.id), asset]));
  $$('.asset-card').forEach(card => {
    const open = () => showAssetPreview(byId.get(card.dataset.assetId));
    card.addEventListener('click', open);
    card.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        open();
      }
    });
  });
}

function showAssetPreview(asset) {
  if (!asset) return;
  const previewDialog = $('#asset-preview-dialog');
  const stage = $('#asset-preview-stage');
  $('#asset-preview-title').textContent = asset.title || asset.filename;
  $('#asset-preview-path').textContent = asset.relative_path;
  const url = assetFileUrl(asset);
  if (asset.media_type === 'video') {
    stage.innerHTML = `<video controls preload="metadata" src="${url}"></video>`;
  } else if (asset.media_type === 'audio') {
    stage.innerHTML = `<div class="audio-preview">♫<audio controls preload="metadata" src="${url}"></audio></div>`;
  } else if (asset.media_type === 'image') {
    stage.innerHTML = `<img src="${url}" alt="">`;
  } else {
    window.open(url, '_blank');
    return;
  }
  previewDialog.showModal();
}

async function loadDuplicates() {
  const list = $('#duplicate-list');
  const groups = await api('/duplicates');
  state.duplicateGroups = groups;
  const total = groups.reduce((sum, group) => sum + group.reclaimable_bytes, 0);
  const removable = groups.reduce((sum, group) => sum + group.removable_count, 0);
  $('#reclaimable').textContent = `${formatBytes(total)} 可释放`;
  $('#delete-all-duplicates').disabled = removable === 0;
  list.innerHTML = groups.length ? groups.map((group, index) => `
    <article class="duplicate-group"><div class="duplicate-head"><strong>重复组 #${index + 1} · ${group.count} 个完全相同文件</strong><span>可释放 ${formatBytes(group.reclaimable_bytes)}</span></div>${group.files.map(file => `<div class="duplicate-file">${escapeHtml(file.path)}${file.protected ? '<b>qB 保护</b>' : ''}</div>`).join('')}</article>`).join('') : '<div class="panel empty-state">暂未发现完全重复的媒体文件。</div>';
}

$('#delete-all-duplicates').addEventListener('click', async event => {
  const groups = state.duplicateGroups;
  const bytes = groups.reduce((sum, group) => sum + group.reclaimable_bytes, 0);
  const count = groups.reduce((sum, group) => sum + group.removable_count, 0);
  if (!count) return;
  if (!window.confirm(`将永久删除 ${count} 个重复文件，预计释放 ${formatBytes(bytes)}。每组会保留至少一份，qB 保护文件不会删除。此操作无法撤销，是否继续？`)) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = '正在校验并删除…';
  try {
    const result = await api('/duplicates/delete', {method:'POST', body:JSON.stringify({expected_groups:groups.length, confirmation:'DELETE_DUPLICATES'})});
    const message = `已删除 ${result.deleted} 个文件，释放 ${formatBytes(result.released_bytes)}${result.skipped ? `，跳过 ${result.skipped} 个已变化或受保护文件` : ''}`;
    toast(message, result.skipped > 0);
    await Promise.all([loadDuplicates(), loadStats(), loadAssets()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = '一键删除重复项';
    button.disabled = state.duplicateGroups.reduce((sum, group) => sum + group.removable_count, 0) === 0;
  }
});

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
  let refreshTick = 0;
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 650));
    const job = await api(`/scans/${jobId}`);
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') throw new Error(job.error || '扫描失败');
    refreshTick += 1;
    if (refreshTick % 8 === 0) {
      await loadStats();
      if (location.hash === '#library') await loadAssets();
    }
  }
}

function route() {
  const name = location.hash.slice(1) || 'overview';
  const titles = { overview: '媒体概览', library: '媒体库', scraping: '影片刮削', duplicates: '重复文件', settings: '目录设置' };
  const target = titles[name] ? name : 'overview';
  $$('.view').forEach(view => view.classList.toggle('active', view.dataset.view === target));
  $$('.nav a').forEach(link => link.classList.toggle('active', link.dataset.route === target));
  $('#page-title').textContent = titles[target];
  if (target === 'library') loadAssets().catch(error => toast(error.message, true));
  if (target === 'duplicates') loadDuplicates().catch(error => toast(error.message, true));
  if (target === 'scraping') loadMDC().catch(error => toast(error.message, true));
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
$('#add-root-dialog .dialog-close').addEventListener('click', () => dialog.close());
$('.dialog-actions .cancel').addEventListener('click', () => dialog.close());
$('#asset-preview-dialog .dialog-close').addEventListener('click', () => $('#asset-preview-dialog').close());
$('#asset-preview-dialog').addEventListener('close', () => $('#asset-preview-stage').innerHTML = '');
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

const mdcSectionNames = {
  common:'运行模式', advenced_sleep:'批处理节奏', proxy:'MDC 独立代理', Name_Rule:'目录与文件命名',
  update:'更新检查', priority:'Provider 优先级', escape:'路径排除', debug_mode:'调试', translate:'翻译',
  trailer:'预告片', uncensored:'无码识别', media:'媒体与字幕', watermark:'封面水印', extrafanart:'剧照',
  storyline:'剧情简介', cc_convert:'繁简转换', javdb:'JavDB 线路', face:'人脸识别裁剪', jellyfin:'Jellyfin',
  actor_photo:'演员头像', direct:'直连模式'
};

function mdcField(section, key, value) {
  const id = `mdc-cfg-${section}-${key}`.replaceAll('_', '-');
  let control;
  if (key === 'main_mode') control = `<select id="${id}" data-mdc-section="${escapeHtml(section)}" data-mdc-key="${escapeHtml(key)}"><option value="1">1 · 刮削并整理</option><option value="2">2 · 仅整理</option><option value="3">3 · 原目录刮削</option></select>`;
  else if (key === 'link_mode') control = `<select id="${id}" data-mdc-section="${escapeHtml(section)}" data-mdc-key="${escapeHtml(key)}"><option value="0">0 · 移动</option><option value="1">1 · 软链接</option><option value="2">2 · 优先硬链接</option></select>`;
  else if (['switch','scan_hardlink','failed_move','auto_exit','translate_to_sc','del_empty_folder','ignore_failed_list','download_only_missing_images','jellyfin','actor_only_tag','anonymous_fill','image_naming_with_number','number_uppercase','uncensored_only','aways_imagecut','multi_part_fanart','download_for_kodi'].includes(key)) control = `<select id="${id}" data-mdc-section="${escapeHtml(section)}" data-mdc-key="${escapeHtml(key)}"><option value="0">关闭</option><option value="1">启用</option></select>`;
  else control = `<input id="${id}" data-mdc-section="${escapeHtml(section)}" data-mdc-key="${escapeHtml(key)}" value="${escapeHtml(value)}" ${section === 'translate' && key === 'key' ? 'type="password" autocomplete="off" placeholder="已配置时留空保持"' : ''}>`;
  return `<label><small>[${escapeHtml(section)}] ${escapeHtml(key)}</small>${control}</label>`;
}

function renderMDCConfig(data) {
  $('#mdc-engine-status').textContent = `MDC 引擎已就绪 · ${data.provider_count} 个 Provider`;
  $('#mdc-provider-count').textContent = `${data.provider_count} PROVIDERS`;
  $('#mdc-config-sections').innerHTML = Object.entries(data.sections).map(([section, values], index) => `<details class="mdc-config-section" ${index < 3 ? 'open' : ''}><summary>${escapeHtml(mdcSectionNames[section] || section)} · [${escapeHtml(section)}]</summary><div class="mdc-config-grid">${Object.entries(values).map(([key, value]) => mdcField(section, key, value)).join('')}</div></details>`).join('');
  $$('[data-mdc-section]').forEach(input => { if (input.tagName === 'SELECT') input.value = data.sections[input.dataset.mdcSection][input.dataset.mdcKey]; });
}

function mdcRunPayload() {
  return {
    kind:'scan', root_id:Number($('#mdc-root').value), mode:Number($('#mdc-mode').value),
    source:$('#mdc-source').value.trim(), regex:$('#mdc-regex').value.trim(),
    no_network:$('#mdc-no-network').checked, dry_run:$('#mdc-dry-run').checked,
  };
}

async function loadMDC() {
  const [settings, jobs, schedules] = await Promise.all([api('/settings/mdc'), api('/mdc/jobs'), api('/mdc/schedules')]);
  renderMDCConfig(settings);
  $('#mdc-root').innerHTML = state.roots.length ? state.roots.map(root => `<option value="${root.id}">${escapeHtml(root.label || root.path)} · ${escapeHtml(root.path)}</option>`).join('') : '<option value="">请先添加媒体目录</option>';
  $('#mdc-jobs').classList.toggle('empty-state', !jobs.length);
  $('#mdc-jobs').innerHTML = jobs.length ? jobs.map(job => `<article class="mdc-job"><div><strong>${escapeHtml(job.kind)} · ${escapeHtml(job.status)}</strong><p>${escapeHtml(job.created_at)} · ${escapeHtml(job.message || job.error)}</p></div><div class="mdc-job-actions"><button class="text-button mdc-show-log" data-job="${job.id}">日志</button>${['queued','running'].includes(job.status) ? `<button class="text-button mdc-cancel" data-job="${job.id}">取消</button>` : ''}</div></article>`).join('') : '暂无 MDC 任务';
  $$('.mdc-show-log').forEach(button => button.addEventListener('click', async () => { const job = await api(`/mdc/jobs/${button.dataset.job}`); $('#mdc-log').textContent = job.log_text || job.error || '暂无日志'; }));
  $$('.mdc-cancel').forEach(button => button.addEventListener('click', async () => { await api(`/mdc/jobs/${button.dataset.job}/cancel`, {method:'POST'}); await loadMDC(); }));
  $('#mdc-schedules').classList.toggle('empty-state', !schedules.length);
  $('#mdc-schedules').innerHTML = schedules.length ? schedules.map(item => `<article class="mdc-schedule-item"><strong>${escapeHtml(item.name)}</strong><button class="text-button mdc-delete-schedule" data-schedule="${item.id}">删除</button><p>每 ${item.interval_seconds} 秒 · ${item.enabled ? '已启用' : '已暂停'}</p></article>`).join('') : '暂无定时任务';
  $$('.mdc-delete-schedule').forEach(button => button.addEventListener('click', async () => { await api(`/mdc/schedules/${button.dataset.schedule}`, {method:'DELETE'}); await loadMDC(); }));
}

$('#mdc-config-form').addEventListener('submit', async event => {
  event.preventDefault();
  const sections = {};
  $$('[data-mdc-section]').forEach(input => { (sections[input.dataset.mdcSection] ||= {})[input.dataset.mdcKey] = input.value; });
  try { renderMDCConfig(await api('/settings/mdc', {method:'PUT', body:JSON.stringify({sections})})); toast('完整 MDC 配置已保存'); }
  catch (error) { toast(error.message, true); }
});

$('#mdc-run-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { const job = await api('/mdc/jobs', {method:'POST', body:JSON.stringify(mdcRunPayload())}); toast(`MDC 任务已启动：${job.id.slice(0,8)}`); await loadMDC(); }
  catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$('#mdc-search-button').addEventListener('click', async () => {
  const number = window.prompt('输入要测试的影片番号'); if (!number) return;
  try { await api('/mdc/jobs', {method:'POST', body:JSON.stringify({kind:'search', number, source:$('#mdc-source').value.trim()})}); toast('番号搜索任务已启动'); await loadMDC(); }
  catch (error) { toast(error.message, true); }
});

$('#mdc-schedule-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('/mdc/schedules', {method:'POST', body:JSON.stringify({name:$('#mdc-schedule-name').value.trim(), interval_seconds:Number($('#mdc-schedule-interval').value), payload:mdcRunPayload()})}); toast('定时任务已保存'); await loadMDC(); }
  catch (error) { toast(error.message, true); }
});

$('#mdc-refresh').addEventListener('click', () => loadMDC().catch(error => toast(error.message, true)));
$('#mdc-reset-config').addEventListener('click', async () => {
  if (!window.confirm('恢复 Movie_Data_Capture 源仓库中的完整默认配置？')) return;
  try { renderMDCConfig(await api('/settings/mdc/reset', {method:'POST'})); toast('已恢复 MDC 默认配置'); }
  catch (error) { toast(error.message, true); }
});

Promise.all([loadHealth(), loadStats(), loadRoots(), loadProviderSettings(), loadQBSettings()]).catch(error => toast(error.message, true));
route();
