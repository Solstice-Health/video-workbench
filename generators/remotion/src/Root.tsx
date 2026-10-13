import React from "react";
import { Composition } from "remotion";
import { Scene, SceneProps, defaultSceneProps } from "./Scene";

// One generic composition. Size, fps, length and every layer come from the canvas file.
export const Root: React.FC = () => (
  <Composition
    id="Scene"
    component={Scene}
    width={1080}
    height={1920}
    fps={30}
    durationInFrames={90}
    defaultProps={defaultSceneProps}
    calculateMetadata={({ props }: { props: SceneProps }) => ({
      width: props.width,
      height: props.height,
      fps: props.fps,
      durationInFrames: props.durationInFrames,
    })}
  />
);
