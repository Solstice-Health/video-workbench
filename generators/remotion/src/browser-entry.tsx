import React from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { PlateScene, type SceneProps } from "./PlateScene";
import { Fill, spring, interpolate, measureText, gsapEase, freezeTicker } from "./browser-runtime";
import { drawCanvas } from "./canvas2d";
import manifest from "../public/fonts/manifest.json";

type Renderer = "browser" | "gsap" | "canvas2d";
let props: SceneProps, renderer: Renderer;
let root: ReturnType<typeof createRoot> | undefined;
let canvas: HTMLCanvasElement, scratch: HTMLCanvasElement;
const host = document.getElementById("plate")!;
const api = {
  async initialize(input: SceneProps, backend: Renderer) {
    props = input;
    renderer = backend;
    const families = new Set(input.layers.filter(l => l.type !== "shape").map(l => l.style.font));
    await Promise.all(manifest.filter(m => families.has(m.family)).flatMap(m => m.faces.map(async face => {
      const font = new FontFace(m.family, `url(${new URL(`fonts/${face.file}`, location.href).href})`, { weight: String(face.weight), style: face.style });
      (document.fonts as FontFaceSet & { add(face: FontFace): void }).add(await font.load());
    })));
    await document.fonts.ready;
    if (backend === "canvas2d") {
      canvas = document.createElement("canvas");
      scratch = document.createElement("canvas");
      canvas.width = scratch.width = input.width;
      canvas.height = scratch.height = input.height;
      host.replaceChildren(canvas);
    } else root = createRoot(host);
    freezeTicker();
  },
  frame(frame: number) {
    if (!Number.isInteger(frame) || frame < 0 || frame >= props.durationInFrames) throw new Error("Frame outside plate");
    if (renderer === "canvas2d") drawCanvas(canvas, props, frame, scratch);
    else flushSync(() => root!.render(<PlateScene {...props} runtime={{ frame, fps: props.fps, Fill, spring, interpolate,
      measureText, ease: renderer === "gsap" ? gsapEase : undefined }} />));
    for (const animation of document.getAnimations()) animation.cancel();
    freezeTicker();
    // Force layout synchronously; screenshot captures only this explicitly selected frame.
    host.getBoundingClientRect();
  },
};
Object.defineProperty(window, "__vwb", { value: Object.freeze(api), writable: false });
