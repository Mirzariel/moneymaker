"use client";

import { useEffect, useRef, useState } from "react";
import type { EquityPoint } from "@/lib/types";
import { fmtMoney, fmtTime } from "@/lib/format";

const H = 220;
const PAD = { l: 8, r: 8, t: 22, b: 44 };

export function EquityChart({ points, quote }: { points: EquityPoint[] | null; quote: string }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(600);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setW(Math.max(240, Math.floor(el.clientWidth)));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const pts = (points ?? []).filter((p) => Number.isFinite(p.ts) && Number.isFinite(p.equity));

  let body: React.ReactNode;
  if (points === null) {
    body = <p className="muted pad">Memuat…</p>;
  } else if (pts.length === 0) {
    body = <p className="muted pad">Belum ada data equity.</p>;
  } else {
    const xs = pts.map((p) => p.ts);
    const ys = pts.map((p) => p.equity);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const minX = xs[0];
    const maxX = xs[xs.length - 1];
    const spanY = maxY - minY || Math.max(Math.abs(maxY) * 0.01, 1);
    const yLo = maxY === minY ? minY - spanY : minY;
    const yHi = maxY === minY ? maxY + spanY : maxY;
    const innerW = w - PAD.l - PAD.r;
    const innerH = H - PAD.t - PAD.b;
    const sx = (t: number) => (maxX === minX ? PAD.l + innerW / 2 : PAD.l + ((t - minX) / (maxX - minX)) * innerW);
    const sy = (v: number) => PAD.t + (1 - (v - yLo) / (yHi - yLo)) * innerH;

    const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.ts).toFixed(1)} ${sy(p.equity).toFixed(1)}`).join(" ");
    const area = `${line} L${sx(maxX).toFixed(1)} ${PAD.t + innerH} L${sx(minX).toFixed(1)} ${PAD.t + innerH} Z`;
    const iMin = ys.indexOf(minY);
    const iMax = ys.indexOf(maxY);
    const last = pts[pts.length - 1];
    const hp = hover != null ? pts[hover] : null;

    const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const x = e.clientX - rect.left;
      let best = 0;
      let bd = Infinity;
      pts.forEach((p, i) => {
        const d = Math.abs(sx(p.ts) - x);
        if (d < bd) {
          bd = d;
          best = i;
        }
      });
      setHover(best);
    };

    const labelAnchor = (x: number) => (x < 70 ? "start" : x > w - 70 ? "end" : "middle");

    body = (
      <svg
        width={w}
        height={H}
        viewBox={`0 0 ${w} ${H}`}
        role="img"
        aria-label={`Grafik equity, min ${fmtMoney(minY, quote)}, max ${fmtMoney(maxY, quote)}`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        className="chart"
      >
        <line x1={PAD.l} x2={w - PAD.r} y1={PAD.t + innerH} y2={PAD.t + innerH} className="axis" />
        <line x1={PAD.l} x2={w - PAD.r} y1={sy(maxY)} y2={sy(maxY)} className="grid" />
        {maxY !== minY && <line x1={PAD.l} x2={w - PAD.r} y1={sy(minY)} y2={sy(minY)} className="grid" />}
        {pts.length > 1 && <path d={area} className="area" />}
        {pts.length > 1 ? (
          <path d={line} className="line" />
        ) : (
          <circle cx={sx(last.ts)} cy={sy(last.equity)} r={4} className="dot" />
        )}
        <text x={sx(pts[iMax].ts)} y={sy(maxY) - 6} textAnchor={labelAnchor(sx(pts[iMax].ts))} className="lbl">
          max {fmtMoney(maxY, quote)}
        </text>
        {maxY !== minY && (
          <text x={sx(pts[iMin].ts)} y={sy(minY) + 14} textAnchor={labelAnchor(sx(pts[iMin].ts))} className="lbl">
            min {fmtMoney(minY, quote)}
          </text>
        )}
        <text x={PAD.l} y={H - 6} textAnchor="start" className="lbl dim">
          {fmtTime(minX)}
        </text>
        {maxX !== minX && (
          <text x={w - PAD.r} y={H - 6} textAnchor="end" className="lbl dim">
            {fmtTime(maxX)}
          </text>
        )}
        {pts.length > 1 && <circle cx={sx(last.ts)} cy={sy(last.equity)} r={3.5} className="dot" />}
        {hp && (
          <g pointerEvents="none">
            <line x1={sx(hp.ts)} x2={sx(hp.ts)} y1={PAD.t} y2={PAD.t + innerH} className="cross" />
            <circle cx={sx(hp.ts)} cy={sy(hp.equity)} r={4} className="dot" />
          </g>
        )}
      </svg>
    );
    return (
      <div ref={wrapRef} className="chart-wrap">
        {body}
        <div className="chart-readout muted">
          {hp ? `${fmtTime(hp.ts)} · ${fmtMoney(hp.equity, quote)}` : `Terakhir: ${fmtMoney(last.equity, quote)} · ${pts.length} titik`}
        </div>
      </div>
    );
  }

  return (
    <div ref={wrapRef} className="chart-wrap">
      {body}
    </div>
  );
}
