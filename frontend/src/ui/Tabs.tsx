// Tabs.tsx — controlled segmented tab strip (e.g. RunViewer Report/JSON/Raw).
interface Props<T extends string> {
  tabs: readonly { id: T; label: string }[];
  active: T;
  onChange: (id: T) => void;
  className?: string;
}

export default function Tabs<T extends string>({ tabs, active, onChange, className = "" }: Props<T>) {
  return (
    <div className={`inline-flex gap-1 rounded-md border border-edge bg-base/50 p-0.5 ${className}`}>
      {tabs.map((t) => {
        const on = t.id === active;
        return (
          <button
            key={t.id}
            onClick={() => onChange(t.id)}
            className={
              "rounded px-3 py-1 font-mono text-xs font-medium transition-colors " +
              (on ? "bg-amber text-coal" : "text-muted hover:text-ink")
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
