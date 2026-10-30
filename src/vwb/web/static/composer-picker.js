// A trigger must start a word; email addresses and ordinary slashes stay as text.
export const displayPath = path => String(path || '').replace(/^work\/imports\/[a-f0-9]{32}\//, '');

export function triggerAt(text, caret = text.length) {
  const before = text.slice(0, caret);
  const workflow = /(?:^|\s)(\/workflows(?:[ \t]+([^\n]{0,80}))?|\/[a-z][a-z-]*)$/.exec(before);
  const workflowList = workflow && /^\/workflows(?:[ \t]|$)/.test(workflow[1]);
  if (workflow && (workflowList || '/brand-context'.startsWith(workflow[1]))) {
    return {kind: 'workflow', query: workflowList ? (workflow[2] || '').trim() : workflow[1],
      start: caret - workflow[1].length, end: caret};
  }
  const match = /(?:^|\s)(\/features(?:[ \t]+([^\n]{0,80}))?|@"([^"\n]{0,512})"?|@([^\s@"]{0,512}))$/.exec(before);
  if (!match) return null;
  const quoted = match[1].startsWith('@"');
  return {kind: match[1].startsWith('@') ? 'path' : 'skill', query: (match[2] || match[3] || match[4] || '').trim(),
    start: caret - match[1].length, end: caret + (quoted && !match[1].endsWith('"') && text[caret] === '"' ? 1 : 0)};
}

export function matchingOptions(catalog, trigger, selected = []) {
  if (!trigger) return [];
  const rows = trigger.kind === 'workflow' ? (catalog.workflows || []).map(row => ({...row, kind: 'workflow'}))
    : trigger.kind === 'skill' ? (catalog.skills || []).map(row => ({...row, kind: 'skill'}))
    : [...(catalog.folders || []).map(row => ({...row, kind: 'folder'})),
      ...(catalog.files || []).map(row => ({...row, kind: 'file'}))];
  const rawQuery = (trigger.kind === 'path' ? catalog.search_query ?? trigger.query : trigger.query).toLowerCase();
  const query = trigger.kind === 'path' ? rawQuery.replace(/^(?:work\/|work$)/, '').replace(/\/$/, '') : rawQuery;
  return rows.filter(row => !selected.some(s => s.kind === row.kind && s.id === row.id)
    && (trigger.kind === 'workflow' && query.startsWith('/') ? (row.command || `/${row.id}`).toLowerCase().startsWith(query)
      : `${row.name} ${row.id} ${row.description || ''} ${row.command || ''}`.toLowerCase().includes(query)))
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
  const workflows = selected.filter(r => r.kind === 'workflow').map(r => r.id);
  if (workflows.length) context.workflows = workflows;
  return context;
}

// A complete command also works after dismissing autocomplete or pasting a brief.
export function workflowReferences(text, selected) {
  if (!/^\s*\/brand-context(?:\s|$)/.test(text) || selected.some(row => row.kind === 'workflow' && row.id === 'brand-context')) return selected;
  return [...selected, {kind: 'workflow', id: 'brand-context', name: 'Build brand context', command: '/brand-context'}];
}

export function workflowProblem(selected, projectId) {
  if (!selected.some(row => row.kind === 'workflow')) return '';
  const missing = [];
  if (!projectId) missing.push('Assign this conversation to a project using “Move current conversation” in the Conversations sidebar. You can import a folder first.');
  if (!selected.some(row => row.kind === 'folder')) missing.push('Tag a folder with @ or import a local folder.');
  return missing.join(' ');
}
