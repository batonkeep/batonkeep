// Sparkline.tsx — dependency-free inline-SVG sparkline (runs/day, §12 StatsBar).
interface Props {
  data: number[];
  width?: number;
  height?: number;
  stroke?: string;
  fill?: string;
}

export default function Sparkline({
  data,
  width = 120,
  height = 32,
  stroke = "#f5b700",
  fill = "rgba(245,183,0,0.12)",
}: Props) {
  if (data.length === 0) {
    return <svg width={width} height={height} aria-hidden />;
  }
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const span = max - min || 1;
  const n = data.length;
  const dx = n > 1 ? width / (n - 1) : 0;
  const pad = 2;
  const usable = height - pad * 2;

  const points = data.map((v, i) => {
    const x = i * dx;
    const y = pad + usable - ((v - min) / span) * usable;
    return [x, y] as const;
  });

  const line = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  const [lx, ly] = points[points.length - 1];

  return (
    <svg width={width} height={height} className="overflow-visible" role="img" aria-label="runs per day">
      <path d={area} fill={fill} stroke="none" />
      <path d={line} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lx} cy={ly} r={2} fill={stroke} />
    </svg>
  );
}
