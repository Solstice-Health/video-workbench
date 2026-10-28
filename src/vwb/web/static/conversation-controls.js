import htm from './vendor/htm.mjs';

const {createElement, useState, useEffect, useRef, useId} = window.React;
const {Button} = window.Eeva;
const html = htm.bind(createElement);

/* Integration contract (all callbacks may return promises; reject or return false on failure):
 * ProjectNavigation: projects [{id,name}], requests [{id,title,project_id,active,queued}],
 * selected request ID, query string, busy boolean. onChoose(id), onCreateConversation(projectId|null),
 * onCreateProject({name}) -> {id,...} or {project:{id,...}}, onMoveConversation(requestId,projectId).
 * The parent adds a stable operation_id to project creation and uses /api/workspaces for draft creation.
 * MessageActions: onAction('fork'|'rewind'|'edit', message). Pass disabled for active/pending turns.
 * HistoryDialog: operation is that action string; onSubmit({action,message_id,text?,fork?}).
 * The parent adds expected_revision and a stable operation_id, performs the API call, and closes on success.
 * Pass busy for active/pending turns as well as mutations. No network or request state lives here.
 * ProjectDialog: operation 'create'|'move', projects, request, busy, error, onSubmit, onClose.
 * Its onSubmit receives {name} (create/new destination) or {project_id} (existing destination).
 * Import conversation-controls.css once alongside this module.
 */

const requestTitle = request => request?.title || 'Untitled conversation';
const projectName = project => project?.name || 'Untitled project';
const messageId = message => message?.id || message?.message_id;
const human = message => message?.role === 'human' || message?.role === 'user';
const count = value => Array.isArray(value) ? value.length : Number(value) || 0;

export function pendingTurns(value) {
  return !!(value?.active || count(value?.pending) > 0 || count(value?.queue) > 0 || count(value?.queued) > 0
    || count(value?.queue_count) > 0 || ['working', 'running', 'queued', 'pending'].includes(value?.status));
}

export function groupConversations(projects = [], requests = [], query = '') {
  const term = String(query).trim().toLocaleLowerCase();
  const matches = value => String(value || '').toLocaleLowerCase().includes(term);
  const byProject = new Map(projects.map(project => [project.id, []]));
  const conversations = [];
  for (const request of requests) {
    const rows = byProject.get(request.project_id);
    if (rows) rows.push(request);
    else if (matches(`${requestTitle(request)} ${request.id}`)) conversations.push(request);
  }
  return {projects:projects.map(project => ({project, requests:byProject.get(project.id).filter(request =>
    matches(projectName(project)) || matches(`${requestTitle(request)} ${request.id}`))}))
    .filter(group => !term || matches(projectName(group.project)) || group.requests.length), conversations};
}

export function messageActions(message) {
  if (!messageId(message) || !['human', 'user', 'assistant', 'job_lead'].includes(message?.role)) return [];
  return human(message) ? ['fork', 'rewind', 'edit'] : ['fork', 'rewind'];
}

export function attachmentCount(message) {
  return Array.isArray(message?.attachments) ? message.attachments.length : 0;
}

export function historyPayload(operation, message, text = message?.text || '', fork = false) {
  if (!messageActions(message).includes(operation)) throw new Error('Choose a saved message for this action.');
  if (pendingTurns(message)) throw new Error('Wait for active and pending turns to finish.');
  const payload = {action:operation, message_id:messageId(message)};
  if (operation === 'edit') {
    if (typeof text !== 'string' || (!text.trim() && !attachmentCount(message))) {
      throw new Error('Enter a message or keep at least one attachment.');
    }
    // Attachments and context are preserved by the server using message_id. Never resend or strip them.
    return {...payload, text, fork:!!fork};
  }
  return payload;
}

export function projectPayload(operation, name, destination, projects = [], request = null) {
  if (!['create', 'move'].includes(operation)) throw new Error('Choose a project action.');
  if (operation === 'move') {
    if (!request?.id) throw new Error('Choose a conversation to move.');
    if (pendingTurns(request)) throw new Error('Wait for active and pending turns to finish.');
    if (destination !== 'new') {
      if (!destination || !projects.some(project => project.id === destination)) throw new Error('Choose an available project.');
      const project_id = destination;
      if (request.project_id === project_id) throw new Error('Choose a different project.');
      return {project_id};
    }
  }
  const trimmed = typeof name === 'string' ? name.trim() : '';
  if (!trimmed) throw new Error('Enter a project name.');
  if ([...trimmed].length > 120 || /[\u0000-\u001f]/.test(name)) throw new Error('Use a project name of up to 120 characters, without line breaks or control characters.');
  return {name:trimmed};
}

