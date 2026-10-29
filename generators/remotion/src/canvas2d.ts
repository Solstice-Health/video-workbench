import { valueAt } from "./animation";
import type { SceneProps, ShapeLayer } from "./PlateScene";

// Shape-only backend. SVG is used solely for safe path geometry measurements;
// pixels are drawn exclusively through CanvasRenderingContext2D and Path2D.
const metrics = new Map<string, { length: number; x: number; y: number; width: number; height: number }>();
function geometry(d: string) {
  if (!metrics.has(d)) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "0"); svg.setAttribute("height", "0");
    svg.style.position = "absolute"; svg.style.visibility = "hidden";
    svg.appendChild(path); document.body.appendChild(svg);
    const { x, y, width, height } = path.getBBox();
    const length = path.getTotalLength();
    svg.remove();
    if (metrics.size >= 1024) metrics.clear();
    metrics.set(d, { length, x, y, width, height });
  }
  return metrics.get(d)!;
}
function gradient(ctx: CanvasRenderingContext2D, l: ShapeLayer, w: number, h: number, pathData?: string) {
  const a = ((l.gradient_angle ?? 180) - 90) * Math.PI / 180;
  const dx = Math.cos(a), dy = Math.sin(a);
  const extent = Math.abs(w * dx) + Math.abs(h * dy);
  const b = pathData ? geometry(pathData) : undefined;
  const g = b ? ctx.createLinearGradient(b.x + b.width * (0.5 - dx / 2), b.y + b.height * (0.5 - dy / 2),
    b.x + b.width * (0.5 + dx / 2), b.y + b.height * (0.5 + dy / 2))
    : ctx.createLinearGradient(w / 2 - dx * extent / 2, h / 2 - dy * extent / 2,
      w / 2 + dx * extent / 2, h / 2 + dy * extent / 2);
  l.gradient!.forEach((color, i) => g.addColorStop(i / (l.gradient!.length - 1), color));
  return g;
}
let pathCanvas: HTMLCanvasElement | undefined;
export function drawCanvas(canvas: HTMLCanvasElement, props: SceneProps, localFrame: number, scratch: HTMLCanvasElement) {
  const ctx = canvas.getContext("2d")!, layerCtx = scratch.getContext("2d")!;
  pathCanvas ??= document.createElement("canvas");
  if (pathCanvas.width !== canvas.width || pathCanvas.height !== canvas.height) { pathCanvas.width = canvas.width; pathCanvas.height = canvas.height; }
  const pathCtx = pathCanvas.getContext("2d")!;
  const f = localFrame + (props.offset ?? 0), total = props.total ?? props.durationInFrames;
  ctx.resetTransform();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  for (const layer of props.layers) {
    const l = layer as ShapeLayer;
    if (l.span && (f < l.span[0] || f >= l.span[1])) continue;
    const [x, y, w0, h0] = valueAt(l.box, f, [0, 0, 100, 100]);
    const h = l.shape === "line" ? Math.max(1, h0) : h0;
    const w = l.progress ? w0 * Math.min(1, (f + 1) / total) : w0;
    if (w <= 0 || h <= 0) continue;
    layerCtx.reset();
    layerCtx.translate(x + w / 2, y + h / 2);
    layerCtx.rotate(valueAt(l.rotate, f, 0) * Math.PI / 180);
    layerCtx.translate(-w / 2, -h / 2);
    const fill = valueAt(l.fill, f, undefined);
    if (l.shape === "path" || l.shape === "svg") {
      const [vw, vh] = l.viewbox ?? [w0, h];
      let sx = w / vw, sy = h / vh;
      if (l.shape === "svg") {
        const scale = Math.min(sx, sy);
        layerCtx.translate((w - vw * scale) / 2, (h - vh * scale) / 2);
        sx = sy = scale;
      }
      layerCtx.scale(sx, sy);
      const drawn = Math.max(0, Math.min(1, valueAt(l.draw, f, 1)));
      const group = l.shape === "svg" || Boolean(l.paths?.length);
      const paths = group ? [...(l.d ? [{ d: l.d, stroke: l.stroke }] : []), ...(l.paths ?? [])] : [{ d: l.d!, stroke: l.stroke }];
      for (const p of paths) {
        layerCtx.save();
        if ("translate" in p && p.translate) layerCtx.translate(...p.translate);
        const path = new Path2D(p.d);
        const color = fill ?? ("fill" in p ? p.fill : undefined);
        const alpha = "opacity" in p ? p.opacity ?? 1 : 1;
        // SVG path opacity composites fill and stroke as one group.
        const paint = alpha === 1 ? layerCtx : pathCtx;
        if (alpha !== 1) { pathCtx.reset(); pathCtx.setTransform(layerCtx.getTransform()); }
        if (color && color !== "none" || !group && l.gradient && l.gradient.length > 1) {
          paint.fillStyle = !group && l.gradient && l.gradient.length > 1 ? gradient(paint, l, vw, vh, p.d) : color!;
          paint.globalAlpha = drawn;
          paint.fill(path);
        }
        if (p.stroke && p.stroke.width > 0 && valueAt(p.stroke.color, f, "#000000") !== "none") {
          paint.globalAlpha = 1;
          paint.strokeStyle = valueAt(p.stroke.color, f, "#000000");
          paint.lineWidth = p.stroke.width * vw / Math.max(1, w0);
          paint.lineCap = p.stroke.cap ?? "butt";
          paint.lineJoin = "round";
          if (drawn < 1) {
            const { length } = geometry(p.d);
            paint.setLineDash([length, length]);
            paint.lineDashOffset = (1 - drawn) * length;
          } else paint.setLineDash(p.stroke.dash ?? []);
          if (drawn > 0) paint.stroke(path);
        }
        if (alpha !== 1) { layerCtx.resetTransform(); layerCtx.globalAlpha = alpha; layerCtx.drawImage(pathCanvas, 0, 0); }
        layerCtx.restore();
      }
    } else {
      const border = l.stroke?.width ?? 0;
      const path = new Path2D(), fillPath = new Path2D();
      if (l.shape === "circle") fillPath.ellipse(w / 2, h / 2, w / 2, h / 2, 0, 0, Math.PI * 2);
      else fillPath.roundRect(0, 0, w, h, l.radius ?? 0);
      const inset = Math.min(border / 2, w / 2, h / 2);
      if (l.shape === "circle") path.ellipse(w / 2, h / 2, Math.max(0, w / 2 - inset), Math.max(0, h / 2 - inset), 0, 0, Math.PI * 2);
      else path.roundRect(inset, inset, Math.max(0, w - inset * 2), Math.max(0, h - inset * 2), Math.max(0, (l.radius ?? 0) - inset));
      if (fill && fill !== "none" || l.shape !== "line" && l.gradient && l.gradient.length > 1) {
        layerCtx.fillStyle = l.shape !== "line" && l.gradient && l.gradient.length > 1 ? gradient(layerCtx, l, w, h) : fill!;
        layerCtx.fill(fillPath);
      }
      if (l.stroke && border > 0 && valueAt(l.stroke.color, f, "#000000") !== "none") {
        layerCtx.strokeStyle = valueAt(l.stroke.color, f, "#000000");
        layerCtx.lineWidth = border;
        // CSS boxes use a dashed border whenever dash is present; paths use the exact pattern.
        layerCtx.setLineDash(l.stroke.dash ? [border * 3, border * 3] : []);
        layerCtx.stroke(path);
      }
    }
    ctx.globalAlpha = valueAt(l.opacity, f, 1);
    ctx.drawImage(scratch, 0, 0);
  }
  ctx.globalAlpha = 1;
}
