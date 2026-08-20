/**
 * Displacement map generation for Liquid Glass refraction.
 *
 * This is what separates Liquid Glass from glassmorphism. Glassmorphism is
 * `backdrop-filter: blur()` — light passes through diffused but travelling
 * straight. Real glass *bends* light, and near a curved edge that bending is
 * what your eye reads as thickness.
 *
 * SVG's `feDisplacementMap` can do it: it shifts each pixel by an amount read
 * from a second image, red channel driving X and green driving Y, with 128
 * meaning "no shift". So the job is to compute, for every point on the surface,
 * how far light would bend there — and encode that as a colour.
 *
 * The physics is Snell's law. Light entering a denser medium bends toward the
 * surface normal by n₁·sin(θ₁) = n₂·sin(θ₂). Across a flat centre the normal
 * points straight out and nothing bends; toward a curved rim the normal tilts
 * and the displacement grows sharply. That gradient is the effect.
 *
 * BROWSER SUPPORT — the constraint that shapes everything here:
 *
 *   Only Chromium accepts an SVG filter as a `backdrop-filter`. Safari and
 *   Firefox ignore it and render the CSS blur alone, which is ordinary
 *   glassmorphism. That is a deliberate, acceptable degradation: the layout,
 *   contrast, and legibility do not depend on the refraction, only the
 *   material quality does.
 *
 * PERFORMANCE — the reason this is memoised and used sparingly:
 *
 *   Every distinct size needs its own map, and generating one is O(width ×
 *   height) work on the main thread. Animating the filter's `scale` is cheap;
 *   resizing the element is not. Hence: fixed-size surfaces, cached maps, and
 *   glass on chrome rather than on content that reflows.
 */

/** Refractive index of glass. Air is ~1.0, water ~1.33, glass ~1.5. */
const GLASS_IOR = 1.5;

/** Neutral displacement. 128 in an 8-bit channel means "do not move me". */
const NEUTRAL = 128;

export interface GlassProfile {
  /** Corner radius in px. The refraction follows this, so it must match the
   *  element's own border-radius or the lens will not line up with the shape. */
  radius: number;
  /** How far the bending reaches inward from the edge, in px. Small values give
   *  a crisp rim; large values give a thick, paperweight-like body. */
  depth: number;
  /** Peak pixel shift. Above roughly 40 the distortion stops reading as glass
   *  and starts reading as a broken screen. */
  strength: number;
}

export const PROFILES = {
  /** Panels and sidebars: present but not showy. */
  subtle: { radius: 12, depth: 14, strength: 12 } satisfies GlassProfile,
  /** Dialogs and floating cards: the default. */
  standard: { radius: 16, depth: 20, strength: 20 } satisfies GlassProfile,
  /** Buttons and pills, where the whole surface is edge. */
  pronounced: { radius: 999, depth: 24, strength: 28 } satisfies GlassProfile,
} as const;

/**
 * Signed distance to the edge of a rounded rectangle.
 *
 * Negative inside, zero on the boundary. Used because it gives a smooth,
 * corner-aware measure of "how close am I to the rim" — which is exactly what
 * the surface curve needs, and what a naive per-side distance gets wrong at
 * the corners.
 */
function roundedRectSDF(
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): number {
  const halfW = width / 2;
  const halfH = height / 2;
  const r = Math.min(radius, halfW, halfH);

  const dx = Math.abs(x - halfW) - (halfW - r);
  const dy = Math.abs(y - halfH) - (halfH - r);

  const outside = Math.hypot(Math.max(dx, 0), Math.max(dy, 0));
  const inside = Math.min(Math.max(dx, dy), 0);
  return outside + inside - r;
}

/**
 * Surface normal tilt at a given depth into the glass.
 *
 * Models the edge as a quarter-round bevel: flat across the middle, curving
 * away toward the rim. Returns the angle between the surface normal and
 * straight-out, in radians — 0 in the flat centre, approaching π/2 at the very
 * edge.
 */
function surfaceAngle(distanceFromEdge: number, depth: number): number {
  if (distanceFromEdge >= depth) return 0;
  // Normalised position across the bevel: 1 at the rim, 0 where it flattens.
  const t = 1 - distanceFromEdge / depth;
  // A quarter-circle profile. Steep at the rim, easing to flat — which is what
  // makes the highlight cling to the edge rather than smearing inward.
  return Math.asin(Math.min(t, 0.999));
}

