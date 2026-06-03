// Select.tsx — styled native select (keeps native a11y + mobile pickers).
import type { SelectHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import { FIELD_BASE } from "./Input";

export default function Select({ className = "", children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className="relative">
      <select className={`${FIELD_BASE} h-10 appearance-none pr-9 ${className}`} {...rest}>
        {children}
      </select>
      <ChevronDown
        size={15}
        className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-muted"
      />
    </div>
  );
}
