// Input.tsx + Field — text input primitive and a label/hint wrapper.
import type { InputHTMLAttributes, ReactNode } from "react";

const FIELD_BASE =
  "w-full rounded-md border border-edge bg-base/60 px-3 text-sm text-ink placeholder:text-muted/70 " +
  "transition-colors focus-visible:border-amber/60 focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-amber/30 disabled:opacity-50";

export default function Input({ className = "", ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${FIELD_BASE} h-10 ${className}`} {...rest} />;
}

interface FieldProps {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}

/** Label + control + optional hint. Use around Input/Select/textarea. */
export function Field({ label, hint, htmlFor, children, className = "" }: FieldProps) {
  return (
    <label htmlFor={htmlFor} className={`block ${className}`}>
      <span className="mb-1.5 block font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
        {label}
      </span>
      {children}
      {hint ? <span className="mt-1 block text-[11px] text-muted">{hint}</span> : null}
    </label>
  );
}

export { FIELD_BASE };