/**
 * Refraction displacement at a point, via Snell's law.
 *
 * Returns pixel offsets. Zero across the flat centre; growing toward the rim
 * as the surface normal tilts away from the viewing direction.
 */
function refractionAt(
  x: number,
  y: number,
  width: number,
  height: number,
  profile: GlassProfile,
): { dx: number; dy: number } {
  const distance = -roundedRectSDF(x, y, width, height, profile.radius);
  if (distance <= 0 || distance >= profile.depth) return { dx: 0, dy: 0 };

  const incident = surfaceAngle(distance, profile.depth);

  // Snell: sin(θ₂) = sin(θ₁) / n. Clamped because beyond the critical angle
  // there is no refracted ray at all (total internal reflection), which the
  // real material shows as a bright rim rather than a bend.
  const refracted = Math.asin(Math.min(Math.sin(incident) / GLASS_IOR, 1));
  const bend = Math.tan(incident - refracted);

  // Direction: outward from the centre, so the bend pushes content toward the
  // rim — the magnifying-lens look rather than a pinch.
  const cx = width / 2;
  const cy = height / 2;
  const vx = x - cx;
  const vy = y - cy;
  const length = Math.hypot(vx, vy) || 1;

  const magnitude = bend * profile.strength;
  return { dx: (vx / length) * magnitude, dy: (vy / length) * magnitude };
}

/**
 * Build the displacement map as a data URI.
 *
 * Encoded as PNG via canvas because `feImage` needs an image, and a data URI
 * keeps it self-contained — no extra request, no cleanup of an object URL.
 */
export function generateDisplacementMap(
  width: number,
  height: number,
  profile: GlassProfile,
): string {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width));
  canvas.height = Math.max(1, Math.round(height));

  const context = canvas.getContext("2d");
  if (!context) return "";

  const image = context.createImageData(canvas.width, canvas.height);
  const { data } = image;

  for (let y = 0; y < canvas.height; y++) {
    for (let x = 0; x < canvas.width; x++) {
      const { dx, dy } = refractionAt(x, y, canvas.width, canvas.height, profile);
      const i = (y * canvas.width + x) * 4;

      // 128 is neutral; ±127 is the encodable range. The filter's `scale`
      // attribute multiplies this back up to real pixels.
      data[i] = clampChannel(NEUTRAL + dx);
      data[i + 1] = clampChannel(NEUTRAL + dy);
      data[i + 2] = NEUTRAL; // blue is unused by feDisplacementMap
      data[i + 3] = 255;
    }
  }

  context.putImageData(image, 0, 0);
  return canvas.toDataURL();
}

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Math.round(value)));
}

/**
 * Cache maps by size and profile.
 *
 * Generation is O(pixels) on the main thread, and several surfaces on a page
 * usually share dimensions. Without this, a resize regenerates every map and
 * the frame budget disappears.
 */
const cache = new Map<string, string>();

export function getDisplacementMap(
  width: number,
  height: number,
  profile: GlassProfile,
): string {
  // Rounded to 4px buckets: a one-pixel size change does not perceptibly alter
  // the refraction, and exact keys would defeat the cache during a resize.
  const w = Math.round(width / 4) * 4;
  const h = Math.round(height / 4) * 4;
  const key = `${w}x${h}:${profile.radius}:${profile.depth}:${profile.strength}`;

  const cached = cache.get(key);
  if (cached) return cached;

  const map = generateDisplacementMap(w, h, profile);
  // Bounded so a page that resizes continuously cannot grow this without limit.
  if (cache.size > 32) cache.clear();
  cache.set(key, map);
  return map;
}

/**
 * Whether this browser will actually render the refraction.
 *
 * Chromium accepts an SVG filter reference in `backdrop-filter`; Safari and
 * Firefox parse it and ignore it. Detecting that lets a component skip the
 * generation cost entirely rather than computing a map nothing will use.
 */
export function supportsBackdropFilterSVG(): boolean {
  if (typeof window === "undefined" || typeof CSS === "undefined") return false;
  return (
    CSS.supports("backdrop-filter", "url(#x)") ||
    CSS.supports("-webkit-backdrop-filter", "url(#x)")
  );
}
