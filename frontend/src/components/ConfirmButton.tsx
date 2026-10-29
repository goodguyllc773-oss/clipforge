import { useEffect, useRef, useState } from "react";

/** A button that needs a second click (within a few seconds) to actually fire.
 * Used instead of `window.confirm` for destructive actions: native dialogs
 * aren't stylable, aren't screenshot-testable, and are disabled entirely in
 * some automated/embedded browsers. */
export default function ConfirmButton({
  onConfirm, className = "tiny ghost bad", title, disabled, children, confirmLabel = "Click again to confirm",
}: {
  onConfirm: () => void | Promise<void>;
  className?: string;
  title?: string;
  disabled?: boolean;
  children: React.ReactNode;
  confirmLabel?: string;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  return (
    <button
      type="button"
      className={className}
      title={armed ? confirmLabel : title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        if (!armed) {
          setArmed(true);
          timer.current = setTimeout(() => setArmed(false), 3000);
          return;
        }
        clearTimeout(timer.current);
        setArmed(false);
        onConfirm();
      }}
    >
      {armed ? confirmLabel : children}
    </button>
  );
}
