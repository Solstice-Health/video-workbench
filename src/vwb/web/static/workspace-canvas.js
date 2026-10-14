import htm from './vendor/htm.mjs';
import {BrowserPanel} from './browser-panel.js';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, Tooltip, MessageBar, SolsticeMark} = window.Eeva;

export const initialCanvas = () => ({groups: [{id: 'primary', tabs: [{id: 'outputs', kind: 'outputs', title: 'Outputs'}], active: 'outputs'}], focused: 'primary'});
export function canvasReducer(state, action) {
  const next = {focused: state.focused, groups: state.groups.map(g => ({...g, tabs: [...g.tabs]}))};
  const group = next.groups.find(g => g.id === (action.group || next.focused)) || next.groups[0];
  if (action.type === 'focus') next.focused = group.id;
  if (action.type === 'open') {
    const existing = next.groups.find(g => g.tabs.some(t => t.id === action.tab.id));
    const target = existing || group;
    if (!existing) target.tabs.push(action.tab);
    target.active = action.tab.id; next.focused = target.id;
  }
  if (action.type === 'select' && group.tabs.some(t => t.id === action.id)) {group.active = action.id; next.focused = group.id;}
  if (action.type === 'close') {
    const index = group.tabs.findIndex(t => t.id === action.id);
    group.tabs = group.tabs.filter(t => t.id !== action.id);
    if (group.active === action.id) group.active = group.tabs[Math.min(index, group.tabs.length - 1)]?.id || null;
  }
  if (action.type === 'split') {
    if (next.groups.length === 1) next.groups.push({id: 'secondary', tabs: [], active: null});
    next.focused = 'secondary';
  }
  if (action.type === 'join' && next.groups.length > 1) {
    const first = next.groups[0], other = next.groups[1];
    first.tabs.push(...other.tabs.filter(t => !first.tabs.some(x => x.id === t.id)));
    if (!first.active) first.active = other.active;
    next.groups = [first]; next.focused = first.id;
  }
  if (action.type === 'move' && next.groups.length > 1) {
    const tab = group.tabs.find(t => t.id === action.id), other = next.groups.find(g => g.id !== group.id);
    if (tab) {
      group.tabs = group.tabs.filter(t => t.id !== tab.id); group.active = group.tabs[0]?.id || null;
      other.tabs.push(tab); other.active = tab.id; next.focused = other.id;
    }
  }
  return next;
}
export function formatBytes(value) {
  if (!Number.isFinite(value)) return '';
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}
export function mergeDirectoryPage(previous, page, append = false) {
  if (!append) return page;
  const byPath = new Map((previous?.entries || []).map(entry => [entry.path, entry]));
  for (const entry of page.entries || []) byPath.set(entry.path, entry);
  return {...page, entries: [...byPath.values()]};
}
// A first snapshot is history. Only events observed after it may open new tabs.
export function importedTabs(events, seen) {
  const cursor = Math.max(seen ?? 0, 0, ...events.map(event => Number(event.seq) || 0));
  if (seen == null) return {cursor, tabs: []};
  const paths = new Map();
  for (const event of events) {
    if (event.kind !== 'integration.imported' || Number(event.seq) <= seen || event.data?.is_error) continue;
    const artifacts = event.data?.artifacts || [];
    const image = artifacts.find(item => item.kind === 'image');
    const document = artifacts.find(item => item.mime_type === 'application/json' || item.workspace_path?.endsWith('.json'));
    for (const item of [image, document]) if (item?.workspace_path) paths.set(item.workspace_path, fileTab({path: item.workspace_path}));
  }
  return {cursor, tabs: [...paths.values()]};
}
export const fileTab = entry => ({id: `file:${entry.path}`, kind: 'file', title: entry.name || entry.path.split('/').at(-1), path: entry.path});
function IconAction({name, label, ...props}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label} ...${props}><${Icon} name=${name} size=${16}/><//><//>`;
}

