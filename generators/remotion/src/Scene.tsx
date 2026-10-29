import React from "react";
import { AbsoluteFill, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig, delayRender, continueRender } from "remotion";
import { loadFont } from "@remotion/fonts";
import { measureText } from "@remotion/layout-utils";
import manifest from "../public/fonts/manifest.json";
import { PlateScene, type SceneProps } from "./PlateScene";
export { defaultSceneProps, type SceneProps } from "./PlateScene";

// ---------------------------------------------------------------- fonts (the pinned library, local files)
type Face = { weight: number; style: "normal" | "italic"; file: string };
const fontHandle = delayRender("fonts");
Promise.all((manifest as { family: string; faces: Face[] }[]).flatMap((m) => m.faces.map((face) =>
  loadFont({ family: m.family, url: staticFile(`fonts/${face.file}`), weight: String(face.weight), style: face.style }),
))).then(() => continueRender(fontHandle)).catch((e) => { console.error(e); continueRender(fontHandle); });


// Preserve the Remotion clock, fonts, layout measurement, spring and wrapper exactly.
export const Scene: React.FC<SceneProps> = (props) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return <PlateScene {...props} runtime={{ frame, fps, Fill: AbsoluteFill, interpolate, spring, measureText }} />;
};
