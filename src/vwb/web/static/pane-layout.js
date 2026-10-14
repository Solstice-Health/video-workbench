export const COMPACT_WIDTH = 768;
export const DEFAULT_PANES = {navigator: 196, chat: 360};
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

export function panePreferences(value) {
  return Object.fromEntries(Object.entries(DEFAULT_PANES).map(([key, fallback]) =>
    [key, Number.isFinite(value?.[key]) && value[key] > 0 ? value[key] : fallback]));
}

export function paneLayout(viewport, preferences, navigatorOpen, chatShown) {
  const width = Math.max(COMPACT_WIDTH, viewport), padding = width >= 1536 ? 20 : 12;
  const navigatorMax = Math.min(480, width - 52 - 8 - padding - 200 - (chatShown ? 280 + 12 : 0));
  const navigator = clamp(preferences.navigator, 144, navigatorMax);
  const chatMax = Math.max(280, width - 52 - (navigatorOpen ? navigator + 8 : 0) - padding - 12 - 200);
  return {navigator, navigatorMin: 144, navigatorMax, chat: clamp(preferences.chat, 280, chatMax), chatMin: 280, chatMax};
}
