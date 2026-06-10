// TagEditor.tsx — capability-tags chip editor (P-0044). Lets an operator pick a
// provider's routing tags from the known vocabulary (so a provider can be aligned
// to what tasks require) plus add custom tags. Pure controlled component.
import { useState } from "react";
import { Plus, X } from "lucide-react";

// The routing vocabulary used by built-in providers + seed tasks (router.py / seed.py).
export const KNOWN_CAPABILITY_TAGS = [
  "synthesis", "coding", "longcontext", "realtime", "markets", "frontier", "open", "local", "any",
] as const;

interface Props {
  value: string[];
  onChange: (tags: string[]) => void;
  /** Tags shown as quick-add suggestions; defaults to the known vocabulary. */
  suggestions?: readonly string[];
}

export default function TagEditor({ value, onChange, suggestions = KNOWN_CAPABILITY_TAGS }: Props) {
  const [draft, setDraft] = useState("");

  const add = (raw: string) => {
    const t = raw.trim();
    if (t && !value.includes(t)) onChange([...value, t]);
    setDraft("");
  };
  const remove = (t: string) => onChange(value.filter((x) => x !== t));

  const unused = suggestions.filter((s) => !value.includes(s));

  return (
    <div className="space-y-2">
      {/* Selected tags */}
      <div className="flex flex-wrap items-center gap-1.5">
        {value.length === 0 && (
          <span className="font-mono text-[11px] text-muted">
            no tags — provider matches only tasks with no required tag
          </span>
        )}
        {value.map((t) => (
          <span key={t} className="flex items-center gap-1 rounded-full border border-brand/40 bg-brand/10 px-2 py-0.5 font-mono text-[11px] text-ink">
            {t}
            <button type="button" onClick={() => remove(t)} className="text-muted hover:text-bad" aria-label={`Remove ${t}`}>
              <X size={10} />
            </button>
          </span>
        ))}
      </div>

      {/* Free-text add */}
      <div className="flex items-center gap-1.5">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(draft); }
          }}
          placeholder="add a tag…"
          className="w-32 rounded border border-edge bg-base px-2 py-0.5 font-mono text-[11px] text-ink outline-none focus:border-brand/50"
        />
        <button type="button" onClick={() => add(draft)} disabled={!draft.trim()}
          className="flex items-center gap-0.5 font-mono text-[11px] text-brand hover:text-ink disabled:text-muted">
          <Plus size={11} /> add
        </button>
      </div>

      {/* Quick-add from the known vocabulary */}
      {unused.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          {unused.map((s) => (
            <button key={s} type="button" onClick={() => add(s)}
              className="rounded-full border border-edge px-2 py-0.5 font-mono text-[10px] text-muted hover:border-brand/40 hover:text-brand">
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
