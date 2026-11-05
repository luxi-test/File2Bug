(() => {
  const root = document.documentElement;
  let theme = 'light';
  try {
    const saved = localStorage.getItem('f2b-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (_) {}
  function apply() {
    root.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]').content = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === 'light' ? '#f5f6f8' : '#1c1c1e';
    const button = document.getElementById('theme-toggle');
    if (button) {
      const label = theme === 'light' ? '深色界面' : '白色界面';
      button.setAttribute('aria-label', '切换' + label);
      button.setAttribute('aria-pressed', String(theme === 'dark'));
      document.getElementById('theme-label').textContent = label;
    }
  }
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      theme = theme === 'light' ? 'dark' : 'light';
      try { localStorage.setItem('f2b-theme', theme); } catch (_) {}
      apply();
    });
  });
})();