// A ref closes the gap before React renders busy, including two clicks in the same event loop.
function useSubmission(busy) {
  const [pending, setPending] = useState(false), [error, setError] = useState('');
  const locked = useRef(false), blocked = useRef(busy);
  blocked.current = busy;
  const run = async action => {
    if (blocked.current || locked.current) return false;
    locked.current = true; setPending(true); setError('');
    try {return await action();}
    catch (failure) {setError(failure?.message || 'The change could not be saved. Try again.'); return false;}
    finally {locked.current = false; setPending(false);}
  };
  return {pending, error, run, isBlocked:() => !!(blocked.current || locked.current)};
}

function Dialog({id, title, busy, onClose, children}) {
  const panel = useRef(null), state = useRef({busy, onClose});
  state.current = {busy, onClose};
  const close = () => {if (!state.current.busy) state.current.onClose?.();};
  useEffect(() => {
    const node = panel.current, prior = document.activeElement;
    // Native modal dialogs make background content inert. Explicit Tab wrapping below
    // also keeps focus in the panel in browsers that otherwise visit browser chrome.
    node.showModal();
    (node.querySelector('[data-cc-initial-focus]:not(:disabled)') || node).focus();
    return () => {node.close(); if (prior?.isConnected) prior.focus();};
  }, []);
  return window.ReactDOM.createPortal(html`<dialog ref=${panel} className="cc-dialog" tabIndex="-1"
    aria-modal="true" aria-labelledby=${`${id}-title`} aria-describedby=${`${id}-description`} aria-busy=${!!busy}
    onKeyDown=${event => {
      if (event.key !== 'Tab') return;
      const nodes = [...event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')];
      const first = nodes[0], last = nodes.at(-1), active = document.activeElement;
      if (!nodes.length || !nodes.includes(active) || (event.shiftKey ? active === first : active === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
        if (!nodes.length) event.currentTarget.focus();
      }
    }}
    onCancel=${event => {event.preventDefault(); close();}} onClick=${event => {
      if (event.target !== event.currentTarget) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
    }}>
    <h2 id=${`${id}-title`}>${title}</h2>${children}
  </dialog>`, document.body);
}

const actionLabel = {fork:'Fork from here', rewind:'Rewind to here', edit:'Edit message'};

export function MessageActions({message, disabled = false, onAction}) {
  const actions = messageActions(message), blocked = !!disabled || pendingTurns(message) || !onAction;
  if (!actions.length) return null;
  return html`<div className="cc-message-actions" role="group" aria-label="Message history actions">
    ${actions.map(action => html`<${Button} key=${action} type="button" variant="text" size="sm" disabled=${blocked}
      title=${blocked ? 'History actions are unavailable while active or pending turns are being handled.' : actionLabel[action]}
      onClick=${() => {if (!blocked) onAction(action, message);}}>${actionLabel[action]}<//>`)}
  </div>`;
}

export function HistoryDialog(props) {
  if (!props.operation || !props.message) return null;
  // A different target always gets its own draft and submission lock, even without a parent key.
  return html`<${HistoryForm} key=${`${props.operation}:${messageId(props.message)}`} ...${props}/>`;
}

function HistoryForm({operation, message, onSubmit, onClose, busy = false, error = ''}) {
  const id = useId(), [text, setText] = useState(message.text || '');
  const submission = useSubmission(busy || pendingTurns(message)), blocked = !!busy || submission.pending || pendingTurns(message);
  const editing = operation === 'edit', valid = messageActions(message).includes(operation);
  const attachments = attachmentCount(message), failure = submission.error || error;
  const disabled = blocked || !valid || !onSubmit || (editing && !text.trim() && !attachments);
  const submit = fork => submission.run(() => {
    if (!valid || !onSubmit) return false;
    return onSubmit(historyPayload(operation, message, text, fork));
  });
  const title = editing ? 'Edit message' : operation === 'fork' ? 'Fork conversation' : 'Rewind conversation';
  return html`<${Dialog} id=${id} title=${title} busy=${blocked} onClose=${() => {if (!submission.isBlocked()) onClose?.();}}>
    <p id=${`${id}-description`}>${editing
      ? 'Edit + continue removes this message and everything after it, then sends your edited message here. Edit + new conversation uses the history before this message and sends your edit in a new conversation.'
      : operation === 'fork'
        ? 'Create a new conversation with the history through and including this message. The original conversation keeps its full history.'
        : 'Keep the history through and including this message. All later messages will be removed from this conversation.'}</p>
    <p className="cc-workspace-note">Current workspace files stay as-is. History changes do not roll back the filesystem.</p>
    ${editing ? html`<label className="cc-field" htmlFor=${`${id}-text`}>Message text
      <textarea id=${`${id}-text`} rows="7" value=${text} disabled=${blocked || !valid}
        aria-describedby=${`${id}-attachments`} onChange=${event => setText(event.target.value)}/>
    </label>` : html`<blockquote className="cc-message-preview">${message.text || '(Attachment-only message)'}</blockquote>`}
    <p id=${`${id}-attachments`} className="cc-caption">${attachments} ${attachments === 1 ? 'attachment' : 'attachments'} on this message · preserved with the message.</p>
    ${!valid && html`<p role="alert" className="cc-error">Choose a saved message for this action.</p>`}
    ${failure && html`<p role="alert" className="cc-error">${failure}</p>`}
    ${blocked && html`<p role="status" className="cc-caption">Wait for active or pending work to finish.</p>`}
    <footer className="cc-dialog-actions">
      <${Button} type="button" variant="secondary" disabled=${blocked} data-cc-initial-focus="true"
        onClick=${() => {if (!submission.isBlocked()) onClose?.();}}>Cancel<//>
      ${editing && html`<${Button} type="button" variant="secondary" disabled=${disabled} onClick=${() => submit(true)}>Edit + new conversation<//>`}
      <${Button} type="button" variant=${operation === 'fork' ? 'primary' : 'destructive'} disabled=${disabled}
        onClick=${() => submit(false)}>${editing ? 'Edit + continue' : operation === 'fork' ? 'Fork conversation' : 'Rewind conversation'}<//>
    </footer>
  <//>`;
}

