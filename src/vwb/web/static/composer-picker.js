// A trigger must start a word; email addresses and ordinary slashes stay as text.
export const displayPath = path => String(path || '').replace(/^work\/imports\/[a-f0-9]{32}\//, '');

export function triggerAt(text, caret = text.length) {
  const before = text.slice(0, caret);
  const match = /(?:^|\s)(\/features(?:[ \t]+([^\n]{0,80}))?|@"([^"\n]{0,512})"?|@([^\s@"]{0,512}))$/.exec(before);
  if (!match) return null;
  const quoted = match[1].startsWith('@"');
  return {kind: match[1].startsWith('@') ? 'path' : 'skill', query: (match[2] || match[3] || match[4] || '').trim(),
    start: caret - match[1].length, end: caret + (quoted && !match[1].endsWith('"') && text[caret] === '"' ? 1 : 0)};
}

export function matchingOptions(catalog, trigger, selected = []) {
  if (!trigger) return [];
  const rows = trigger.kind === 'skill' ? (catalog.skills || []).map(row => ({...row, kind: 'skill'}))
    : [...(catalog.folders || []).map(row => ({...row, kind: 'folder'})),
      ...(catalog.files || []).map(row => ({...row, kind: 'file'}))];
  const query = (trigger.kind === 'path' ? catalog.search_query ?? trigger.query : trigger.query)
    .toLowerCase().replace(/^(?:work\/|work$)/, '').replace(/\/$/, '');
  return rows.filter(row => !selected.some(s => s.kind === row.kind && s.id === row.id)
    && `${row.name} ${row.id} ${row.description}`.toLowerCase().includes(query))
    .slice(0, 100);
}

// A sidebar drag references a path already inside this conversation, never a host path.
export function workspaceReference(value, requestId) {
  let entry;
  try {entry = typeof value === 'string' ? JSON.parse(value) : value;} catch (_) {return null;}
  if (!requestId || entry?.requestId !== requestId || typeof entry.path !== 'string') return null;
  const directory = entry.kind === 'directory';
  if (entry.path !== 'work' && !entry.path.startsWith('work/')) return null;
  const id = entry.path === 'work' || entry.path === 'work/' ? '.' : entry.path.slice(5);
  if (!id || id.includes('\\') || id.includes('\0') || (id === '.' && !directory)
      || (id !== '.' && id.split('/').some(part => !part || part.startsWith('.') || ['node_modules', '__pycache__'].includes(part)))) return null;
  return {id, kind: directory ? 'folder' : 'file', name: `work/${id === '.' ? '' : id}${directory && id !== '.' ? '/' : ''}`,
    description: directory ? 'All files in this folder, read as needed' : 'File in this conversation’s workspace'};
}

export function toContext(selected) {
  const context = {skills: selected.filter(r => r.kind === 'skill').map(r => r.id),
    folders: selected.filter(r => r.kind === 'folder').map(r => r.id),
    plugins: selected.filter(r => r.kind === 'plugin').map(r => r.id)};
  const files = selected.filter(r => r.kind === 'file').map(r => r.id);
  if (files.length) context.files = files;
  return context;
}