export function WorkspaceExplorer({api, requestId, onOpen, onOutputs, onBrowser, activePath}) {
  const [folders, setFolders] = useState({}), [expanded, setExpanded] = useState(new Set(['']));
  const [loading, setLoading] = useState(new Set()), [error, setError] = useState(''), [query, setQuery] = useState('');
  const generation = useRef(0), busyPaths = useRef(new Set());
  const load = async (path, token = generation.current, cursor = null) => {
    if (!requestId || busyPaths.current.has(path)) return;
    busyPaths.current.add(path); setLoading(p => new Set(p).add(path)); setError('');
    try {
      const result = await api(`/api/requests/${encodeURIComponent(requestId)}/workspace?path=${encodeURIComponent(path)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
      if (token === generation.current) setFolders(p => ({...p, [path]: mergeDirectoryPage(p[path], result, cursor != null)}));
    } catch (e) {if (token === generation.current) setError(e.message);}
    finally {if (token === generation.current) {busyPaths.current.delete(path); setLoading(p => {const n = new Set(p); n.delete(path); return n;});}}
  };
  useEffect(() => {
    generation.current++; busyPaths.current = new Set(); setFolders({}); setExpanded(new Set([''])); setQuery(''); setError(''); setLoading(new Set());
    load('', generation.current);
    return () => {generation.current++;};
  }, [requestId]);
  const toggle = path => {
    if (expanded.has(path)) setExpanded(p => {const n = new Set(p); n.delete(path); return n;});
    else {setExpanded(p => new Set(p).add(path)); if (!folders[path]) load(path);}
  };
  const entries = (path, depth = 0) => {
    const rows = folders[path]?.entries || [];
    const rendered = rows.filter(e => !query || e.kind === 'directory' || e.name.toLowerCase().includes(query.toLowerCase())).map(entry => {
      const directory = entry.kind === 'directory', open = expanded.has(entry.path);
      return html`<li key=${entry.path}><button className=${`workspace-file-row ${activePath === entry.path ? 'selected' : ''}`} style=${{'--depth': depth}}
        aria-expanded=${directory ? open : undefined} title=${entry.path} onClick=${() => directory ? toggle(entry.path) : onOpen(fileTab(entry))}>
        <${Icon} name=${directory ? open ? 'chevron-down' : 'chevron-right' : 'file'} size=${14}/><span>${entry.name}</span>
        ${directory && loading.has(entry.path) && html`<span className="caption">…</span>`}</button>
        ${directory && open && html`<ul>${entries(entry.path, depth + 1)}${folders[entry.path]?.entries?.length === 0 && html`<li className="explorer-empty-row" style=${{'--depth': depth + 1}}>Empty folder</li>`}</ul>`}</li>`;
    });
    if (folders[path]?.next_cursor) rendered.push(html`<li key=${`more:${path}`}><button className="explorer-load-more" disabled=${loading.has(path)} onClick=${() => load(path, generation.current, folders[path].next_cursor)}>${loading.has(path) ? 'Reading files…' : 'Load more files'}</button></li>`);
    if (folders[path]?.truncated && !folders[path]?.next_cursor) rendered.push(html`<li key=${`limit:${path}`} className="explorer-empty-row">${folders[path].reason || 'Folder scan limit reached. Narrow this folder to see additional files.'}</li>`);
    return rendered;
  };
  const reload = () => {generation.current++; busyPaths.current = new Set(); setLoading(new Set()); setFolders({}); setExpanded(new Set([''])); load('');};
  return html`<div className="workspace-explorer"><div className="navigator-heading"><h2>Workspace</h2><${IconAction} name="refresh" label="Refresh workspace files" disabled=${!requestId || loading.has('')} onClick=${reload}/></div>
    <div className="explorer-shortcuts"><button onClick=${onOutputs}><${Icon} name="file" size=${14}/>Outputs</button><button onClick=${onBrowser}><${Icon} name="link" size=${14}/>Browser</button></div>
    <label className="conversation-search"><${Icon} name="search" size=${14}/><input aria-label="Filter visible workspace files" placeholder="Filter visible files" value=${query} onChange=${e => setQuery(e.target.value)}/></label>
    ${error && html`<p className="explorer-error" role="alert">${error}<button className="text-action" onClick=${() => load('')}>Try again</button></p>`}
    <div className="workspace-file-list">${!requestId ? html`<p className="navigator-empty">Choose a conversation to browse its files.</p>` : loading.has('') && !folders[''] ? html`<p className="navigator-empty">Reading workspace…</p>` : html`<ul aria-label="Workspace files">${entries('')}</ul>`}</div>
    <div className="explorer-footer"><${Icon} name="folder" size=${13}/><span>Conversation files</span></div>
  </div>`;
}

function PdfPreview({api, requestId, path}) {
  const [page, setPage] = useState(1), [result, setResult] = useState(null), [error, setError] = useState('');
  const [loading, setLoading] = useState(true), [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true; setLoading(true); setError('');
    api(`/api/requests/${encodeURIComponent(requestId)}/workspace/pdf?path=${encodeURIComponent(path)}&page=${page}`)
      .then(data => {if (live) setResult(data);}).catch(e => {if (live) setError(e.message);})
      .finally(() => {if (live) setLoading(false);});
    return () => {live = false;};
  }, [api, requestId, path, page, revision]);
  return html`<div className="pdf-preview"><div className="pdf-controls" aria-label="PDF pages">
    <${IconAction} name="chevron-left" label="Previous PDF page" disabled=${loading || page <= 1} onClick=${() => setPage(n => n - 1)}/>
    <span aria-live="polite">${result ? `Page ${page} of ${result.pages}` : 'Opening PDF…'}</span>
    <${IconAction} name="chevron-right" label="Next PDF page" disabled=${loading || !result || page >= result.pages} onClick=${() => setPage(n => n + 1)}/></div>
    ${error ? html`<div className="canvas-message"><${MessageBar} tone="error">${error}<//><${Button} variant="secondary" size="sm" onClick=${() => setRevision(n => n + 1)}>Try again<//></div>`
      : loading ? html`<div className="canvas-empty"><p>Rendering page ${page}…</p></div>` : html`<div className="pdf-page"><img src=${result.image} alt=${`PDF page ${result.page} of ${result.pages}`} width=${result.width} height=${result.height}/></div>`}
  </div>`;
}

function FilePreview({api, requestId, tab}) {
  const [file, setFile] = useState(null), [error, setError] = useState(''), [revision, setRevision] = useState(0);
  useEffect(() => {
    let live = true; setFile(null); setError('');
    api(`/api/requests/${encodeURIComponent(requestId)}/workspace/file?path=${encodeURIComponent(tab.path)}`)
      .then(result => {if (live) setFile(result);}).catch(e => {if (live) setError(e.message);});
    return () => {live = false;};
  }, [api, requestId, tab.path, revision]);
  const kind = file?.preview_kind, url = file?.url;
  return html`<section className="file-preview" aria-label=${`Preview ${tab.title}`}>
    <div className="file-preview-toolbar"><span title=${tab.path}>${tab.path}</span><div>${file && html`<span className="caption">${formatBytes(file.size)}</span>`}
      <${IconAction} name="refresh" label="Reload file preview" onClick=${() => setRevision(n => n + 1)}/>
      ${url && html`<a className="text-link" href=${url} target="_blank" rel="noopener noreferrer" aria-label=${`Open ${tab.title} in a new tab`}>Open <${Icon} name="arrow-up" size=${13}/></a>`}</div></div>
    ${error ? html`<div className="canvas-message"><${MessageBar} tone="error">${error}<//><${Button} variant="secondary" size="sm" onClick=${() => setRevision(n => n + 1)}>Try again<//></div>` : !file ? html`<div className="canvas-empty"><${SolsticeMark} size=${24}/><p>Opening ${tab.title}…</p></div>`
      : kind === 'video' && url ? html`<div className="file-media"><video key=${url} controls playsInline preload="metadata" src=${url} aria-label=${tab.title}/></div>`
      : kind === 'audio' && url ? html`<div className="file-media"><audio controls preload="metadata" src=${url} aria-label=${tab.title}/></div>`
      : kind === 'image' && url ? html`<div className="file-media"><img src=${url} alt=${tab.title}/></div>`
      : kind === 'html' && url ? html`<iframe className="file-document" src=${url} sandbox="allow-scripts" referrerPolicy="no-referrer" title=${`HTML preview: ${tab.title}`}/>`
      : kind === 'pdf' ? html`<${PdfPreview} key=${`${tab.path}:${revision}`} api=${api} requestId=${requestId} path=${tab.path}/>`
      : ['text', 'json', 'code', 'markdown'].includes(kind) || typeof file.text === 'string' ? html`<div className="file-text-view">${file.truncated && html`<p className="file-truncation">Showing a bounded preview. Open the file to read the rest.</p>`}<pre tabIndex="0">${file.text ?? ''}</pre></div>`
      : html`<div className="canvas-empty"><${Icon} name="file" size=${18}/><h3>Preview unavailable</h3><p>This file type can be opened or downloaded.</p>${url && html`<a className="secondary-link" href=${url} download>Download ${tab.title}</a>`}</div>`}
  </section>`;
}

export function WorkspaceCanvas({api, requestId, state, dispatch, renderOutputs, onExplore, notice, visible = true}) {
  const focusTab = (event, group, index) => {
    const next = {ArrowLeft: (index - 1 + group.tabs.length) % group.tabs.length, ArrowRight: (index + 1) % group.tabs.length, Home: 0, End: group.tabs.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault(); dispatch({type: 'select', group: group.id, id: group.tabs[next].id});
    const row = event.currentTarget.closest('[role=tablist]'); requestAnimationFrame(() => row?.querySelectorAll('[role=tab]')[next]?.focus());
  };
  return html`<section className=${`workspace-canvas ${state.groups.length > 1 ? 'split' : ''}`} aria-label="Workspace canvas">
    ${state.groups.map(group => {const tab = group.tabs.find(t => t.id === group.active); return html`<section key=${group.id} className=${`canvas-group ${state.focused === group.id ? 'focused' : ''}`} aria-label=${group.id === 'primary' ? 'Main canvas group' : 'Second canvas group'} onFocusCapture=${() => {if (state.focused !== group.id) dispatch({type: 'focus', group: group.id});}}>
      <div className="canvas-tabbar"><div className="canvas-tabs" role="tablist" aria-label=${`${group.id === 'primary' ? 'Main' : 'Second'} canvas tabs`}>
        ${group.tabs.map((item, i) => html`<div className=${`canvas-tab ${item.id === group.active ? 'active' : ''}`} key=${item.id}>
          <button role="tab" aria-selected=${item.id === group.active} tabIndex=${item.id === group.active ? 0 : -1} title=${item.path || item.title} onClick=${() => dispatch({type: 'select', group: group.id, id: item.id})} onKeyDown=${event => focusTab(event, group, i)}>
            <${Icon} name=${item.kind === 'browser' ? 'link' : 'file'} size=${14}/><span>${item.title}</span></button>
          <${IconAction} name="x" label=${`Close canvas tab ${item.title}`} onClick=${event => {const row = event.currentTarget.closest('[role=tablist]'); dispatch({type: 'close', group: group.id, id: item.id}); requestAnimationFrame(() => row?.querySelector('[aria-selected=true]')?.focus());}}/></div>`)}</div>
        <div className="canvas-group-actions"><${IconAction} name="link" label="Open browser in canvas" onClick=${() => dispatch({type: 'open', group: group.id, tab: {id: 'browser', kind: 'browser', title: 'Browser'}})}/>
          ${state.groups.length === 1 ? html`<${IconAction} name="table" label="Split canvas into two groups" onClick=${() => dispatch({type: 'split'})}/>` : html`<${IconAction} name=${group.id === 'secondary' ? 'x' : 'table'} label=${group.id === 'secondary' ? 'Join canvas groups' : 'Move active tab to other group'} disabled=${group.id === 'primary' && !tab} onClick=${() => dispatch(group.id === 'secondary' ? {type: 'join'} : {type: 'move', group: group.id, id: tab?.id})}/>`}</div></div>
      <div className="canvas-group-content" role="tabpanel" aria-label=${tab?.title || 'Empty canvas'}>
        ${!visible ? null : tab?.kind === 'browser' ? html`<${BrowserPanel} key=${requestId || 'new'} embedded api=${api} requestId=${requestId} notice=${notice} onClose=${() => dispatch({type: 'close', group: group.id, id: tab.id})}/>`
          : tab?.kind === 'outputs' ? renderOutputs()
          : tab?.kind === 'file' ? html`<${FilePreview} key=${tab.id} api=${api} requestId=${requestId} tab=${tab}/>`
          : html`<div className="canvas-empty"><${Icon} name="folder" size=${18}/><h3>A place for your work</h3><p>Open a file from the workspace, or bring the browser alongside your conversation.</p><div><${Button} variant="secondary" size="sm" onClick=${onExplore}>Browse workspace<//><${Button} variant="text" size="sm" onClick=${() => dispatch({type: 'open', group: group.id, tab: {id: 'browser', kind: 'browser', title: 'Browser'}})}>Open browser<//></div></div>`}
      </div></section>`;})}
  </section>`;
}