export function ProjectDialog(props) {
  return html`<${ProjectForm} key=${`${props.operation || 'create'}:${props.request?.id || ''}`} ...${props}/>`;
}

function ProjectForm({operation = 'create', projects = [], request = null, busy = false, error = '', onSubmit, onClose}) {
  const id = useId(), [name, setName] = useState(''), [destination, setDestination] = useState(request?.project_id || '');
  const moving = operation === 'move', submission = useSubmission(busy || (moving && pendingTurns(request)));
  const blocked = !!busy || submission.pending || (moving && pendingTurns(request));
  const needsName = !moving || destination === 'new', failure = submission.error || error;
  let payload = null;
  try {payload = projectPayload(operation, name, destination, projects, request);} catch { /* Disable incomplete forms. */ }
  const submit = () => submission.run(() => onSubmit?.(projectPayload(operation, name, destination, projects, request)));
  return html`<${Dialog} id=${id} title=${moving ? 'Move conversation' : 'New project'} busy=${blocked}
    onClose=${() => {if (!submission.isBlocked()) onClose?.();}}>
    <form onSubmit=${event => {event.preventDefault(); if (payload && onSubmit) submit();}}>
      <p id=${`${id}-description`}>${moving ? `Choose a project for “${requestTitle(request)}”.` : 'Give your project a name to organize its conversations.'}</p>
      ${moving && html`<p className="cc-workspace-note">${(request?.project_id
        ? 'This conversation will use the destination project’s workspace. Its original files remain in the original project.'
        : 'Joining a project merges this conversation’s files into its workspace, preserving paths. If the same path contains a different file, rename it or choose a new project; existing files will not be overwritten.')
        + ' Conversations keep separate histories.'}
      </p>`}
      ${moving && html`<label className="cc-field" htmlFor=${`${id}-destination`}>Destination
        <select id=${`${id}-destination`} value=${destination} disabled=${blocked} onChange=${event => setDestination(event.target.value)}>
          <option value="" disabled>Choose a project</option>
          ${projects.map(project => html`<option key=${project.id} value=${project.id}>${projectName(project)}</option>`)}
          <option value="new">New project…</option>
        </select>
      </label>`}
      ${needsName && html`<label className="cc-field" htmlFor=${`${id}-name`}>Project name
        <input id=${`${id}-name`} value=${name} disabled=${blocked} autoComplete="off" required
          aria-describedby=${`${id}-name-help`} onChange=${event => setName(event.target.value)}/>
      </label>`}
      ${needsName && html`<p className="cc-caption" id=${`${id}-name-help`}>Use 1–120 characters, without line breaks.</p>`}
      ${failure && html`<p role="alert" className="cc-error">${failure}</p>`}
      ${blocked && html`<p role="status" className="cc-caption">Wait for active or pending work to finish.</p>`}
      <footer className="cc-dialog-actions">
        <${Button} type="button" variant="secondary" disabled=${blocked} data-cc-initial-focus="true"
          onClick=${() => {if (!submission.isBlocked()) onClose?.();}}>Cancel<//>
        <${Button} type="submit" variant="primary" disabled=${blocked || !payload || !onSubmit}>${moving ? needsName ? 'Create project and move' : 'Move conversation' : 'Create project'}<//>
      </footer>
    </form>
  <//>`;
}

