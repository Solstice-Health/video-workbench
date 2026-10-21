import htm from './vendor/htm.mjs';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, TextField, MessageBar} = window.Eeva;

export const importPending = row => ['queued', 'running'].includes(row?.status);
export function localPath(value) {
  const path = String(value || '').trim().replace(/^"(.*)"$/, '$1');
  return path.startsWith('/') || path.startsWith('~/') ? path : '';
}
export function importProgress(row) {
  if (!row) return 'Preparing the workspace…';
  if (row.status === 'queued') return 'Waiting to import your files…';
  const count = new Intl.NumberFormat().format(row.files || 0);
  const total = row.total_files ? ` of ${new Intl.NumberFormat().format(row.total_files)}` : '';
  return `${count}${total} files copied${row.skipped ? ` · ${row.skipped} skipped` : ''}`;
}

export function LocalImportDialog({open, initialPath = '', initialJob = null, api, ensureWorkspace, onReady, onPending, onClose}) {
  const [path, setPath] = useState(''), [job, setJob] = useState(null);
  const [busy, setBusy] = useState(''), [error, setError] = useState('');
  const panel = useRef(null), wasOpen = useRef(false), delivered = useRef(new Set());
  const callbacks = useRef({onReady, onPending, onClose});
  callbacks.current = {onReady, onPending, onClose};
  const pending = !!busy || importPending(job?.row);
  const pendingRef = useRef(pending); pendingRef.current = pending;
  useEffect(() => {
    if (open && !wasOpen.current) {setPath(initialPath); setJob(initialJob); setError('');}
    wasOpen.current = open;
  }, [open, initialPath, initialJob]);
  useEffect(() => {callbacks.current.onPending(pending);}, [pending]);
  useEffect(() => {
    if (!open) return;
    const prior = document.activeElement;
    panel.current?.querySelector('input')?.focus();
    const key = event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation();
        if (!pendingRef.current) callbacks.current.onClose();
      }
      if (event.key !== 'Tab') return;
      const nodes = [...panel.current.querySelectorAll('button:not(:disabled),input:not(:disabled)')];
      const first = nodes[0], last = nodes.at(-1);
      if (event.shiftKey && (document.activeElement === first || !panel.current.contains(document.activeElement))) {event.preventDefault(); last?.focus();}
      else if (!event.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) {event.preventDefault(); first?.focus();}
    };
    document.addEventListener('keydown', key, true);
    return () => {document.removeEventListener('keydown', key, true); prior?.isConnected && prior.focus();};
  }, [open]);
  useEffect(() => {
    if (!job || !importPending(job.row)) return;
    let live = true, timer;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const result = await api(`/api/requests/${encodeURIComponent(job.requestId)}/imports/${job.row.id}`, {signal: controller.signal});
        if (live) {setJob(previous => ({...previous, row: result.import})); setError('');}
      } catch (e) {
        if (live && e.name !== 'AbortError') {setError(`The import status could not be read. Retrying. ${e.message}`); timer = setTimeout(poll, 1000);}
      }
    };
    timer = setTimeout(poll, 1000);
    return () => {live = false; clearTimeout(timer); controller.abort();};
  }, [api, job]);
  useEffect(() => {
    if (job?.row.status !== 'ready' || !job.row.reference || delivered.current.has(job.row.id)) return;
    delivered.current.add(job.row.id);
    callbacks.current.onReady(job.row.reference, job.requestId, job.row);
  }, [job]);
  const start = async () => {
    if (pending || !localPath(path)) return;
    setBusy('starting'); setError(''); callbacks.current.onPending(true);
    try {
      const requestId = await ensureWorkspace();
      const result = await api(`/api/requests/${encodeURIComponent(requestId)}/imports`, {method: 'POST', body: JSON.stringify({path: localPath(path)})});
      setJob({requestId, row: result.import});
    } catch (e) {setError(e.message);}
    finally {setBusy('');}
  };
  const cancel = async () => {
    if (!importPending(job?.row)) {if (!busy) callbacks.current.onClose(); return;}
    setBusy('cancelling'); setError('');
    try {
      const result = await api(`/api/requests/${encodeURIComponent(job.requestId)}/imports/${job.row.id}`, {method: 'DELETE'});
      setJob(previous => ({...previous, row: result.import}));
    } catch (e) {setError(`The import could not be cancelled. ${e.message}`);}
    finally {setBusy('');}
  };
  if (!open) return null;
  const failure = error || (job?.row.status === 'failed' ? job.row.error || 'The import failed. Check the path and try again.' : '');
  return ReactDOM.createPortal(html`<div className="local-import-scrim" onMouseDown=${event => {if (event.target === event.currentTarget && !pending) onClose();}}>
    <section className="local-import-dialog" role="dialog" aria-modal="true" aria-labelledby="local-import-title" aria-describedby="local-import-description" ref=${panel}>
      <h2 id="local-import-title">Import a local path</h2>
      ${failure ? html`<${MessageBar} tone="error">${failure}<//>` : pending ? html`<${MessageBar} tone="progress">${busy === 'cancelling' ? 'Cancelling the import…' : importProgress(job?.row)}<//>` : job?.row.status === 'cancelled' ? html`<${MessageBar} tone="info">Import cancelled. Your original files are unchanged.<//>` : null}
      <p id="local-import-description">Copy a folder or file from this Mac into the workspace. Subfolders stay together, and your originals stay unchanged. The agent reads the files as needed.</p>
      <label htmlFor="local-import-path">Folder or file path</label>
      ${pending ? html`<p className="local-import-path">${path}</p>` : html`<${TextField} id="local-import-path" value=${path} placeholder="~/Downloads/Campaign assets" autoComplete="off" spellCheck=${false}
        aria-describedby="local-import-help" onChange=${event => setPath(event.target.value)} onKeyDown=${event => {if (event.key === 'Enter') {event.preventDefault(); start();}}}/>`}
      <p className="caption" id="local-import-help">One folder becomes one @ reference, regardless of the number of files. Importing does not send a message.</p>
      <footer><${Button} variant="secondary" disabled=${!!busy} onClick=${cancel}>${importPending(job?.row) ? 'Cancel import' : 'Close'}<//>
        <${Button} variant="primary" disabled=${pending || !localPath(path)} onClick=${start}>Import path<//></footer>
    </section>
  </div>`, document.body);
}
