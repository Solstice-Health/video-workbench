// A trigger must start a word; email addresses and ordinary slashes stay as text.
export function triggerAt(text, caret = text.length) {
  const before = text.slice(0, caret);
  const match = /(?:^|\s)(\/features(?:[ \t]+([^\n]{0,80}))?|@([^\s@]*))$/.exec(before);
  if (!match) return null;
  return {kind: match[1].startsWith('@') ? 'folder' : 'skill', query: (match[2] || match[3] || '').trim(),
    start: caret - match[1].length, end: caret};
}

export function matchingOptions(catalog, trigger, selected = []) {
  if (!trigger) return [];
  const rows = trigger.kind === 'skill' ? catalog.skills || [] : catalog.folders || [];
  const query = trigger.query.toLowerCase();
  return rows.filter(row => !selected.some(s => s.kind === trigger.kind && s.id === row.id)
    && `${row.name} ${row.id} ${row.description}`.toLowerCase().includes(query))
    .slice(0, 50).map(row => ({...row, kind: trigger.kind}));
}

export function toContext(selected) {
  return {skills: selected.filter(r => r.kind === 'skill').map(r => r.id),
    folders: selected.filter(r => r.kind === 'folder').map(r => r.id)};
}
