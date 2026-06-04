// Modal.tsx — overlay dialog. Backdrop click + Esc close; body scroll-locked.
import { useEffect, type ReactNode } from "react";
import { X } from "lucide-react";

interface Props {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  /** Footer actions (e.g. Cancel / Save). */
  footer?: ReactNode;
  /** max-width utility, e.g. "max-w-lg" (default) | "max-w-2xl". */
  size?: string;
}

export default function Modal({ open, onClose, title, children, footer, size = "max-w-lg" }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-coal/60 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        className={
          `flex max-h-[92vh] w-full ${size} animate-rise-in flex-col overflow-hidden rounded-t-xl border ` +
          "border-edge bg-panel shadow-2xl sm:rounded-xl"
        }
      >
        {title ? (
          <div className="flex items-center justify-between border-b border-edge px-5 py-3.5">
            <h2 className="font-mono text-sm font-semibold tracking-tight text-ink">{title}</h2>
            <button
              onClick={onClose}
              aria-label="Close"
              className="rounded-md p-1 text-muted hover:bg-ink/5 hover:text-ink"
            >
              <X size={16} />
            </button>
          </div>
        ) : null}
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-edge px-5 py-3">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}
