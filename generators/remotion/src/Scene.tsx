import React from "react";
import { AbsoluteFill, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig, delayRender, continueRender } from "remotion";
import { loadFont } from "@remotion/fonts";
import { measureText } from "@remotion/layout-utils";

// A generic renderer for the text, caption and shape layers of a vwb.canvas/2 file.
// It contains no layout and no house style: every position, size, font, colour and motion comes
// from the canvas the agent wrote. Strings come only from the copy deck (resolved by the harness
// into `text`). Text is never truncated: type shrinks to fit its box, down to the layer's min_size.
import manifest from "../public/fonts/manifest.json";

type Key = { f: number; v: any; ease?: string };
type Anim<T> = T | Key[];
type Box = [number, number, number, number];

type TextStyle = {
  font: string; weight: number; size: number; min_size?: number; color: Anim<string>; italic?: boolean;
  align: "left" | "center" | "right"; valign: "top" | "middle" | "bottom"; line_height: number;
  letter_spacing: number; uppercase: boolean; shadow: boolean;
  stroke?: { width: number; color: string }; background?: string; padding: number; radius: number;
  accent_bar?: string; highlight?: string; muted?: string;
};

type Base = { id: string; span?: [number, number]; opacity?: Anim<number>; rotate?: Anim<number> };
type TextLayer = Base & { type: "text"; copy_id: string; text: string; box: Anim<Box>; style: TextStyle;
  animate: { enter: string; exit: string; frames: number } };
type Word = { text: string; from: number; to: number };
type Page = { copy_id: string; from: number; to: number; lines: Word[][] };
type CaptionsLayer = Base & { type: "captions"; box: Anim<Box>; style: TextStyle; mode: "karaoke" | "plain"; pages: Page[] };
type Stroke = { width: number; color: Anim<string>; dash?: number[]; cap?: "butt" | "round" | "square" };
type PathSpec = { d: string; fill?: string; stroke?: Stroke; opacity?: number; translate?: [number, number] };
type ShapeLayer = Base & { type: "shape"; shape: "rect" | "circle" | "line" | "path" | "svg"; box: Anim<Box>; fill?: Anim<string>;
  stroke?: Stroke; radius: number; gradient?: string[]; gradient_angle?: number; d?: string; viewbox?: [number, number];
  draw?: Anim<number>; progress: boolean; paths?: PathSpec[] };
type Layer = TextLayer | CaptionsLayer | ShapeLayer;

export type SceneProps = { width: number; height: number; fps: number; durationInFrames: number; layers: Layer[];
  offset?: number; total?: number };

// A plate covers only the output frames where its layers show: local frame + offset = output frame.
const Clock = React.createContext({ offset: 0, total: 0 });
const useFrame = () => useCurrentFrame() + React.useContext(Clock).offset;
const useTotal = () => React.useContext(Clock).total;

export const defaultSceneProps: SceneProps = {
  width: 1080, height: 1920, fps: 30, durationInFrames: 90,
  layers: [{ type: "text", id: "t", copy_id: "x", text: "A scene layer with a reference^{1}",
    box: [60, 500, 960, 200], opacity: 1,
    style: { font: "Inter", weight: 700, size: 64, color: "#FFFFFF", align: "left", valign: "top",
      line_height: 1.2, letter_spacing: 0, uppercase: false, shadow: false, padding: 0, radius: 0 },
    animate: { enter: "none", exit: "none", frames: 12 } }],
};

// ---------------------------------------------------------------- fonts (the pinned library, local files)
type Face = { weight: number; style: "normal" | "italic"; file: string };
const fontHandle = delayRender("fonts");
Promise.all((manifest as { family: string; faces: Face[] }[]).flatMap((m) => m.faces.map((face) =>
  loadFont({ family: m.family, url: staticFile(`fonts/${face.file}`), weight: String(face.weight), style: face.style }),
))).then(() => continueRender(fontHandle)).catch((e) => { console.error(e); continueRender(fontHandle); });

