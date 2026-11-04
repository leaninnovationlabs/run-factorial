/**
 * The sign-in page's left panel: the mark, placed tile by tile on a black bed and picked up
 * again, on a loop. The panel itself is the plate `logo.svg` draws, which is why no black square
 * is drawn here — and why the geometry is a second copy of the file's white rects, guarded by
 * `LogoPlate.test.tsx`. The rail still shows the file, static.
 *
 * Placement order is the array order: the tall bar and the lone block below it are the "!" of
 * "F!", so the dot is last and held back a beat by `logoPlate.css`.
 */

type Tile = { kind: "tile"; x: number; y: number; w: number; h: number };
/** A hollow tile as one stroked rect: the stroke, centred on the path, is what the file's four
 *  rects add up to, and one path is what lets the outline draw itself in. */
type Ring = { kind: "ring"; x: number; y: number; w: number; h: number; stroke: number };

export const MARK: readonly (Tile | Ring)[] = [
  { kind: "tile", x: 36, y: 12, w: 32, h: 96 },
  { kind: "tile", x: 78, y: 12, w: 32, h: 32 },
  { kind: "tile", x: 78, y: 76, w: 32, h: 32 },
  { kind: "ring", x: 123.5, y: 16.5, w: 23, h: 23, stroke: 9 },
  { kind: "ring", x: 123.5, y: 80.5, w: 23, h: 23, stroke: 9 },
  { kind: "tile", x: 36, y: 140, w: 32, h: 32 },
];

/** Four corner brackets just outside a shape, for the placement reticle. */
function reticlePath(shape: Tile | Ring): string {
  const half = shape.kind === "ring" ? shape.stroke / 2 : 0;
  const x0 = shape.x - half;
  const y0 = shape.y - half;
  const w = shape.w + half * 2;
  const h = shape.h + half * 2;
  const gap = 5;
  const arm = 8;
  const x1 = x0 - gap;
  const y1 = y0 - gap;
  const x2 = x0 + w + gap;
  const y2 = y0 + h + gap;
  return [
    `M${x1} ${y1 + arm}V${y1}H${x1 + arm}`,
    `M${x2 - arm} ${y1}H${x2}V${y1 + arm}`,
    `M${x2} ${y2 - arm}V${y2}H${x2 - arm}`,
    `M${x1 + arm} ${y2}H${x1}V${y2 - arm}`,
  ].join("");
}

const BED_W = 360;
const BED_H = 420;
const BED_STEP = 18;

function bedDots() {
  const dots = [];
  for (let y = 10; y < BED_H; y += BED_STEP) {
    for (let x = 10; x < BED_W; x += BED_STEP) {
      dots.push(<circle key={`${x}-${y}`} cx={x} cy={y} r={1.2} />);
    }
  }
  return dots;
}

const dotIndex = MARK.length - 1;

export function LogoPlate() {
  return (
    // The bed is the file's own black, in either theme: the mark is drawn for it, not for a
    // surface token that would turn grey under the light theme.
    <div
      className="logo-plate relative flex flex-col items-center justify-center gap-5 overflow-hidden p-8"
      style={{ backgroundColor: "#000", color: "#fff" }}
    >
      <svg
        className="absolute inset-0 h-full w-full"
        viewBox={`0 0 ${BED_W} ${BED_H}`}
        preserveAspectRatio="xMidYMid slice"
        aria-hidden="true"
      >
        <defs>
          <linearGradient id="logo-plate-glint" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#fff" stopOpacity="0" />
            <stop offset="0.5" stopColor="#fff" stopOpacity="0.13" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g className="logo-plate-grid">{bedDots()}</g>
        <rect
          className="logo-plate-sweep"
          x="0"
          y="-60"
          width="150"
          height={BED_H + 120}
          fill="url(#logo-plate-glint)"
          transform="skewX(-20)"
        />
      </svg>
      <svg
        className="logo-plate-mark relative h-28 w-28 sm:h-38 sm:w-38"
        viewBox="0 0 184 184"
        role="img"
        aria-label="Factorial"
      >
        {MARK.map((shape, i) =>
          shape.kind === "tile" ? (
            <rect
              key={`${shape.x},${shape.y}`}
              className={i === dotIndex ? "logo-plate-tile logo-plate-dot" : "logo-plate-tile"}
              style={{ "--i": i } as React.CSSProperties}
              x={shape.x}
              y={shape.y}
              width={shape.w}
              height={shape.h}
            />
          ) : (
            <rect
              key={`${shape.x},${shape.y}`}
              className="logo-plate-ring"
              style={{ "--i": i } as React.CSSProperties}
              x={shape.x}
              y={shape.y}
              width={shape.w}
              height={shape.h}
              strokeWidth={shape.stroke}
            />
          ),
        )}
        {MARK.map((shape, i) => (
          <path
            key={`${shape.x},${shape.y}`}
            className={i === dotIndex ? "logo-plate-reticle logo-plate-dot" : "logo-plate-reticle"}
            style={{ "--i": i } as React.CSSProperties}
            d={reticlePath(shape)}
          />
        ))}
        <circle className="logo-plate-ripple" cx="52" cy="156" r="22" />
      </svg>
      <span className="logo-plate-word relative text-heading-lg font-semibold tracking-tight">
        Factorial
      </span>
    </div>
  );
}
