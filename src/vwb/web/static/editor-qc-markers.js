import htm from './vendor/htm.mjs';

const {createElement, useRef, useState} = window.React;
const {Icon, Button} = window.Eeva;
const html = htm.bind(createElement);
const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const positive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
const outputId = output => output?.id || output?.output;

function validTarget(target) {
  return target && typeof target.output === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(target.output)
    && !target.output.includes('..') && Number.isSafeInteger(target.version) && target.version > 0
    && target.target === `output:${target.output}@v${target.version}`;
}

export function verifiedOutputTarget(output) {
  const target = output?.qc_target;
  return validTarget(target) && target.output === outputId(output) && target.version === output.version
    && typeof output.video_url === 'string' && !!output.video_url && target.url === output.video_url
    && target.file === `renders/${target.output}/v${target.version}/${target.output}_v${target.version}.mp4`
    && digest(target.sha256) && digest(target.canvas_file_sha256)
    && positive(target.fps) && Number.isSafeInteger(target.frames) && target.frames > 0 ? target : null;
}

export function editorVideoURL(output) {
  const target = verifiedOutputTarget(output);
  // A re-render at the same path must reload the video and its duration before
  // any finding can seek. Both file hashes also participate in the React key.
  return target ? `${output.video_url}${output.video_url.includes('?') ? '&' : '?'}qc_pin=${target.sha256}.${target.canvas_file_sha256}` : output?.video_url;
}

export function editorMediaKey(output) {
  return JSON.stringify([outputId(output), output?.version, editorVideoURL(output)]);
}

export function findingSeekTime(item, output, duration) {
  const anchor = item?.anchor, target = verifiedOutputTarget(output);
  if (item?.lane !== 'shared' || !validTarget(anchor) || !target || !positive(duration)) return null;
  for (const field of ['target', 'output', 'version', 'file', 'sha256', 'canvas_file_sha256', 'frames', 'fps']) {
    if (anchor[field] !== target[field]) return null;
  }
  const {f0, f1, fps, frames} = anchor;
  if (!Number.isSafeInteger(f0) || !Number.isSafeInteger(f1) || f0 < 0 || f1 <= f0 || f1 > frames) return null;
  // The last selected frame must exist in the loaded media. A shortened final
  // frame is valid; seeking to the frame boundary or duration itself is not.
  if ((f1 - 1) / fps >= duration) return null;
  const start = f0 / fps, end = Math.min((f0 + 1) / fps, duration);
  const time = start + (end - start) / 2;
  return time > start && time < end ? time : null;
}

export function editorFindings(findings, output, duration) {
  const groups = {current: [], older: [], other: []};
  if (!Array.isArray(findings) || !output) return groups;
  for (const item of findings) {
    // This component consumes only the editor snapshot, never QC chat state.
    // Fail closed if an accidental mixed-lane payload reaches the UI.
    if (item?.lane !== 'shared' || typeof item.id !== 'string' || !validTarget(item.anchor)
        || item.anchor.output !== outputId(output)) continue;
    const row = {item, time: findingSeekTime(item, output, duration)};
    const group = item.anchor.version === output.version ? 'current' : item.anchor.version < output.version ? 'older' : 'other';
    groups[group].push(row);
  }
  return groups;
}

function loadedVideo(video, output) {
  if (!video || video.readyState < 1 || video.error || video.getAttribute('src') !== editorVideoURL(output)) return false;
  const base = globalThis.document?.baseURI || 'http://localhost/';
  return !!video.currentSrc && new URL(video.currentSrc, base).href === new URL(editorVideoURL(output), base).href;
}

export function seekFinding(video, item, output) {
  if (!loadedVideo(video, output)) return false;
  const time = findingSeekTime(item, output, video.duration);
  if (time === null) return false;
  video.pause();
  video.currentTime = time;
  return true;
}

const frameLabel = anchor => Number.isSafeInteger(anchor.f0) && Number.isSafeInteger(anchor.f1)
  ? anchor.f1 === anchor.f0 + 1 ? `frame ${anchor.f0}` : `frames ${anchor.f0}–${anchor.f1 - 1}` : 'frame unavailable';
const label = item => `Seek to QC finding ${item.id}, version ${item.anchor.version}, ${frameLabel(item.anchor)}`;

export function EditorQCMarkers({findings, output, duration = 0, onSeek}) {
  const groups = editorFindings(findings, output, duration);
  if (![...groups.current, ...groups.older, ...groups.other].length) return null;
  const markers = groups.current.filter(row => row.time !== null);
  const list = (rows, title) => rows.length ? html`<div className="editor-qc-group"><h4>${title}</h4>
    <ul>${rows.map(({item, time}) => html`<li key=${item.id}>
      <div className="editor-qc-finding-heading"><span>Version ${item.anchor.version} · ${frameLabel(item.anchor)}</span>
        ${time !== null && html`<${Button} type="button" variant="text" size="sm" aria-label=${label(item)} onClick=${() => onSeek(item)}>Seek<//>`}
      </div>
      <p>${typeof item.observed === 'string' ? item.observed : ''}</p>
      ${typeof item.expected === 'string' && html`<details><summary>Expected</summary><p>${item.expected}</p></details>`}
      ${time === null && item.anchor.version === output.version && html`<span className="editor-qc-unavailable">Seek unavailable until this render and frame range are verified.</span>`}
    </li>`)}</ul></div>` : null;
  return html`<section className="editor-qc-markers" aria-label="QC findings sent to editor">
    <h3>QC handoffs</h3><p className="editor-qc-caption">Findings as sent to the editor.</p>
    ${markers.length > 0 && html`<div className="editor-qc-timeline" role="group" aria-label=${`QC timeline markers for version ${output.version}`}>
      ${markers.map(({item, time}) => {
        const start = item.anchor.f0 / item.anchor.fps / duration * 100;
        const end = Math.min(item.anchor.f1 / item.anchor.fps / duration * 100, 100);
        return html`<button type="button" key=${item.id} className="editor-qc-marker" aria-label=${label(item)}
          title=${`Version ${item.anchor.version} · ${frameLabel(item.anchor)}`} onClick=${() => onSeek(item)}
          style=${{left: `${start}%`, width: `${end - start}%`}}><span className="editor-qc-marker-dot" style=${{left: `${(time / duration * 100 - start) / (end - start) * 100}%`}}/></button>`;
      })}
    </div>`}
    ${list(groups.current, 'This version')}
    ${list(groups.older, 'Older-version findings')}
    ${list(groups.other, 'Other-version findings')}
  </section>`;
}

export function EditorOutputPreview({output, findings}) {
  const video = useRef(null), [duration, setDuration] = useState(0);
  const refreshDuration = event => setDuration(loadedVideo(event.currentTarget, output) ? event.currentTarget.duration : 0);
  return html`<div className="editor-output-preview">
    <div className="preview-stage">${output.video_url ? html`<video ref=${video} controls playsInline preload="metadata"
      poster=${output.thumbnail_url || undefined} src=${editorVideoURL(output)} aria-label=${`Preview ${outputId(output)}`}
      onLoadedMetadata=${refreshDuration} onDurationChange=${refreshDuration} onEmptied=${() => setDuration(0)} onError=${() => setDuration(0)}/>`
      : html`<div className="empty-preview"><${Icon} name="file" size=${18}/><p>The preview appears after rendering.</p></div>`}</div>
    <${EditorQCMarkers} findings=${findings} output=${output} duration=${duration}
      onSeek=${item => seekFinding(video.current, item, output)}/>
  </div>`;
}
