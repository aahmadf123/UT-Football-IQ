"use client";

import { useEffect, useRef, useState } from "react";
import type { EChartsCoreOption } from "echarts/core";

/** One lazy, resize-aware lifecycle for all statistical charts. Tables remain HTML. */
export function EChart({ option, label, onSelect }: {
  option: EChartsCoreOption; label: string; onSelect?: (index: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const select = useRef(onSelect);
  const [error, setError] = useState(false);
  useEffect(() => { select.current = onSelect; }, [onSelect]);
  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    setError(false);
    Promise.all([import("echarts/core"), import("echarts/charts"), import("echarts/components"), import("echarts/renderers")]).then(([core, charts, components, renderers]) => {
      if (cancelled || !host.current) return;
      core.use([charts.BarChart, charts.LineChart, charts.ScatterChart, charts.HeatmapChart, components.GridComponent, components.TooltipComponent, components.AriaComponent, components.VisualMapComponent, renderers.SVGRenderer]);
      const axis = { axisLabel: { color: "#cbd5e1" }, nameTextStyle: { color: "#cbd5e1" }, axisLine: { lineStyle: { color: "#64748b" } }, splitLine: { lineStyle: { color: "#2d3b4b" } } };
      const chart = core.init(host.current, { textStyle: { color: "#cbd5e1" }, categoryAxis: axis, valueAxis: axis, color: ["#e4bb50", "#38bdf8", "#86efac"] }, { renderer: "svg" });
      chart.setOption({ animation: false, aria: { enabled: true }, ...option });
      chart.on("click", (event) => { if (typeof event.dataIndex === "number") select.current?.(event.dataIndex); });
      const observer = new ResizeObserver(() => chart.resize());
      observer.observe(host.current);
      cleanup = () => { observer.disconnect(); chart.dispose(); };
    }).catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; cleanup?.(); };
  }, [option]);
  return <div><div ref={host} role="img" aria-label={label} className="h-64 w-full min-w-0" />{error ? <p role="status" className="text-sm text-muted-foreground">Chart unavailable. Use the data table below.</p> : null}</div>;
}
