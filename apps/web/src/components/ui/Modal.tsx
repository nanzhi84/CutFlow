import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

type ModalSize = "sm" | "md" | "lg" | "xl" | "2xl" | "3xl";

type ModalProps = {
  isOpen: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  size?: ModalSize;
  showCloseButton?: boolean;
};

const sizeClasses: Record<ModalSize, string> = {
  sm: "max-w-md",
  md: "max-w-lg",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
  "2xl": "max-w-6xl",
  "3xl": "max-w-[88rem]",
};

let activeScrollLocks = 0;
let lockedScrollY = 0;
let savedBodyOverflow = "";
let savedBodyPosition = "";
let savedBodyTop = "";
let savedBodyWidth = "";
let savedHtmlOverflow = "";

export function Modal({ isOpen, onClose, title, children, size = "md", showCloseButton = true }: ModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;

    activeScrollLocks += 1;
    if (activeScrollLocks === 1) {
      lockedScrollY = window.scrollY;
      savedBodyOverflow = document.body.style.overflow;
      savedBodyPosition = document.body.style.position;
      savedBodyTop = document.body.style.top;
      savedBodyWidth = document.body.style.width;
      savedHtmlOverflow = document.documentElement.style.overflow;

      document.documentElement.style.overflow = "hidden";
      document.body.style.overflow = "hidden";
      document.body.style.position = "fixed";
      document.body.style.top = `-${lockedScrollY}px`;
      document.body.style.width = "100%";
    }

    return () => {
      activeScrollLocks = Math.max(0, activeScrollLocks - 1);
      if (activeScrollLocks === 0) {
        document.documentElement.style.overflow = savedHtmlOverflow;
        document.body.style.overflow = savedBodyOverflow;
        document.body.style.position = savedBodyPosition;
        document.body.style.top = savedBodyTop;
        document.body.style.width = savedBodyWidth;
        window.scrollTo(0, lockedScrollY);
      }
    };
  }, [isOpen]);

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && isOpen) onClose();
    };
    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      ref={overlayRef}
      className="fixed inset-0 z-[90] flex items-center justify-center overscroll-contain bg-black/50 p-4 animate-in fade-in"
      onMouseDown={(event) => {
        if (event.target === overlayRef.current) onClose();
      }}
      role="presentation"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`flex max-h-[90vh] w-full ${sizeClasses[size]} flex-col rounded-[24px] border border-border bg-surface shadow-2xl animate-in zoom-in-95`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        {title ? (
          <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-6 py-4">
            <h2 className="text-lg font-bold text-text-primary">{title}</h2>
            {showCloseButton ? (
              <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
                <X className="h-5 w-5" />
              </button>
            ) : null}
          </header>
        ) : null}
        <div className="overflow-y-auto overscroll-contain p-6">{children}</div>
      </section>
    </div>,
    document.body,
  );
}
