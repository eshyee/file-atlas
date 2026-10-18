const fragment = window.location.hash.slice(1);
if (fragment) {
  sessionStorage.setItem('fileatlas-session', fragment);
  window.history.replaceState(null, '', window.location.pathname + window.location.search);
}
const token = sessionStorage.getItem('fileatlas-session') || '';

export async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { 'X-FileAtlas-Token': token, ...options.headers },
      cache: 'no-store',
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('无法连接归序。请确认本地程序仍在运行，然后重新打开界面。');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.error || `请求未完成（${response.status}），请重试。`);
  }
  return options.blob ? response.blob() : response.json();
}

export const post = (path, data, options = {}) => api(path, {
  ...options, method: 'POST', headers: { 'Content-Type': 'application/json', ...options.headers }, body: JSON.stringify(data),
});

export function getPlan(id, kind = 'items', offset = 0, limit = 100, signal) {
  const params = new URLSearchParams({ id, kind, offset: String(offset), limit: String(limit) });
  return api(`/api/plan?${params}`, { signal });
}

export async function exportPlan(id) {
  const blob = await api(`/api/export?${new URLSearchParams({ id })}`, { blob: true });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = '归序-整理预览.csv';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const number = value => new Intl.NumberFormat('zh-CN').format(Number(value) || 0);
export function bytes(value) {
  let amount = Number(value) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toLocaleString('zh-CN', { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}
export const basename = path => String(path || '').split(/[\\/]/).filter(Boolean).pop() || path;
export const modeLabel = mode => ({ classify: '按类型分类', dedup: '查找重复文件', merge: '跨盘归并' })[mode] || mode;
export const actionLabel = action => ({ copy: '复制', move: '移动', quarantine: '移入隔离区' })[action] || action;
