import htm from './vendor/htm.mjs';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, MessageBar} = window.Eeva;
// Keep unfinished reviews when switching canvas tabs during this page session.
const artifactDrafts = new Map();

export const artifactTab = row => ({id:`artifact:${row.id}`,kind:'artifact',artifactId:row.id,revision:row.revision,title:row.title});
export const artifactLibraryTab = () => ({id:'artifacts',kind:'artifacts',title:'Canvases'});

// The event's actual WindowProxy must be the currently mounted isolated frame.
// No message here mutates the host, submits chat or changes a user's typed draft.
export function artifactProposal(event, frameWindow, artifact) {
  if (!frameWindow || event.source !== frameWindow || event.origin !== 'null' || !artifact) return null;
  const data = event.data;
  if (!data || data.type !== 'vwb-artifact-select' || data.artifact_id !== artifact.id || data.revision !== artifact.revision) return null;
  const value = data.selection;
  if (!value || typeof value !== 'object' || !artifact.items.some(item => item.id === value.item_id)) return null;
  const result = {item_id:value.item_id};
  if (value.region != null) {
    const r = value.region, keys = ['x','y','width','height'];
    if (!r || keys.some(k => typeof r[k] !== 'number' || !Number.isFinite(r[k])) || r.x < 0 || r.y < 0 || r.width <= 0 || r.height <= 0 || r.x+r.width > 1.000001 || r.y+r.height > 1.000001) return null;
    result.region = Object.fromEntries(keys.map(k => [k,r[k]]));
  }
  if (value.time_s != null) {
    if (typeof value.time_s !== 'number' || !Number.isFinite(value.time_s) || value.time_s < 0 || value.time_s > 86400) return null;
    result.time_s = value.time_s;
  }
  if (value.text != null) {
    if (typeof value.text !== 'string' || value.text.length > 8000) return null;
    result.text = value.text;
  }
  return result;
}