export function ProjectNavigation({projects = [], requests = [], selected = null, query = '', busy = false,
  onChoose, onCreateConversation, onCreateProject, onMoveConversation}) {
  const [dialog, setDialog] = useState(null), created = useRef(null), submission = useSubmission(busy);
  useEffect(() => {
    setDialog(previous => previous?.operation === 'move' && previous.request?.id !== selected ? null : previous);
  }, [selected]);
  const groups = groupConversations(projects, requests, query), current = requests.find(request => request.id === selected);
  const blocked = !!busy || submission.pending, moveBlocked = blocked || pendingTurns(current);
  const open = operation => {created.current = null; setDialog({operation, request:current});};
  const close = () => setDialog(null);
  const createConversation = projectId => submission.run(() => onCreateConversation?.(projectId));
  const saveProject = async payload => {
    let destination = payload.project_id;
    if (payload.name) {
      // Keep a successfully created destination when moving fails, so retry does not create duplicates.
      if (created.current?.name !== payload.name) {
        const result = await onCreateProject(payload);
        if (result === false) return false;
        const project = result?.project || result;
        if (dialog.operation === 'move' && !project?.id) throw new Error('The project was created without an ID. Refresh the projects before moving.');
        created.current = {name:payload.name, id:project?.id};
      }
      destination = created.current.id;
    }
    if (dialog.operation === 'move') {
      const result = await onMoveConversation(dialog.request.id, destination);
      if (result === false) return false;
    }
    setDialog(previous => previous === dialog ? null : previous);
    return true;
  };
  const rows = items => html`<ul className="cc-conversation-list">${items.map(request => html`<li key=${request.id}>
    <${Button} type="button" variant="text" size="sm" className=${`cc-conversation-row${selected === request.id ? ' is-current' : ''}`}
      aria-current=${selected === request.id ? 'page' : undefined} disabled=${blocked || !onChoose}
      onClick=${() => {if (!blocked) onChoose?.(request.id);}}>
      <span className="cc-conversation-name">${requestTitle(request)}</span>
      <span className="cc-conversation-meta">${pendingTurns(request) ? 'Active / pending' : request.output_count ? `${request.output_count} outputs` : 'Conversation'}</span>
    <//>
  </li>`)}</ul>`;
  return html`<div className="cc-navigation" aria-label="Projects and conversations" aria-busy=${blocked}>
    <div className="cc-navigation-actions">
      <${Button} type="button" variant="secondary" size="sm" disabled=${blocked || !onCreateConversation} onClick=${() => createConversation(null)}>New conversation<//>
      ${current && html`<${Button} type="button" variant="text" size="sm" disabled=${moveBlocked || !onMoveConversation || !onCreateProject}
        onClick=${() => {if (!moveBlocked) open('move');}}>Move current conversation<//>`}
    </div>
    ${submission.error && html`<p className="cc-error" role="alert">${submission.error}</p>`}
    <section className="cc-nav-section" aria-label="Projects">
      <div className="cc-section-heading"><h3>Projects</h3>
        <${Button} type="button" variant="text" size="sm" disabled=${blocked || !onCreateProject} onClick=${() => {if (!blocked) open('create');}}>New project<//>
      </div>
      ${groups.projects.map(({project, requests:items}) => html`<section key=${project.id} className="cc-project" aria-label=${projectName(project)}>
        <h4>${projectName(project)}</h4>${rows(items)}
        ${!items.length && html`<p className="cc-caption">No conversations yet.</p>`}
        <${Button} type="button" variant="text" size="sm" disabled=${blocked || !onCreateConversation}
          aria-label=${`New conversation in ${projectName(project)}`} onClick=${() => createConversation(project.id)}>New conversation<//>
      </section>`)}
      ${!groups.projects.length && html`<p className="cc-caption">${String(query).trim() ? 'No projects match.' : 'Create a project to group conversations.'}</p>`}
    </section>
    <section className="cc-nav-section" aria-label="Conversations without a project">
      <div className="cc-section-heading"><h3>Conversations</h3></div>${rows(groups.conversations)}
      ${!groups.conversations.length && html`<p className="cc-caption">${String(query).trim() ? 'No conversations match.' : 'Conversations without a project appear here.'}</p>`}
    </section>
    ${dialog && (dialog.operation === 'create' || dialog.request?.id === selected) && html`<${ProjectDialog}
      operation=${dialog.operation} projects=${projects} request=${dialog.operation === 'move' ? current : null}
      busy=${blocked} onSubmit=${saveProject} onClose=${close}/>`}
  </div>`;
}