// ---------------------------------------------------------------- keyframes (mirrors vwb/canvas/anim.py)
const ease = (t: number, kind = "inout"): number => {
  t = Math.min(1, Math.max(0, t));
  if (kind === "hold") return 0;
  if (kind === "linear") return t;
  if (kind === "in") return t * t * t;
  if (kind === "out") return 1 - Math.pow(1 - t, 3);
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
};
const isKeyed = (v: any): v is Key[] => Array.isArray(v) && v.length > 0 && typeof v[0] === "object" && v[0] !== null && "f" in v[0];
const rgba = (h: string): number[] => {
  const v = h.replace("#", "").padEnd(8, "F").slice(0, 8);
  return [0, 2, 4, 6].map((i) => parseInt(v.slice(i, i + 2), 16));
};
const hex = (c: number[]) => "#" + c.map((x) => Math.max(0, Math.min(255, Math.round(x))).toString(16).padStart(2, "0")).join("");
function valueAt<T>(v: Anim<T> | undefined, f: number, fallback: T): T {
  if (v === undefined || v === null) return fallback;
  if (!isKeyed(v)) return v as T;
  const k = v as Key[];
  if (f <= k[0].f) return k[0].v;
  for (let i = 0; i + 1 < k.length; i++) {
    const a = k[i], b = k[i + 1];
    if (a.f <= f && f < b.f) {
      if (b.ease === "hold") return a.v;
      const u = ease((f - a.f) / Math.max(1, b.f - a.f), b.ease ?? "inout");
      if (typeof a.v === "number") return (a.v + (b.v - a.v) * u) as T;
      if (typeof a.v === "string") { const x = rgba(a.v), y = rgba(b.v); return hex(x.map((c, j) => c + (y[j] - c) * u)) as T; }
      return (a.v as number[]).map((x, j) => x + ((b.v as number[])[j] - x) * u) as T;
    }
  }
  return k[k.length - 1].v;
}
const rotation = (l: Base, f: number) => { const r = valueAt<number>(l.rotate, f, 0); return r ? ` rotate(${r}deg)` : ""; };

// ---------------------------------------------------------------- text helpers
const SUP = /\^\{([^}]*)\}/g;
const plain = (t: string) => t.replace(SUP, "$1");
const Rich: React.FC<{ text: string }> = ({ text }) => {
  const parts: React.ReactNode[] = [];
  let last = 0, i = 0;
  let m: RegExpExecArray | null;
  SUP.lastIndex = 0;
  while ((m = SUP.exec(text)) !== null) {
    if (m.index > last) parts.push(<span key={i++}>{text.slice(last, m.index)}</span>);
    parts.push(<sup key={i++} style={{ fontSize: "0.55em", verticalAlign: "0.85em", lineHeight: 0 }}>{m[1]}</sup>);
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<span key={i++}>{text.slice(last)}</span>);
  return <>{parts}</>;
};

const lines = (text: string, size: number, s: TextStyle, maxW: number): number => {
  const words = plain(s.uppercase ? text.toUpperCase() : text).split(/\s+/).filter(Boolean);
  const m = (t: string) => measureText({ text: t, fontFamily: s.font, fontSize: size, fontWeight: String(s.weight),
    letterSpacing: `${s.letter_spacing ?? 0}px` }).width * (s.italic ? 1.02 : 1);
  const space = m(" ");
  let n = 1, w = 0;
  for (const word of words) {
    const ww = m(word);
    if (w > 0 && w + space + ww > maxW) { n += 1; w = ww; } else { w += (w > 0 ? space : 0) + ww; }
  }
  return n;
};
const fit = (text: string, s: TextStyle, w: number, h: number): number => {
  let size = s.size;
  const pad = 2 * (s.padding || 0);
  const bar = s.accent_bar ? 22 : 0;
  const floor = s.min_size ?? Math.round(s.size / 2);
  while (size > floor && lines(text, size, s, w - pad - 4) * size * s.line_height > h - pad - bar) size -= 1;
  return size;
};
const shadowCss = (on: boolean) => on ? "0 0 2px rgba(0,0,0,0.9), 0 3px 12px rgba(0,0,0,0.6)" : undefined;
const alignItems = (v: string) => (v === "top" ? "flex-start" : v === "bottom" ? "flex-end" : "center");

