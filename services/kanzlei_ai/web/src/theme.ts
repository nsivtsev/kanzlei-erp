type Theme = 'light' | 'dark';

const preferred = matchMedia('(prefers-color-scheme: dark)');
const options = [['light', 'Hell'], ['dark', 'Dunkel']] as const;
const root = document.documentElement;

function activeTheme(): Theme {
  const choice = root.dataset.theme;
  return choice === 'light' || choice === 'dark' ? choice : preferred.matches ? 'dark' : 'light';
}

function syncThemeButtons() {
  for (const control of document.querySelectorAll<HTMLButtonElement>('.theme-switch button')) {
    control.setAttribute('aria-pressed', String(control.value === activeTheme()));
  }
}

preferred.addEventListener('change', syncThemeButtons);

export function themeSwitcher() {
  const group = document.createElement('div');
  group.className = 'theme-switch';
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', 'Farbschema');
  for (const [value, label] of options) {
    const control = document.createElement('button');
    control.type = 'button';
    control.value = value;
    control.textContent = label;
    control.dataset.focus = `theme-${value}`;
    control.setAttribute('aria-pressed', String(value === activeTheme()));
    control.onclick = () => {
      root.dataset.theme = value;
      try { localStorage.setItem('kanzlei-ai-theme', value); } catch {}
      syncThemeButtons();
    };
    group.append(control);
  }
  return group;
}