export function ArtifactLibrary({api, requestId, onOpen}) {
  const [rows,setRows] = useState(null), [error,setError] = useState(''), [revision,setRevision] = useState(0);
  useEffect(() => {
    let live = true; setRows(null); setError('');
    if (requestId) api(`/api/requests/${encodeURIComponent(requestId)}/artifacts`).then(result => {if(live)setRows(result.artifacts);}).catch(e => {if(live)setError(e.message);});
    return () => {live=false;};
  }, [api,requestId,revision]);
  return html`<section className="artifact-library"><div className="artifact-library-heading"><h2>Canvases</h2><${Button} variant="text" size="sm" onClick=${() => setRevision(n=>n+1)}>Refresh<//></div>
    ${error ? html`<${MessageBar} tone="error">${error}<//>` : !requestId ? html`<p>Choose a conversation to see its canvases.</p>` : rows === null ? html`<p>Reading canvases…</p>` : !rows.length ? html`<div className="canvas-empty"><${Icon} name="table" size=${18}/><h3>A canvas for anything</h3><p>Ask the agent to show a review grid, comparison, chart or interactive explanation. Published canvases appear here.</p></div>` : html`<div className="artifact-library-list">${rows.map(row => html`<button key=${row.id} onClick=${() => onOpen(artifactTab(row))}><${Icon} name="table" size=${18}/><span><strong>${row.title}</strong><small>Revision ${row.revision} · ${row.items.length} review ${row.items.length===1?'item':'items'}</small></span><${Icon} name="chevron-right" size=${14}/></button>`)}</div>`}
  </section>`;
}

export function ArtifactPanel({api, requestId, tab, onRevision}) {
  const [artifact,setArtifact] = useState(null), [error,setError] = useState(''), [info,setInfo] = useState('');
  const [selected,setSelected] = useState({item_id:null}), [proposal,setProposal] = useState(null), [text,setText] = useState('');
  const [busy,setBusy] = useState(false), [entries,setEntries] = useState([]), [expanded,setExpanded] = useState(false), [reload,setReload] = useState(0);
  const draftCache = useRef(artifactDrafts), liveDraft = useRef(null);
  const frame = useRef(null), submission = useRef(null), apiRoot = `/api/requests/${encodeURIComponent(requestId)}/artifacts/${encodeURIComponent(tab.artifactId)}`;
  const draftKey = `${requestId}:${tab.artifactId}:${tab.revision || 'latest'}`;
  liveDraft.current = {selected,proposal,text,submission:submission.current};
  useEffect(() => {
    const saved=draftCache.current.get(draftKey);
    setSelected(saved?.selected || {item_id:null});setProposal(saved?.proposal || null);setText(saved?.text || '');setExpanded(!!saved?.text || !!saved?.selected?.item_id);submission.current=saved?.submission || null;
    return () => {draftCache.current.set(draftKey,liveDraft.current);};
  }, [draftKey]);
  useEffect(() => {
    let live = true; setArtifact(null); setError(''); setInfo('');
    api(`${apiRoot}${tab.revision ? `?revision=${tab.revision}` : ''}`).then(result => {if(live)setArtifact(result);}).catch(e=>{if(live)setError(e.message);});
    api(`${apiRoot}/feedback`).then(result=>{if(live)setEntries(result.feedback);}).catch(()=>{});
    return () => {live=false;};
  }, [api,apiRoot,tab.revision,reload]);
  useEffect(() => {
    const receive = event => {const next=artifactProposal(event,frame.current?.contentWindow,artifact); if(next)setProposal(next);};
    window.addEventListener('message',receive); return () => window.removeEventListener('message',receive);
  }, [artifact]);
  const applyProposal = () => {if(proposal){const {text:hint,...target}=proposal;setSelected(target);if(!text && hint)setText(hint);setExpanded(true);setProposal(null);submission.current=null;}};
  const send = async mode => {
    if(busy || !text.trim() || !artifact)return;
    setBusy(true);setError('');setInfo('');
    const payload={...selected,revision:artifact.revision,text:text.trim(),mode};
    const signature=JSON.stringify(payload);
    if(submission.current?.signature!==signature)submission.current={signature,id:crypto.randomUUID()};
    try {
      const result=await api(`${apiRoot}/feedback`,{method:'POST',body:JSON.stringify({...payload,submission_id:submission.current.id})});
      if(result.feedback.delivery!=='queued'){setError('Feedback was saved but delivery is unconfirmed. Ask the agent to read canvas feedback before sending again.');return;}
      setEntries(previous=>[...previous.filter(entry=>entry.id!==result.feedback.id),result.feedback]);setText('');submission.current=null;
      setInfo(mode==='now'?'Feedback sent. The agent will continue with your changes.':'Feedback queued in your conversation.');
    } catch(e){setError(e.message);} finally{setBusy(false);}
  };
  const label = id => artifact?.items.find(item=>item.id===id)?.label || 'Whole canvas';
  const targetDetails = value => [value.region && `Region ${Math.round(value.region.x*100)}%, ${Math.round(value.region.y*100)}% · ${Math.round(value.region.width*100)}% × ${Math.round(value.region.height*100)}%`, value.time_s != null && `${value.time_s.toFixed(2)} s`].filter(Boolean).join(' · ');
  return html`<section className="artifact-panel" aria-label=${`${tab.title} interactive canvas`}>
    <div className="artifact-toolbar"><span>${artifact ? `Revision ${artifact.revision} · ${artifact.items.length} review items` : 'Opening canvas…'}</span>${tab.latestRevision > tab.revision && html`<button className="text-action" disabled=${busy} onClick=${()=>onRevision(tab.latestRevision)}>View revision ${tab.latestRevision}</button>`}<button className="text-action" onClick=${()=>setReload(n=>n+1)} disabled=${busy}>Reload</button></div>
    ${(error||info) && html`<div className="artifact-notice"><${MessageBar} tone=${error?'error':'info'}>${error||info}<//></div>`}
    ${artifact ? html`<iframe key=${`${artifact.id}:${artifact.revision}:${reload}`} ref=${frame} className="artifact-frame" title=${`${artifact.title} interactive preview`} src=${artifact.url} sandbox="allow-scripts" referrerPolicy="no-referrer"/>` : html`<div className="canvas-empty"><p>${error?'The canvas could not open.':'Opening your canvas…'}</p></div>`}
    ${artifact && html`<div className="artifact-feedback"><div className="artifact-feedback-heading"><button className="text-action" aria-expanded=${expanded} onClick=${()=>setExpanded(v=>!v)}><${Icon} name=${expanded?'chevron-down':'chevron-right'} size=${14}/>Give feedback</button><span>${label(selected.item_id)}</span></div>
      ${proposal && html`<div className="artifact-proposal"><span>Canvas selection: <strong>${label(proposal.item_id)}</strong>${targetDetails(proposal)&&html`<small>${targetDetails(proposal)}</small>`}</span><${Button} variant="secondary" size="sm" disabled=${busy} onClick=${applyProposal}>Use selection<//><button className="text-action" onClick=${()=>setProposal(null)}>Dismiss</button></div>`}
      ${expanded && html`<div className="artifact-feedback-form"><label>Review item<select disabled=${busy} value=${selected.item_id||''} onChange=${event=>{setSelected({item_id:event.target.value||null});submission.current=null;}}><option value="">Whole canvas</option>${artifact.items.map(item=>html`<option key=${item.id} value=${item.id}>${item.label}</option>`)}</select></label>
        ${targetDetails(selected)&&html`<div className="artifact-selection-detail"><span>${targetDetails(selected)}</span><button className="text-action" disabled=${busy} onClick=${()=>setSelected({item_id:selected.item_id})}>Clear region and time</button></div>`}
        <label>What should change?<textarea rows="2" maxLength=${8000} placeholder="Describe your changes for this item…" value=${text} disabled=${busy} onChange=${event=>{setText(event.target.value);submission.current=null;}}/></label>
        <div className="artifact-send-row"><span>Only feedback you send enters the conversation.</span><button className="text-action" disabled=${busy||!text.trim()} onClick=${()=>send('now')}>Send now</button><${Button} variant="secondary" size="sm" disabled=${busy||!text.trim()} onClick=${()=>send('queue')}>${busy?'Sending…':'Send feedback'}<//></div>
        ${entries.length>0&&html`<details className="artifact-feedback-history"><summary>Sent feedback (${entries.length})</summary>${entries.slice(-12).reverse().map(entry=>html`<article key=${entry.id}><strong>${label(entry.item_id)} · Revision ${entry.revision}</strong><p>${entry.text}</p><small>${targetDetails(entry)}</small></article>`)}</details>`}
      </div>`}
    </div>`}
  </section>`;
}
