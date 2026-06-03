// Card.tsx — the raised-surface primitive (panel over the textured base).
import type { HTMLAttributes, ReactNode } from "react";

interface Props extends HTMLAttributes<HTMLDivElement> {
  /** Amber hairline + faint glow for the focused/active card. */
  active?: boolean;
  children: ReactNode;
}

export default function Card({ active = false, children, className = "", ...rest }: Props) {
  return (
    <div
      className={
        "rounded-lg border bg-panel/80 backdrop-blur-sm transition-colors " +
        (active ? "border-amber/50 shadow-[0_0_0_1px_rgb(var(--c-amber)/0.25)] " : "border-edge ") +
        className
      }
      {...rest}
    >
      {children}
    </div>
  );
}
