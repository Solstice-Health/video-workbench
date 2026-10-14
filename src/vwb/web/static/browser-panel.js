import htm from './vendor/htm.mjs';

const {createElement, useState, useEffect, useRef, useCallback, useId} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, Tooltip, MessageBar, SolsticeMark} = window.Eeva;
const post = body => ({method: 'POST', body: JSON.stringify(body)});
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const modifiers = event => (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
const mouseButton = event => ['left', 'middle', 'right'][event.button] || 'none';

// The image may be letterboxed and its encoded dimensions may differ from CSS
// viewport pixels. Cursor placement and input share these two transformations.
export function fittedFrame(boxWidth, boxHeight, imageWidth, imageHeight) {
  if (![boxWidth, boxHeight, imageWidth, imageHeight].every(n => Number.isFinite(n) && n > 0)) return null;
  const scale = Math.min(boxWidth / imageWidth, boxHeight / imageHeight);
  const width = imageWidth * scale, height = imageHeight * scale;
  return {left: (boxWidth - width) / 2, top: (boxHeight - height) / 2, width, height};
}

export function viewportPoint(x, y, rect, viewport, captured = false) {
  if (!rect || !viewport?.width || !viewport?.height) return null;
  const localX = x - rect.left, localY = y - rect.top;
  if (!captured && (localX < 0 || localY < 0 || localX > rect.width || localY > rect.height)) return null;
  return {x: clamp(localX / rect.width * viewport.width, 0, viewport.width - 1),
    y: clamp(localY / rect.height * viewport.height, 0, viewport.height - 1)};
}

export function browserUrl(value) {
  const text = value.trim();
  if (text === 'about:blank') return text;
  if (!text || /\s/.test(text)) throw new Error('Enter a website address, such as https://example.com.');
  const prefixed = /^[a-z][a-z\d+.-]*:/i.test(text) && !/^(localhost|127\.0\.0\.1):\d/i.test(text)
    ? text : `${/^(localhost|127\.0\.0\.1)(:|\/|$)/i.test(text) ? 'http' : 'https'}://${text}`;
  const url = new URL(prefixed);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Use an http or https website address.');
  return url.href;
}

export function appendInput(queue, event) {
  const previous = queue.at(-1);
  if (previous?.type === 'input_mouse' && event.type === 'input_mouse' && previous.eventType === event.eventType
      && previous.modifiers === event.modifiers) {
    if (event.eventType === 'mouseMoved') {queue[queue.length - 1] = event; return;}
    if (event.eventType === 'mouseWheel') {
      queue[queue.length - 1] = {...event, deltaX: clamp(previous.deltaX + event.deltaX, -10000, 10000), deltaY: clamp(previous.deltaY + event.deltaY, -10000, 10000)};
      return;
    }
  }
  queue.push(event);
}

function IconAction({name, label, ...props}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label} ...${props}>
    <${Icon} name=${name} size=${16}/><//><//>`;
}

