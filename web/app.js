/* 本机界面只消费脱敏展示模型；令牌不进入 DOM、日志或浏览器持久存储。 */
'use strict';
const $ = id => document.getElementById(id);
const incomingToken = new URLSearchParams(location.hash.slice(1)).get('token');
if (incomingToken) {
  sessionStorage.setItem('viewer-session', incomingToken);
  history.replaceState(null, '', '/');
}
const sessionToken = sessionStorage.getItem('viewer-session') || '';
let accounts = [], busy = false, editId = null, removeId = null, loginTimer = null;
// 重绘快照时保留用户主动展开的账户详情。
const expandedAccounts = new Set();

// 使用文本节点渲染上游名称、备注与错误，不解释任何上游 HTML。
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function message(text, error = false) {
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
  $('message').hidden = !text;
}
async function api(path, payload) {
  const options = {headers: {'X-Viewer-Token': sessionToken}, cache: 'no-store'};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '本机请求失败。');
  return result;
}
function date(value) {
  if (!value) return '未知';
  return new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
}
function age(value) {
  if (!value) return '尚未查询';
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - value) / 60));
  return minutes < 1 ? '刚刚' : minutes < 60 ? `${minutes} 分钟前` : date(value);
}
function stale(account) {
  return Boolean(account.error || !account.quota_at || Date.now() / 1000 - account.quota_at > 300 ||
    account.quota?.windows.some(w => w.reset_at && w.reset_at <= Date.now() / 1000));
}
function score(account, kind) {
  const now = Date.now() / 1000;
  if (kind === 'card') {
    if (account.credits_error || !account.credits_at || now - account.credits_at > 300) return Infinity;
    return Math.min(...(account.credits?.details || []).filter(c => c.usable && c.expires_at > now).map(c => c.expires_at));
  }
  if (stale(account)) return Infinity;
  const windows = (account.quota?.windows || []).filter(w => w.group === '通用额度');
  if (kind === 'remaining') {
    const values = windows.filter(w => w.remaining !== null).map(w => w.remaining);
    return values.length ? -Math.min(...values) : Infinity;
  }
  return Math.min(...windows.filter(w => w.reset_at && w.reset_at > now).map(w => w.reset_at));
}
function setBusy(value) {
  busy = value;
  for (const button of document.querySelectorAll('.toolbar button,.tools button')) button.disabled = value;
}
function action(text, handler) {
  const button = el('button', text);
  button.disabled = busy;
  button.addEventListener('click', handler);
  return button;
}
// 核心时间拆分为日期与时分；完整年份及本地时间保留在标题提示中。
function timeMetric(title, timestamp, emptyText = '未知') {
  const box = el('div', null, 'metric');
  box.append(el('div', title, 'metric-label'));
  if (timestamp) {
    const value = new Date(timestamp * 1000);
    const dateText = `${value.getMonth() + 1} 月 ${value.getDate()} 日`;
    const line = el('div', dateText, 'metric-date');
    line.title = date(timestamp);
    box.append(line, el('div', value.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', hour12:false}), 'metric-time'));
    if (timestamp <= Date.now()/1000) box.append(el('div', '已到时间，请刷新确认', 'metric-hint warning'));
    else if (new Date().getFullYear() !== value.getFullYear()) box.append(el('div', `${value.getFullYear()} 年`, 'metric-hint'));
  } else box.append(el('div', emptyText, 'metric-empty'));
  return box;
}
// 仅突出通用窗口，周恢复时间必须对应真实的 604800 秒窗口。
function mainMetrics(account) {
  const grid = el('div', null, 'focus-grid');
  const general = (account.quota?.windows || []).filter(w => w.group === '通用额度');
  const quota = el('div', null, 'metric quota-metric');
  quota.append(el('div', '通用剩余额度', 'metric-label'));
  for (const window of general) {
    const value = el('div', window.remaining === null ? '未知' : `${Number(window.remaining.toFixed(1))}%`, 'metric-value');
    value.append(el('small', window.window));
    quota.append(value);
    if (window.remaining !== null) {
      const meter = el('progress'); meter.max = 100; meter.value = window.remaining;
      meter.setAttribute('aria-label', `通用额度 ${window.window} 剩余`);
      if (window.remaining <= 10) meter.className = 'low';
      quota.append(meter);
    }
    if (window.allowed === false || window.limit_reached === true) quota.append(el('div', '当前受限', 'metric-hint warning'));
  }
  if (!general.length) quota.append(el('div', '尚未获取', 'metric-empty'));
  const weekly = general.find(w => w.seconds === 604800);
  const cards = account.credits;
  const count = el('div', null, 'metric');
  count.append(el('div', '可用重置卡', 'metric-label'));
  const amount = el('div', cards?.available == null ? '未知' : String(cards.available), 'metric-value');
  if (cards?.available != null) amount.append(el('small', '张'));
  count.append(amount);
  const available = (cards?.details || []).filter(c => c.status === 'available');
  const dated = available.filter(c => c.expires_at).sort((a,b) => a.expires_at-b.expires_at);
  const expiry = timeMetric(available.length > 1 ? '重置卡最近到期' : '重置卡到期', dated[0]?.expires_at,
    cards?.available === 0 ? '暂无可用卡' : '到期时间未知');
  if (available.some(c => !c.expires_at)) expiry.append(el('div', '部分卡片到期时间未知', 'metric-hint warning'));
  if (dated.length > 1) expiry.append(el('div', `共 ${dated.length} 张有到期明细 · 展开查看`, 'metric-hint'));
  grid.append(quota, timeMetric('周额度恢复', weekly?.reset_at, '未返回周恢复时间'), count, expiry);
  return grid;
}
// 次要额度、卡片全部明细和凭据元数据只在用户主动展开后显示。
function accountDetails(account) {
  const details = el('details', null, 'account-details');
  details.open = expandedAccounts.has(account.id);
  details.addEventListener('toggle', () => {
    if (!details.isConnected) return;
    if (details.open) expandedAccounts.add(account.id); else expandedAccounts.delete(account.id);
  });
  details.append(el('summary', '更多信息'));
  const content = el('div', null, 'details-content');
  const extras = (account.quota?.windows || []).filter(w => w.group !== '通用额度');
  if (extras.length) {
    content.append(el('h3', '其他额度'));
    const list = el('div', null, 'extra-windows');
    for (const window of extras) {
      const item = el('div', null, 'extra-window');
      item.append(el('span', `${window.group} · ${window.window}`),
        el('strong', window.remaining === null ? '剩余未知' : `剩余 ${Number(window.remaining.toFixed(1))}%`),
        el('small', `恢复：${date(window.reset_at)}`));
      list.append(item);
    }
    content.append(list);
  }
  if (account.credits?.details_present) {
    content.append(el('h3', '重置卡明细'));
    const list = el('ul', null, 'credit-list');
    const cards = account.credits.details.filter(c => c.status === 'available').sort((a,b) => (a.expires_at || Infinity)-(b.expires_at || Infinity));
    for (const card of cards) list.append(el('li', `到期：${date(card.expires_at)}`));
    if (!cards.length) list.append(el('li', '没有可用卡明细'));
    content.append(list);
  }
  if (account.credits?.applicable != null) content.append(el('p', `当前适用重置卡：${account.credits.applicable} 张`, 'meta'));
  const info = el('div', null, 'detail-account');
  info.append(el('p', `${account.owned ? '独立登录' : '导入副本'} · ${account.email || '邮箱未提供'}`, 'meta'),
    el('p', `${account.owned ? '登录令牌到期' : '副本到期，需重新导入'}：${date(account.expires_at)}`, 'meta'),
    el('p', `重置卡查询：${age(account.credits_at)}`, 'meta'));
  if (account.notes) info.append(el('p', account.notes, 'note'));
  const tools = el('div', null, 'tools');
  tools.append(action('编辑账户', () => edit(account)), action('移除账户', () => remove(account)));
  info.append(tools); content.append(info); details.append(content);
  return details;
}
function render() {
  $('total').textContent = accounts.length;
  $('fresh').textContent = accounts.filter(a => !stale(a)).length;
  $('attention').textContent = accounts.filter(a => a.error || a.credits_error ||
    a.quota?.windows.some(w => w.remaining !== null && w.remaining <= 10) ||
    a.credits?.details.some(c => c.status === 'available' && c.expires_at && c.expires_at - Date.now()/1000 < 86400)).length;
  const search = $('search').value.trim().toLowerCase();
  const list = accounts.filter(a => `${a.name || ''} ${a.label} ${a.email} ${a.notes}`.toLowerCase().includes(search));
  const kind = $('sort').value;
  list.sort((a,b) => kind === 'name' ? (a.name || a.label).localeCompare(b.name || b.label, 'zh-CN') : (score(a,kind) - score(b,kind) || (a.name || a.label).localeCompare(b.name || b.label)));
  const container = $('accounts');
  container.replaceChildren();
  $('empty').hidden = accounts.length !== 0;
  if (accounts.length && !list.length) container.append(el('p', '没有匹配的账户。', 'muted'));
  for (const account of list) {
    const card = el('article', null, 'card');
    const head = el('div', null, 'card-head');
    const identity = el('div', null, 'identity');
    // 用户名称作为主标题，邮箱同排弱化；没有名称时保留原来的账户标题。
    identity.append(el('h2', account.name || account.label));
    if (account.email && account.email !== (account.name || account.label)) identity.append(el('span', account.email, 'account-email'));
    identity.append(el('span', account.quota?.plan || account.plan_hint || '套餐未知', 'tag'));
    const tools = el('div', null, 'tools');
    tools.append(el('span', `更新于 ${age(account.quota_at)}${stale(account) ? ' · 待刷新' : ''}`, 'updated'),
      action('刷新', () => refreshAccounts([account])));
    head.append(identity, tools); card.append(head, mainMetrics(account));
    // 刷新失败必须保持可见，避免折叠详情后把旧快照误认为当前数据。
    if (account.error) card.append(el('div', `额度未更新：${account.error}${account.quota ? ' 当前显示上次快照。' : ''}`, 'card-alert error'));
    if (account.credits_error) card.append(el('div', `重置卡未更新：${account.credits_error}`, 'card-alert error'));
    if (account.quota?.warnings?.length) card.append(el('div', account.quota.warnings.join('；'), 'card-alert warning'));
    card.append(accountDetails(account)); container.append(card);
  }
}
async function load() {
  const result = await api('/api/accounts'); accounts = result.accounts; render();
  const state = result.login;
  const active = state.status === 'waiting' || state.status === 'exchanging';
  $('login-status').hidden = !active;
  $('login-status').querySelector('span').textContent = state.message;
  if (!active && loginTimer) {
    clearInterval(loginTimer); loginTimer = null;
    message(state.message, state.status === 'error');
  }
}
async function refreshAccounts(selected) {
  if (busy) return;
  setBusy(true);
  try {
    for (let i=0; i<selected.length; i++) {
      message(`正在查询 ${i+1}/${selected.length}：${selected[i].label}…`);
      await api('/api/refresh', {id:selected[i].id}); await load();
    }
    const bad = accounts.filter(a => selected.some(s => s.id === a.id) && (a.error || a.credits_error));
    message(bad.length ? `查询结束，${bad.length} 个账户有未更新的信息，请查看账户提示。` : '查询完成。', bad.length > 0);
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
}
function edit(account) {
  editId = account.id; $('edit-label').value = account.label; $('edit-notes').value = account.notes || ''; $('edit-dialog').showModal();
}
function remove(account) {
  removeId = account.id; $('remove-label').textContent = account.label; $('remove-dialog').showModal();
}
$('search').addEventListener('input', render); $('sort').addEventListener('change', render);
$('refresh-all').addEventListener('click', () => refreshAccounts(accounts));
$('import').addEventListener('click', () => $('files').click());
$('files').addEventListener('change', async () => {
  setBusy(true);
  try {
    const files = [...$('files').files];
    if (!files.length) return;
    if (files.length > 50 || files.reduce((n,f) => n+f.size,0) > 3 * 1024 * 1024) throw new Error('每次最多 50 个文件，总大小不超过 3 MB。');
    const documents = await Promise.all(files.map(async file => ({content: await file.text()})));
    const result = await api('/api/import', {documents}); await load();
    message(`已导入 ${result.count} 个账户。点击刷新查询额度；副本过期后请重新导入。`);
  } catch (error) { message(error.message, true); }
  finally { $('files').value = ''; setBusy(false); }
});
$('login').addEventListener('click', async () => {
  const popup = window.open('about:blank', '_blank');
  if (popup) popup.opener = null;
  try {
    const result = await api('/api/login', {});
    $('login-link').href = result.url;
    if (popup) popup.location = result.url;
    message('请登录要添加的账户，登录完成后返回此页面。');
    if (loginTimer) clearInterval(loginTimer);
    loginTimer = setInterval(() => load().catch(error => {clearInterval(loginTimer); loginTimer=null; message(error.message,true);}), 1500);
    await load();
  } catch (error) { if (popup) popup.close(); message(error.message, true); }
});
$('cancel-login').addEventListener('click', async () => { try { await api('/api/login/cancel', {}); await load(); } catch(error) { message(error.message,true); } });
$('edit-cancel').addEventListener('click', () => $('edit-dialog').close());
$('edit-save').addEventListener('click', async () => {
  try { await api('/api/edit', {id:editId,label:$('edit-label').value,notes:$('edit-notes').value}); $('edit-dialog').close(); await load(); }
  catch(error) { $('edit-dialog').close(); message(error.message,true); }
});
$('remove-cancel').addEventListener('click', () => $('remove-dialog').close());
$('remove-confirm').addEventListener('click', async () => {
  try { await api('/api/remove', {id:removeId}); $('remove-dialog').close(); await load(); message('已移除本地记录。'); }
  catch(error) { $('remove-dialog').close(); message(error.message,true); }
});
// 仅更新快照年龄与提示，不在后台自动请求上游。
setInterval(render, 60000);
load().catch(error => message(error.message, true));
