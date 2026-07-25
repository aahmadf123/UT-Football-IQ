/**
 * SVG polyline trend chart. Renders whatever numeric series it is given and an
 * honest "not available" box when there is none — callers must never feed it
 * fabricated data.
 */

export function TrendLine({ data }: { data: number[] | undefined }) {
  if (!data || data.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center rounded-md border border-dashed border-border-soft">
        <span className="text-xs text-muted-foreground">Trend not available</span>
      </div>
    );
  }
  const points = data
    .map((value, index) => `${(index / Math.max(data.length - 1, 1)) * 100},${100 - value}`)
    .join(" ");
  return (
    <div className="h-24 rounded-md border border-border-soft bg-secondary/20 p-1.5">
      <svg
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        role="img"
        aria-label="Trend chart"
        className="h-full w-full"
      >
        <polyline
          points={points}
          fill="none"
          stroke="var(--status-info)"
          strokeWidth="3"
          vectorEffect="non-scaling-stroke"
        />
        {data.map((value, index) => (
          <circle
            key={index}
            cx={(index / Math.max(data.length - 1, 1)) * 100}
            cy={100 - value}
            r="2.2"
            fill="var(--primary)"
          />
        ))}
      </svg>
    </div>
  );
}
