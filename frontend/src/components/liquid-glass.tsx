"use client";

import { useEffect, useId, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import {
  PROFILES,
  getDisplacementMap,
  supportsBackdropFilterSVG,
} from "@/lib/liquid-glass";

/**
 * A Liquid Glass surface.
 *
 * Four things stack to make this read as a material rather than a blur:
 *
 *   1. REFRACTION — an SVG feDisplacementMap bends what is behind the element,
 *      strongly at the rim and not at all across the middle. This is the part
 *      that distinguishes Liquid Glass from glassmorphism, and the part only
 *      Chromium renders.
 *   2. SPECULAR EDGE — a gradient border catching light from one direction, so
 *      the surface has a lit side and a shadowed side.
 *   3. POINTER HIGHLIGHT — a soft bloom tracking the cursor, which is what
 *      makes the material feel wet rather than printed.
 *   4. INNER SHADOW — a faint inset ring giving the panel thickness.
 *
 * Three of the four work everywhere. Only the refraction is Chromium-only, and
 * its absence is a graceful loss of realism rather than a broken layout.
 */

interface LiquidGlassProps extends React.HTMLAttributes<HTMLDivElement> {
  /** Edge profile. `subtle` for panels, `standard` for dialogs. */
  profile?: keyof typeof PROFILES;
  /** Track the pointer with a specular bloom. Off for large static surfaces,
   *  where per-frame updates buy little and cost paint. */
  interactive?: boolean;
  children?: React.ReactNode;
}

export function LiquidGlass({
  profile = "standard",
  interactive = true,
  className,
  children,
  ...props
}: LiquidGlassProps) {
  const ref = useRef<HTMLDivElement>(null);
  const filterId = useId().replace(/:/g, "");
  const [displacementMap, setDisplacementMap] = useState<string>("");
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [refracts, setRefracts] = useState(false);

  const settings = PROFILES[profile];

  // One effect rather than three.
  //
  // Support detection, size observation, and map generation were separate
  // effects each calling setState, which React 19 correctly flags as cascading
  // renders: every one of them re-triggered the next. Doing the work inside the
  // observer callback means a single state update per size change, and none at
  // all where the browser will not render the filter anyway.
  useEffect(() => {
    if (!supportsBackdropFilterSVG() || !ref.current) return;
    setRefracts(true);

    const element = ref.current;
    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect;
      if (!box || box.width < 8 || box.height < 8) return;
      setSize({ width: box.width, height: box.height });
      setDisplacementMap(getDisplacementMap(box.width, box.height, settings));
    });

    observer.observe(element);
    return () => observer.disconnect();
  }, [settings]);

  // Pointer position drives the specular bloom. Written to CSS custom
  // properties rather than React state: this fires on every mousemove, and a
  // re-render per frame would be far more expensive than a style mutation.
  useEffect(() => {
    if (!interactive || !ref.current) return;

    const element = ref.current;
    const onMove = (event: PointerEvent) => {
      const rect = element.getBoundingClientRect();
      element.style.setProperty("--glass-x", `${event.clientX - rect.left}px`);
      element.style.setProperty("--glass-y", `${event.clientY - rect.top}px`);
      element.style.setProperty("--glass-glow", "1");
    };
    const onLeave = () => element.style.setProperty("--glass-glow", "0");

    element.addEventListener("pointermove", onMove);
    element.addEventListener("pointerleave", onLeave);
    return () => {
      element.removeEventListener("pointermove", onMove);
      element.removeEventListener("pointerleave", onLeave);
    };
  }, [interactive]);

  return (
    <>
      {refracts && displacementMap ? (
        <svg aria-hidden className="pointer-events-none absolute size-0">
          <filter
            id={filterId}
            // userSpaceOnUse so the map lines up with the element in pixels
            // rather than being stretched into a fractional bounding box.
            filterUnits="userSpaceOnUse"
            x="0"
            y="0"
            width={size.width}
            height={size.height}
            colorInterpolationFilters="sRGB"
          >
            <feImage
              href={displacementMap}
              x="0"
              y="0"
              width={size.width}
              height={size.height}
              result="map"
            />
            <feDisplacementMap
              in="SourceGraphic"
              in2="map"
              scale={settings.strength}
              xChannelSelector="R"
              yChannelSelector="G"
            />
          </filter>
        </svg>
      ) : null}

      <div
        ref={ref}
        className={cn("glass-surface", className)}
        style={
          {
            // The refraction and the blur are one backdrop-filter. Where the
            // SVG reference is ignored the blur still applies, which is the
            // fallback rather than a special case.
            backdropFilter: refracts && displacementMap
              ? `url(#${filterId}) blur(2px) saturate(1.4)`
              : "blur(12px) saturate(1.4)",
            WebkitBackdropFilter: "blur(12px) saturate(1.4)",
            borderRadius: settings.radius === 999 ? "9999px" : `${settings.radius}px`,
          } as React.CSSProperties
        }
        {...props}
      >
        {children}
      </div>
    </>
  );
}
