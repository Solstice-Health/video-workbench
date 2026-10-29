import React from "react";
import { gsap } from "gsap";
import type { RenderRuntime } from "./PlateScene";

export const Fill: RenderRuntime["Fill"] = ({ children, style }) => React.createElement("div", {
  style: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, width: "100%", height: "100%", display: "flex", flexDirection: "column", ...style },
}, children);

// The supported springs are damped harmonic oscillators evaluated from time zero.
// No wall clock, retained velocities or Remotion runtime are involved.
export const spring: RenderRuntime["spring"] = ({ frame, fps, config: { damping, stiffness, mass } }) => {
  const t = Math.max(0, frame) / fps;
  const w = Math.sqrt(stiffness / mass), z = damping / (2 * Math.sqrt(stiffness * mass));
  if (z < 1) {
    const wd = w * Math.sqrt(1 - z * z);
    return 1 - Math.exp(-z * w * t) * (Math.cos(wd * t) + z * w / wd * Math.sin(wd * t));
  }
  return 1 - Math.exp(-w * t) * (1 + w * t);
};
export const interpolate: RenderRuntime["interpolate"] = (v, input, output) => output[0] + (v - input[0]) / (input[1] - input[0]) * (output[1] - output[0]);

const measurements = new Map<string, { width: number }>();
export const measureText: RenderRuntime["measureText"] = (args) => {
  const key = JSON.stringify(args);
  if (measurements.has(key)) return measurements.get(key)!;
  const node = document.createElement("span");
  Object.assign(node.style, { fontFamily: args.fontFamily, fontWeight: args.fontWeight,
    fontSize: `${args.fontSize}px`, letterSpacing: args.letterSpacing ?? "normal",
    display: "inline-block", position: "absolute", top: "-10000px", whiteSpace: "pre" });
  node.innerText = args.text;
  document.body.appendChild(node);
  const result = { width: node.getBoundingClientRect().width };
  node.remove();
  if (measurements.size >= 10000) measurements.clear();
  measurements.set(key, result);
  return result;
};

// GSAP owns the easing evaluation through paused, explicitly sought timelines.
// Reuse only immutable 0->1 timelines; seeking backwards never accumulates state.
const timelines = new Map<string, { state: { value: number }; timeline: gsap.core.Timeline }>();
for (const [kind, easing] of Object.entries({ linear: "none", in: "power2.in", out: "power2.out", inout: "power2.inOut" })) {
  const state = { value: 0 };
  const timeline = gsap.timeline({ paused: true }).fromTo(state, { value: 0 }, { value: 1, duration: 1, ease: easing, immediateRender: true, lazy: false });
  timelines.set(kind, { state, timeline });
}
gsap.ticker.sleep();
export const gsapEase = (t: number, kind = "inout") => {
  if (kind === "hold") return 0;
  const item = timelines.get(kind)!;
  item.timeline.seek(Math.max(0, Math.min(1, t)), true);
  gsap.ticker.sleep();
  return item.state.value;
};
export const freezeTicker = () => gsap.ticker.sleep();
