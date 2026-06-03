// Button.tsx — the one button primitive. Variants encode intent; the amber
// `primary` is the single signal action per view (mission-control restraint).
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "outline" | "ghost" | "danger";
type Size = "sm" | "md";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
  children?: ReactNode;
}

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-md font-medium tracking-tight " +
  "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber/60 " +
  "disabled:cursor-not-allowed disabled:opacity-45";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-amber text-coal hover:bg-amber/90 active:bg-amber/80",
  outline: "border border-edge text-ink hover:border-amber/60 hover:text-amber",
  ghost: "text-muted hover:bg-ink/5 hover:text-ink",
  danger: "border border-bad/40 text-bad hover:bg-bad/10",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-xs",
  md: "h-10 px-4 text-sm",
};

export default function Button({
  variant = "outline",
  size = "md",
  icon,
  children,
  className = "",
  ...rest
}: Props) {
  return (
    <button className={`${BASE} ${VARIANTS[variant]} ${SIZES[size]} ${className}`} {...rest}>
      {icon}
      {children}
    </button>
  );
}