const visible = (l: Base, f: number) => !l.span || (l.span[0] <= f && f < l.span[1]);

// ---------------------------------------------------------------- layers
const TextView: React.FC<{ l: TextLayer }> = ({ l }) => {
  const f = useFrame();
  const durationInFrames = useTotal();
  const { fps } = useVideoConfig();
  if (!visible(l, f)) return null;
  const [x, y, w, h] = valueAt<Box>(l.box, f, [0, 0, 100, 100]);
  const s = l.style;
  const start = l.span ? l.span[0] : 0;
  const end = l.span ? l.span[1] : durationInFrames;
  const n = Math.max(1, l.animate?.frames ?? 12);
  const tIn = Math.min(1, (f - start) / n);
  const tOut = Math.min(1, Math.max(0, (end - f) / n));
  const enter = l.animate?.enter ?? "fade";
  const exit = l.animate?.exit ?? "none";
  const pop = spring({ frame: f - start, fps, config: { damping: 14, stiffness: 180, mass: 0.6 } });
  let opacity = valueAt<number>(l.opacity, f, 1);
  let transform = rotation(l, f);
  let clip: string | undefined;
  let text = l.text;
  if (enter === "fade") opacity *= ease(tIn, "out");
  if (enter === "rise") { opacity *= ease(tIn, "out"); transform += ` translateY(${(1 - ease(tIn, "out")) * 28}px)`; }
  if (enter === "pop") { opacity *= Math.min(1, pop * 1.5); transform += ` scale(${interpolate(pop, [0, 1], [0.82, 1])})`; }
  if (enter === "wipe") clip = `inset(0 ${(1 - ease(tIn, "out")) * 100}% 0 0)`;
  if (enter === "type") {
    const p = plain(l.text);
    const shown = Math.ceil(p.length * Math.min(1, (f - start) / Math.max(1, n * 3)));
    text = shown >= p.length ? l.text : p.slice(0, shown);
  }
  if (exit === "fade") opacity *= ease(tOut, "in");
  if (exit === "sink") { opacity *= ease(tOut, "in"); transform += ` translateY(${(1 - ease(tOut, "in")) * 24}px)`; }
  const size = fit(l.text, s, w, h);
  return (
    <div style={{ position: "absolute", left: x, top: y, width: w, height: h, display: "flex", flexDirection: "column",
      justifyContent: alignItems(s.valign), opacity, transform, clipPath: clip }}>
      {s.accent_bar ? <div style={{ width: 72, height: 7, borderRadius: 4, background: s.accent_bar, marginBottom: 14,
        alignSelf: s.align === "center" ? "center" : s.align === "right" ? "flex-end" : "flex-start" }} /> : null}
      <div data-copy-id={l.copy_id} style={{ fontFamily: s.font, fontWeight: s.weight, fontSize: size, lineHeight: s.line_height,
        fontStyle: s.italic ? "italic" : "normal",
        color: valueAt<string>(s.color, f, "#000000"), textAlign: s.align, letterSpacing: s.letter_spacing,
        textTransform: s.uppercase ? "uppercase" : "none",
        textShadow: shadowCss(s.shadow), background: s.background, padding: s.padding, borderRadius: s.radius,
        WebkitTextStroke: s.stroke ? `${s.stroke.width}px ${s.stroke.color}` : undefined, paintOrder: "stroke fill",
        alignSelf: s.background ? (s.align === "center" ? "center" : s.align === "right" ? "flex-end" : "flex-start") : undefined }}>
        <Rich text={text} />
      </div>
    </div>
  );
};

