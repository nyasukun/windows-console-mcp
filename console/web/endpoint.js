export const base = '/api' + location.pathname.replace(/\/console\/$/, '');

export function socket(path) {
  const url = new URL(base + path, location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return new WebSocket(url);
}

export async function api(path, method = 'GET', data = {}) {
  const response = await fetch(base + path, method === 'GET' ? {} : {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '操作に失敗しました');
  return result;
}
