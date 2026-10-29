export type Key = { f: number; v: any; ease?: string };
export type Anim<T> = T | Key[];
export const ease = (t: number, kind = "inout"): number => {
  t = Math.min(1, Math.max(0, t));
  if (kind === "hold") return 0;
  if (kind === "linear") return t;
  if (kind === "in") return t * t * t;
  if (kind === "out") return 1 - Math.pow(1 - t, 3);
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
};
export const isKeyed = (v: any): v is Key[] => Array.isArray(v) && v.length > 0 && typeof v[0] === "object" && v[0] !== null && "f" in v[0];
const rgba = (h: string): number[] => {
  const v = h.replace("#", "").padEnd(8, "F").slice(0, 8);
  return [0, 2, 4, 6].map((i) => parseInt(v.slice(i, i + 2), 16));
};
const hex = (c: number[]) => "#" + c.map((x) => Math.max(0, Math.min(255, Math.round(x))).toString(16).padStart(2, "0")).join("");
export function valueAt<T>(v: Anim<T> | undefined, f: number, fallback: T, easing = ease): T {
  if (v === undefined || v === null) return fallback;
  if (!isKeyed(v)) return v as T;
  const k = v as Key[];
  if (f <= k[0].f) return k[0].v;
  for (let i = 0; i + 1 < k.length; i++) {
    const a = k[i], b = k[i + 1];
    if (a.f <= f && f < b.f) {
      if (b.ease === "hold") return a.v;
      const u = easing((f - a.f) / Math.max(1, b.f - a.f), b.ease ?? "inout");
      if (typeof a.v === "number") return (a.v + (b.v - a.v) * u) as T;
      if (typeof a.v === "string") { const x = rgba(a.v), y = rgba(b.v); return hex(x.map((c, j) => c + (y[j] - c) * u)) as T; }
      return (a.v as number[]).map((x, j) => x + ((b.v as number[])[j] - x) * u) as T;
    }
  }
  return k[k.length - 1].v;
}