const CaptionsView: React.FC<{ l: CaptionsLayer }> = ({ l }) => {
  const f = useFrame();
  const { fps } = useVideoConfig();
  if (!visible(l, f)) return null;
  const page = l.pages.find((p) => f >= p.from && f < p.to);
  if (!page) return null;
  const [x, y, w, h] = valueAt<Box>(l.box, f, [0, 0, 100, 100]);
  const s = l.style;
  const pop = spring({ frame: f - page.from, fps, config: { damping: 18, stiffness: 220, mass: 0.5 } });
  const widest = Math.max(...page.lines.map((ln) => measureText({ text: ln.map((wd) => wd.text).join(" "),
    fontFamily: s.font, fontSize: s.size, fontWeight: String(s.weight) }).width * 1.08));
  const size = Math.max(s.min_size ?? Math.round(s.size / 2), Math.min(s.size, Math.floor(s.size * (w - 8) / Math.max(1, widest))));
  const color = valueAt<string>(s.color, f, "#000000");
  return (
    <div data-copy-id={page.copy_id} style={{ position: "absolute", left: x, top: y, width: w, height: h, display: "flex",
      flexDirection: "column", justifyContent: alignItems(s.valign), alignItems: s.align === "center" ? "center" : s.align === "right" ? "flex-end" : "flex-start",
      opacity: valueAt<number>(l.opacity, f, 1) * Math.min(1, pop * 1.4),
      transform: `scale(${interpolate(pop, [0, 1], [0.93, 1])})${rotation(l, f)}` }}>
      <div style={{ background: s.background, padding: s.padding, borderRadius: s.radius, textAlign: s.align }}>
        {page.lines.map((ln, li) => (
          <div key={li} style={{ fontFamily: s.font, fontWeight: s.weight, fontSize: size, lineHeight: s.line_height,
            whiteSpace: "nowrap", fontStyle: s.italic ? "italic" : "normal" }}>
            {ln.map((wd, wi) => {
              const on = l.mode === "karaoke" && f >= wd.from && f < wd.to;
              const said = l.mode !== "karaoke" || f >= wd.from;
              return (
                <span key={wi} style={{ color: on ? (s.highlight ?? color) : said ? color : (s.muted ?? color),
                  textShadow: shadowCss(s.shadow), display: "inline-block", margin: "0 0.14em",
                  transform: on ? "scale(1.06)" : "none", textTransform: s.uppercase ? "uppercase" : "none",
                  WebkitTextStroke: s.stroke ? `${s.stroke.width}px ${s.stroke.color}` : undefined, paintOrder: "stroke fill" }}>
                  {wd.text}
                </span>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
};

const ShapeView: React.FC<{ l: ShapeLayer }> = ({ l }) => {
  const f = useFrame();
  const durationInFrames = useTotal();
  if (!visible(l, f)) return null;
  const [x, y, w0, h] = valueAt<Box>(l.box, f, [0, 0, 100, 100]);
  const w = l.progress ? w0 * Math.min(1, (f + 1) / durationInFrames) : w0;
  const fill = valueAt<string | undefined>(l.fill, f, undefined);
  const opacity = valueAt<number>(l.opacity, f, 1);
  const transform = rotation(l, f) || undefined;
  const strokeColor = l.stroke ? valueAt<string>(l.stroke.color, f, "#000000") : undefined;
  if (l.shape === "svg" || (l.shape === "path" && l.paths && l.paths.length)) {
    // several paths in one coordinate space: traced vector assets, or agent-drawn groups (3D bar faces, icons)
    const [vw, vh] = l.viewbox ?? [w0, h];
    const drawn = Math.min(1, Math.max(0, valueAt<number>(l.draw, f, 1)));
    const all: PathSpec[] = [...(l.d ? [{ d: l.d, fill: undefined, stroke: l.stroke }] : []), ...(l.paths ?? [])];
    return (
      <svg style={{ position: "absolute", left: x, top: y, width: w, height: h, opacity, transform, overflow: "visible" }}
        viewBox={`0 0 ${vw} ${vh}`} preserveAspectRatio={l.shape === "svg" ? "xMidYMid meet" : "none"}>
        {all.map((pth, i) => {
          const sc = pth.stroke ? valueAt<string>(pth.stroke.color, f, "#000000") : undefined;
          return (
            <path key={i} d={pth.d} fill={fill ?? pth.fill ?? "none"} fillOpacity={drawn < 1 ? drawn : 1}
              opacity={pth.opacity ?? 1} stroke={sc ?? "none"} strokeWidth={pth.stroke ? pth.stroke.width * vw / Math.max(1, w0) : 0}
              strokeLinecap={pth.stroke?.cap ?? "butt"} strokeLinejoin="round"
              transform={pth.translate ? `translate(${pth.translate[0]},${pth.translate[1]})` : undefined}
              pathLength={drawn < 1 && pth.stroke ? 1 : undefined}
              strokeDasharray={drawn < 1 && pth.stroke ? "1 1" : pth.stroke?.dash?.join(" ")}
              strokeDashoffset={drawn < 1 && pth.stroke ? 1 - drawn : undefined} />
          );
        })}
      </svg>
    );
  }
  if (l.shape === "path") {
    const [vw, vh] = l.viewbox ?? [w0, h];
    const drawn = Math.min(1, Math.max(0, valueAt<number>(l.draw, f, 1)));
    const gid = `g-${l.id}`;
    const grad = l.gradient && l.gradient.length > 1;
    const a = ((l.gradient_angle ?? 180) - 90) * Math.PI / 180;
    return (
      <svg style={{ position: "absolute", left: x, top: y, width: w, height: h, opacity, transform, overflow: "visible" }}
        viewBox={`0 0 ${vw} ${vh}`} preserveAspectRatio="none">
        {grad ? (
          <defs><linearGradient id={gid} x1={0.5 - Math.cos(a) / 2} y1={0.5 - Math.sin(a) / 2} x2={0.5 + Math.cos(a) / 2} y2={0.5 + Math.sin(a) / 2}>
            {l.gradient!.map((c, i) => <stop key={i} offset={i / (l.gradient!.length - 1)} stopColor={c} />)}
          </linearGradient></defs>) : null}
        <path d={l.d} fill={grad ? `url(#${gid})` : (fill ?? "none")} fillOpacity={drawn < 1 ? drawn : 1}
          stroke={strokeColor ?? "none"} strokeWidth={l.stroke ? l.stroke.width * vw / Math.max(1, w0) : 0}
          strokeLinecap={l.stroke?.cap ?? "butt"} strokeLinejoin="round" vectorEffect={undefined}
          pathLength={drawn < 1 ? 1 : undefined}
          strokeDasharray={drawn < 1 ? "1 1" : l.stroke?.dash?.join(" ")} strokeDashoffset={drawn < 1 ? 1 - drawn : undefined} />
      </svg>
    );
  }
  const bg = l.gradient && l.gradient.length > 1 ? `linear-gradient(${l.gradient_angle ?? 180}deg, ${l.gradient.join(", ")})` : fill;
  const style: React.CSSProperties = { position: "absolute", left: x, top: y, width: w, height: h,
    background: l.shape === "line" ? undefined : bg, opacity, transform,
    borderRadius: l.shape === "circle" ? "50%" : l.radius,
    border: l.stroke ? `${l.stroke.width}px ${l.stroke.dash ? "dashed" : "solid"} ${strokeColor}` : undefined, boxSizing: "border-box" };
  if (l.shape === "line") { style.height = Math.max(1, h); style.background = fill; }
  return <div style={style} />;
};

export const Scene: React.FC<SceneProps> = ({ layers, offset, total, durationInFrames }) => (
  <Clock.Provider value={{ offset: offset ?? 0, total: total ?? durationInFrames }}>
    <AbsoluteFill style={{ backgroundColor: "transparent" }}>
      {layers.map((l) => l.type === "text" ? <TextView key={l.id} l={l} /> : l.type === "captions" ? <CaptionsView key={l.id} l={l} />
        : <ShapeView key={l.id} l={l} />)}
    </AbsoluteFill>
  </Clock.Provider>
);
