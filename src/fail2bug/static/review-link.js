(async () => {
  const host = document.querySelector('[data-review-batch]');
  if (!host) return;
  const token = new URLSearchParams(location.hash.slice(1)).get('access') || '';
  history.replaceState(null, '', location.pathname);
  try {
    if (!token) throw new Error('请回到飞书机器人发送“审核链接”，再点击新链接。');
    const response = await fetch(`/api/review-access/${encodeURIComponent(host.dataset.reviewBatch)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '链接验证失败，请重新获取审核链接。');
    location.replace(result.path);
  } catch (error) {
    document.querySelector('#review-entry-title').textContent = '暂时无法打开';
    document.querySelector('#review-entry-message').textContent = error.message || '无法连接服务，请确认电脑服务已启动，手机连接同一局域网。';
  }
})();