export function BrowserPanel({api, requestId, onClose, notice}) {
  const [status, setStatus] = useState(null), [error, setError] = useState('');
  const [connection, setConnection] = useState('loading'), [busy, setBusy] = useState('');
  const [mode, setMode] = useState('headed'), [address, setAddress] = useState('');
  const [dialogText, setDialogText] = useState('');
  const [frame, setFrame] = useState(null), [decoded, setDecoded] = useState(false), [streamKey, setStreamKey] = useState(0);
  const [box, setBox] = useState({width: 0, height: 0}), [width, setWidth] = useState(58);
  const panel = useRef(null), stage = useRef(null), addressInput = useRef(null), controlButton = useRef(null), keyboardInput = useRef(null), frameImage = useRef(null);
  const stateRef = useRef(null), live = useRef(true), currentSession = useRef(null), inputEnabled = useRef(false);
  const chain = useRef(Promise.resolve()), inputQueue = useRef([]), inputTimer = useRef(null), inputFlight = useRef(false);
  const pressed = useRef({keys: new Map(), buttons: new Set(), point: {x: 0, y: 0}}), composing = useRef(false);
  const resize = useRef(null), addressEditing = useRef(false), previousTab = useRef(null);
  const helpId = useId(), panelId = useId();
  const base = `/api/requests/${encodeURIComponent(requestId || '')}/browser`;
  const running = status?.state === 'running', human = status?.owner === 'human';
  const editable = running && human && !busy && !status?.error && connection === 'connected';
  const viewport = status?.viewport;
  const dialog = status?.dialog;
  const fit = fittedFrame(box.width, box.height, frame?.width || viewport?.width, frame?.height || viewport?.height);
  inputEnabled.current = !!(editable && frame && decoded && fit && !error);
  stateRef.current = status;

  useEffect(() => setDialogText(dialog?.defaultPrompt || ''), [dialog?.type, dialog?.message, dialog?.defaultPrompt]);

  const acceptState = useCallback(next => {
    if (!live.current || !next || typeof next.state !== 'string') return;
    const old = stateRef.current;
    if (old?.session_id === next.session_id && Number.isFinite(old?.revision) && Number.isFinite(next.revision)
        && next.revision < old.revision) return;
    stateRef.current = next; setStatus(next);
    const tab = next.tabs?.find(t => t.active)?.id || null;
    if (next.session_id !== currentSession.current || (previousTab.current !== null && tab !== previousTab.current)) {
      setFrame(null); setDecoded(false); inputQueue.current = [];
      pressed.current = {keys: new Map(), buttons: new Set(), point: {x: 0, y: 0}};
    }
    currentSession.current = next.session_id; previousTab.current = tab;
    if (!addressEditing.current) setAddress(next.url === 'about:blank' ? '' : next.url || '');
    if (next.owner !== 'human') {
      inputQueue.current = [];
      pressed.current = {keys: new Map(), buttons: new Set(), point: {x: 0, y: 0}};
    }
  }, []);

  const refresh = useCallback(async () => {
    const next = await api(base); acceptState(next); return next;
  }, [api, base, acceptState]);
  const acknowledge = useCallback((viewerId, seq) => {
    if (!viewerId || !live.current) return;
    api(base, post({action: 'frame_ack', viewer_id: viewerId, seq})).catch(() => {
      if (live.current) {setConnection('reconnecting'); setStreamKey(key => key + 1);}
    });
  }, [api, base]);

  useEffect(() => {
    live.current = true;
    let timer, cancelled = false;
    const poll = async () => {
      try {await refresh(); if (!cancelled) setError(previous => previous.startsWith('Could not connect.') ? '' : previous);}
      catch (e) {if (!cancelled) {setError(`Could not connect. ${e.message}`); setConnection('error');}}
      if (!cancelled) timer = setTimeout(poll, 4000);
    };
    if (requestId) poll(); else setConnection('idle');
    return () => {cancelled = true; live.current = false; clearTimeout(timer); clearTimeout(inputTimer.current); inputQueue.current = [];};
  }, [requestId, refresh]);

  useEffect(() => {
    if (!running || !requestId) {setConnection('idle'); return;}
    let cancelled = false, lastSeq = -1, frameTarget = '';
    setConnection('connecting');
    const source = new EventSource(`${base}/stream`);
    source.onopen = () => {if (!cancelled) setConnection('connected');};
    source.addEventListener('state', event => {
      try {acceptState(JSON.parse(event.data));} catch {if (!cancelled) setError('The browser sent an unreadable update. Reconnect to try again.');}
    });
    source.addEventListener('frame', event => {
      if (cancelled) return;
      try {
        const next = JSON.parse(event.data);
        const now = stateRef.current, tabId = now?.tabs?.find(tab => tab.active)?.id;
        if ((next.session_id && next.session_id !== now?.session_id) || (next.tab_id && next.tab_id !== tabId)) {
          acknowledge(next.viewer_id, next.seq); return;
        }
        if (typeof next.data !== 'string' || !['jpeg', 'png', 'webp'].includes(next.format)) return;
        const target = `${next.session_id || ''}:${next.tab_id || tabId || ''}:${next.viewer_id || ''}`;
        if (target !== frameTarget) {lastSeq = -1; frameTarget = target;}
        if (Number.isFinite(next.seq) && next.seq <= lastSeq) {acknowledge(next.viewer_id, next.seq); return;}
        lastSeq = next.seq ?? lastSeq + 1;
        const metadata = next.metadata || {};
        setFrame({src: `data:image/${next.format};base64,${next.data}`, width: metadata.deviceWidth,
          height: metadata.deviceHeight, seq: lastSeq, viewerId: next.viewer_id});
        setConnection('connected');
      } catch {setError('The browser image could not be read. Reconnect to try again.');}
    });
    source.onerror = () => {if (!cancelled) {setConnection('reconnecting'); inputEnabled.current = false; inputQueue.current = [];}};
    return () => {cancelled = true; source.close();};
  }, [running, requestId, status?.session_id, base, streamKey, acceptState, acknowledge]);

  useEffect(() => {
    if (!stage.current) return;
    const observer = new ResizeObserver(entries => {
      const {width, height} = entries[0].contentRect; setBox({width, height});
    });
    observer.observe(stage.current);
    return () => observer.disconnect();
  }, [running]);

  const send = useCallback(body => {
    const pending = chain.current.catch(() => {}).then(async () => {
      if (!live.current) return null;
      if (body.action === 'input' && stateRef.current?.owner !== 'human') return null;
      const next = await api(base, post(body)); acceptState(next); return next;
    });
    chain.current = pending;
    return pending;
  }, [api, base, acceptState]);

  const flush = useCallback(async () => {
    clearTimeout(inputTimer.current); inputTimer.current = null;
    if (inputFlight.current || !inputQueue.current.length || !live.current) return;
    inputFlight.current = true;
    try {
      while (inputQueue.current.length && live.current) {
        const events = inputQueue.current.splice(0, 64);
        await send({action: 'input', events});
      }
    } catch (e) {
      inputQueue.current = []; inputEnabled.current = false;
      if (live.current) {setError(`Browser input stopped. ${e.message}`); refresh().catch(() => {});}
    } finally {inputFlight.current = false;}
  }, [send, refresh]);

  const enqueue = useCallback(events => {
    if (!inputEnabled.current) return;
    for (const event of events) appendInput(inputQueue.current, event);
    if (!inputTimer.current && !inputFlight.current) inputTimer.current = setTimeout(flush, 16);
  }, [flush]);

  const releasePressed = useCallback(() => {
    if (stateRef.current?.owner !== 'human') return;
    const held = pressed.current;
    for (const button of held.buttons) inputQueue.current.push({type: 'input_mouse', eventType: 'mouseReleased', ...held.point, button, clickCount: 1, modifiers: 0});
    for (const [key, values] of held.keys) inputQueue.current.push({type: 'input_keyboard', eventType: 'keyUp', key, ...values, modifiers: 0});
    held.buttons.clear(); held.keys.clear();
    flush();
  }, [flush]);

  useEffect(() => {
    const hidden = () => {if (document.hidden) releasePressed();};
    window.addEventListener('blur', releasePressed); document.addEventListener('visibilitychange', hidden);
    return () => {window.removeEventListener('blur', releasePressed); document.removeEventListener('visibilitychange', hidden);};
  }, [releasePressed]);

  useEffect(() => {
    if (connection !== 'connected') releasePressed();
  }, [connection, releasePressed]);

  const action = async (body, label = body.action) => {
    if (busy) return;
    setBusy(label); setError(''); inputEnabled.current = false;
    releasePressed();
    // Put all pending inputs onto the same request chain before ownership or
    // navigation changes, so releases cannot arrive after handing control back.
    while (inputQueue.current.length) await send({action: 'input', events: inputQueue.current.splice(0, 64)}).catch(() => {});
    try {await send(body);}
    catch (e) {if (live.current) setError(e.message);}
    finally {if (live.current) setBusy('');}
  };

  const point = (event, captured = false) => {
    if (!fit || !stage.current) return null;
    const rect = stage.current.getBoundingClientRect();
    return viewportPoint(event.clientX - rect.left, event.clientY - rect.top, fit, viewport, captured);
  };
  const pointer = (event, eventType) => {
    if (!inputEnabled.current) return;
    const down = eventType === 'mousePressed', up = eventType === 'mouseReleased';
    const location = point(event, stage.current.hasPointerCapture(event.pointerId) || up);
    if (!location) return;
    event.preventDefault();
    const button = down || up ? mouseButton(event) : 'none';
    if (down) {keyboardInput.current?.focus({preventScroll: true}); stage.current.setPointerCapture(event.pointerId); pressed.current.buttons.add(button);}
    if (up) {pressed.current.buttons.delete(button); if (stage.current.hasPointerCapture(event.pointerId)) stage.current.releasePointerCapture(event.pointerId);}
    pressed.current.point = location;
    enqueue([{type: 'input_mouse', eventType, ...location, button, clickCount: event.detail > 1 ? 2 : 1, modifiers: modifiers(event)}]);
  };

  useEffect(() => {
    const element = stage.current;
    if (!element) return;
    const wheel = event => {
      if (!inputEnabled.current) return;
      const location = point(event); if (!location) return;
      event.preventDefault();
      const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.height : 1;
      enqueue([{type: 'input_mouse', eventType: 'mouseWheel', ...location, button: 'none', clickCount: 1,
        deltaX: clamp(event.deltaX * scale, -10000, 10000), deltaY: clamp(event.deltaY * scale, -10000, 10000), modifiers: modifiers(event)}]);
    };
    element.addEventListener('wheel', wheel, {passive: false});
    return () => element.removeEventListener('wheel', wheel);
  }, [fit?.left, fit?.top, fit?.width, fit?.height, viewport?.width, viewport?.height, enqueue, running]);

  const keyboard = (event, down) => {
    if (!inputEnabled.current) return;
    if (down && event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); releasePressed(); controlButton.current?.focus(); return;
    }
    if (event.isComposing || composing.current) return;
    // Native paste delivers text through onPaste rather than a remote shortcut
    // that would paste unrelated contents of the remote machine's clipboard.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'v') return;
    const textKey = down && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
    if (!textKey) event.preventDefault();
    event.stopPropagation();
    const windowsVirtualKeyCode = clamp(event.keyCode || event.which || 0, 0, 255);
    const values = {code: event.code, windowsVirtualKeyCode};
    if (down) pressed.current.keys.set(event.key, values); else pressed.current.keys.delete(event.key);
    const events = [{type: 'input_keyboard', eventType: down ? 'keyDown' : 'keyUp', key: event.key, ...values, modifiers: modifiers(event)}];
    enqueue(events);
  };

  const bounds = () => {
    const available = panel.current?.parentElement?.clientWidth || window.innerWidth;
    return {min: Math.min(45, 400 / available * 100), max: Math.max(45, Math.min(78, (available - 320) / available * 100))};
  };
  const setPanelWidth = next => {const {min, max} = bounds(); setWidth(clamp(next, min, max));};
  const resizeKey = event => {
    const {min, max} = bounds();
    const next = {ArrowLeft: width + 2, ArrowRight: width - 2, Home: min, End: max}[event.key];
    if (next !== undefined) {event.preventDefault(); setPanelWidth(next);}
  };
  const tabKey = async (event, tab) => {
    const tabs = status.tabs || [], index = tabs.findIndex(item => item.id === tab.id);
    const next = {ArrowLeft: (index - 1 + tabs.length) % tabs.length, ArrowRight: (index + 1) % tabs.length,
      Home: 0, End: tabs.length - 1}[event.key];
    if (next === undefined || !editable) return;
    event.preventDefault();
    await action({action: 'tab', operation: 'select', tab_id: tabs[next].id}, 'Switching tab');
    requestAnimationFrame(() => panel.current?.querySelectorAll('[role="tab"]')[next]?.focus());
  };
  const navigate = event => {
    event.preventDefault(); if (!editable) return;
    try {addressEditing.current = false; action({action: 'navigate', url: browserUrl(address)}, 'Opening page');}
    catch (e) {setError(e.message); addressInput.current?.focus();}
  };
  useEffect(() => {
    if (!frame || !frameImage.current) return;
    let cancelled = false, paint, acknowledged = false;
    const acknowledgeFrame = () => {
      if (!acknowledged && live.current) {acknowledged = true; acknowledge(frame.viewerId, frame.seq);}
    };
    // Static pages often deliver identical JPEG bytes with a new sequence.
    // decode() also resolves for an unchanged src, so every painted sequence is
    // acknowledged without remounting the image or introducing a flicker.
    frameImage.current.decode().then(() => {
      if (cancelled || !live.current) return;
      setDecoded(true);
      paint = requestAnimationFrame(() => {
        if (!cancelled && live.current) acknowledgeFrame();
      });
    }).catch(() => {
      if (!cancelled && live.current) {setDecoded(false); setError('The browser image could not load. Reconnect to try again.');}
    });
    return () => {
      cancelled = true; cancelAnimationFrame(paint);
      // A tab switch can retire an image before decode/paint finishes. It must
      // still release the old in-flight frame so the new tab can start streaming.
      acknowledgeFrame();
    };
  }, [frame, acknowledge, !!fit]);
  const textInput = text => {
    if (!text) return;
    const events = [];
    for (let start = 0; start < text.length; start += 10000)
      events.push({type: 'input_keyboard', eventType: 'char', key: 'Unidentified', code: '', text: text.slice(start, start + 10000), modifiers: 0});
    enqueue(events);
  };
  const reconnect = () => {setError(''); setStreamKey(k => k + 1); refresh().catch(e => setError(e.message));};
  const browserError = error || status?.error;
  let bar = browserError ? {tone: 'error', text: `${typeof browserError === 'string' ? browserError : browserError.message || 'The browser could not connect.'}`}
    : !requestId ? {tone: 'info', text: 'Choose a conversation to open its browser.'}
    : !status ? {tone: 'progress', text: 'Connecting to the browser service…'}
    : status.available === false ? {tone: 'info', text: 'The browser is not available on this workbench.'}
    : busy ? {tone: 'progress', text: `${busy === 'control' ? 'Changing browser control' : busy}…`}
    : !running ? {tone: 'info', text: 'Open a browser for this conversation when you need it.'}
    : connection === 'reconnecting' || connection === 'error' ? {tone: 'error', text: 'The live view disconnected. Reconnecting; input is paused.'}
    : connection !== 'connected' ? {tone: 'progress', text: 'Connecting the live browser view…'}
    : {tone: 'info', text: human ? 'You control the browser. Return it when you are done.' : 'The agent controls the browser. Take control to interact.'};
  if (notice?.tone === 'error' && !browserError) bar = notice;
  const cursor = status?.cursor;
  const cursorVisible = running && !human && cursor?.visible && fit && viewport?.width > 0 && viewport?.height > 0
    && Number.isFinite(cursor.x) && Number.isFinite(cursor.y) && frame && decoded;
  const cursorStyle = cursorVisible ? {left: `${fit.left + clamp(cursor.x / viewport.width, 0, 1) * fit.width}px`,
    top: `${fit.top + clamp(cursor.y / viewport.height, 0, 1) * fit.height}px`} : null;

  return html`<div className="browser-panel-shell" ref=${panel} style=${{'--wb-browser-width': `${width}%`}}>
    <div className="browser-resize-handle" role="separator" aria-label="Resize browser panel" aria-orientation="vertical"
      aria-controls=${panelId} aria-valuemin=${Math.round(bounds().min)} aria-valuemax=${Math.round(bounds().max)} aria-valuenow=${Math.round(width)} aria-valuetext=${`${Math.round(width)} percent of workspace`} tabIndex="0"
      onKeyDown=${resizeKey} onDoubleClick=${() => setPanelWidth(58)}
      onPointerDown=${event => {event.preventDefault(); resize.current = panel.current.parentElement.getBoundingClientRect(); event.currentTarget.setPointerCapture(event.pointerId);}}
      onPointerMove=${event => {if (resize.current) setPanelWidth((resize.current.right - event.clientX) / resize.current.width * 100);}}
      onPointerUp=${event => {resize.current = null; event.currentTarget.releasePointerCapture(event.pointerId);}}
      onPointerCancel=${() => {resize.current = null;}}></div>
    <aside className="browser-panel" id=${panelId} aria-label="Conversation browser">
      <div className="browser-heading"><div><h2>Browser</h2><span className="caption">${running ? `${status.mode === 'remote' ? 'Remote' : status.mode === 'headless' ? 'Headless' : 'Headed'} session` : 'Your conversation’s browser'}</span></div>
        <div className="browser-heading-actions"><${IconAction} name="panel-right-close" label="Hide browser panel" onClick=${onClose}/>
          ${running && html`<${Button} variant="text" size="sm" disabled=${!!busy} onClick=${() => action({action: 'close'}, 'Closing browser')}>Close browser<//>`}</div></div>
      <div className="browser-status" aria-live="polite"><${MessageBar} tone=${bar.tone}>${bar.text}
        ${(browserError || connection === 'reconnecting') && requestId && html` <button className="text-action" onClick=${reconnect}>Reconnect</button>`}<//></div>
      ${running && dialog && html`<section className="browser-dialog" aria-label="Message from this web page">
        <h3>Message from this page</h3><p>${dialog.message || 'This page is waiting for a response.'}</p>
        ${dialog.type === 'prompt' && html`<label><span>Your response</span><input value=${dialogText} disabled=${!editable} maxLength="10000" onChange=${event => setDialogText(event.target.value)}/></label>`}
        <div><${Button} variant="text" size="sm" disabled=${!editable} onClick=${() => action({action: 'dialog', response: 'dismiss'}, 'Dismissing message')}>Dismiss<//>
          <${Button} variant="secondary" size="sm" disabled=${!editable} onClick=${() => action({action: 'dialog', response: 'accept', ...(dialog.type === 'prompt' ? {text: dialogText} : {})}, 'Responding to page')}>Accept<//></div>
      </section>`}
      ${running ? html`
        <div className="browser-tabs" role="tablist" aria-label="Browser tabs">
          ${(status.tabs || []).map(tab => html`<div className=${`browser-tab ${tab.active ? 'active' : ''}`} key=${tab.id}>
            <button role="tab" aria-selected=${!!tab.active} aria-label=${tab.title || tab.url || 'New tab'} disabled=${!editable} title=${tab.title || tab.url}
              tabIndex=${tab.active ? 0 : -1} onKeyDown=${event => tabKey(event, tab)}
              onClick=${() => action({action: 'tab', operation: 'select', tab_id: tab.id}, 'Switching tab')}>${tab.title || (tab.url === 'about:blank' ? 'New tab' : tab.url) || 'New tab'}</button>
            <${IconAction} name="x" label=${`Close ${tab.title || 'tab'}`} disabled=${!editable} onClick=${() => action({action: 'tab', operation: 'close', tab_id: tab.id}, 'Closing tab')}/>
          </div>`)}
          <${IconAction} name="plus" label="New browser tab" disabled=${!editable} onClick=${() => action({action: 'tab', operation: 'new', url: 'about:blank'}, 'Opening tab')}/>
        </div>
        <div className="browser-toolbar"><div className="browser-navigation">
          <${IconAction} name="chevron-left" label="Back" disabled=${!editable} onClick=${() => action({action: 'history', direction: 'back'}, 'Going back')}/>
          <${IconAction} name="chevron-right" label="Forward" disabled=${!editable} onClick=${() => action({action: 'history', direction: 'forward'}, 'Going forward')}/>
          <${IconAction} name="refresh" label="Reload page" disabled=${!editable} onClick=${() => action({action: 'reload'}, 'Reloading page')}/>
        </div><form className="browser-address" onSubmit=${navigate}>
          <${Icon} name="link" size=${14}/><input ref=${addressInput} aria-label="Website address" value=${address} disabled=${!editable} placeholder="Enter a website address"
            autoComplete="off" autoCapitalize="none" spellCheck=${false} onFocus=${() => {addressEditing.current = true;}}
            onBlur=${() => {addressEditing.current = false;}} onChange=${event => setAddress(event.target.value)}/>
          <${IconAction} name="arrow-up" label="Open address" type="submit" disabled=${!editable || !address.trim()}/>
        </form></div>
        <div className="browser-ownership"><span><${Icon} name=${human ? 'pen' : 'info'} size=${14}/>${human ? 'You are in control' : 'View only'}</span>
          <${Button} ref=${controlButton} variant="secondary" size="sm" disabled=${!!busy || connection !== 'connected'}
            onClick=${() => action({action: 'control', owner: human ? 'agent' : 'human'}, 'control')}>${human ? 'Return to agent' : 'Take control'}<//></div>
        <div className=${`browser-stage ${human ? 'human-control' : 'agent-control'} ${connection === 'connected' ? '' : 'disconnected'}`}
          ref=${stage} role=${human ? 'application' : 'img'} tabIndex=${human && decoded ? 0 : -1}
          aria-label=${human ? 'Interactive browser. Press Escape to return focus to browser controls.' : `Agent browser view${status.title ? `: ${status.title}` : ''}`}
          aria-describedby=${helpId} onKeyDown=${event => keyboard(event, true)} onKeyUp=${event => keyboard(event, false)}
          onFocus=${event => {if (event.target === event.currentTarget && human) keyboardInput.current?.focus({preventScroll: true});}}
          onBlur=${event => {if (!event.currentTarget.contains(event.relatedTarget)) releasePressed();}} onPointerDown=${event => pointer(event, 'mousePressed')} onPointerUp=${event => pointer(event, 'mouseReleased')}
          onPointerMove=${event => pointer(event, 'mouseMoved')} onPointerCancel=${releasePressed}
          onContextMenu=${event => {if (human) event.preventDefault();}}
          onCompositionStart=${() => {composing.current = true;}} onCompositionEnd=${event => {composing.current = false; textInput(event.currentTarget.querySelector('textarea')?.value || event.data); if (keyboardInput.current) keyboardInput.current.value = '';}}
          onPaste=${event => {if (!inputEnabled.current) return; event.preventDefault(); textInput(event.clipboardData.getData('text/plain'));}}>
          ${human && html`<textarea ref=${keyboardInput} className="browser-keyboard-input" tabIndex="-1" aria-label="Browser keyboard input" aria-describedby=${helpId}
            autoComplete="off" autoCapitalize="none" spellCheck=${false}
            onInput=${event => {if (composing.current || event.nativeEvent.isComposing) return; textInput(event.currentTarget.value); event.currentTarget.value = '';}}/>`}
          ${frame && fit && html`<img ref=${frameImage} className="browser-frame" src=${frame.src} alt="" draggable=${false} style=${{left: fit.left, top: fit.top, width: fit.width, height: fit.height}}
            onError=${() => {setDecoded(false); setError('The browser image could not load. Reconnect to try again.');}}/>`}
          ${(!frame || !decoded) && html`<div className="browser-view-placeholder"><${SolsticeMark} size=${24}/><p>Waiting for the browser image…</p><span>The live page appears when the browser sends a frame.</span></div>`}
          ${cursorVisible && html`<span className="browser-agent-cursor" style=${cursorStyle} aria-hidden="true"><${SolsticeMark} size=${18}/><span>Agent</span></span>`}
        </div>
        <div className="browser-footer"><span id=${helpId}>${human ? 'Click the page to type. Escape returns to controls.' : 'Live view. The mark follows the agent’s cursor.'}</span><span>${connection === 'connected' ? 'Connected' : 'Reconnecting'}</span></div>
      ` : html`<div className="browser-start"><${Icon} name="link" size=${18}/><h3>A browser beside your work</h3>
        <p>Follow the agent as it browses. Take control whenever you need to interact with a page.</p>
        <label className="browser-mode"><span>Browser mode</span><select value=${mode} onChange=${event => setMode(event.target.value)} disabled=${!!busy}>
          <option value="headed">Headed</option><option value="headless">Headless</option>
          ${status?.remote_available && html`<option value="remote">Remote browser</option>`}</select></label>
        <p className="browser-mode-help">${mode === 'remote' ? 'Connects to the browser configured by your workbench host. The live view stays here.' : mode === 'headed' ? 'Opens a visible browser window with a live view here.' : 'Runs without a separate window. The live view stays here.'}</p>
        <${Button} variant="secondary" size="md" disabled=${!requestId || !status || status.available === false || !!busy}
          loading=${!!busy} onClick=${() => action({action: 'start', mode}, 'Opening browser')}>Open browser<//>
      </div>`}
    </aside>
  </div>`;
}
