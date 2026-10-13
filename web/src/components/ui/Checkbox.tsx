import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";

import { cn } from "@/utils/cn";

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  /** The label text; may hold links, which stay clickable on their own. */
  children: ReactNode;
}

/**
 * A checkbox whose whole row is the target (WCAG 2.5.8): the label wraps the
 * input, so no id wiring is needed and a click anywhere on the text toggles it.
 * A link inside the label follows its own href instead of toggling.
 */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { children, className, ...rest },
  ref,
) {
  return (
    <label className="flex min-h-10 cursor-pointer items-start gap-3 py-2 text-sm leading-snug text-text-primary">
      <input
        ref={ref}
        type="checkbox"
        className={cn(
          "mt-px size-5 shrink-0 cursor-pointer accent-action-primary",
          "focus-visible:outline-none focus-visible:shadow-focus-ring",
          className,
        )}
        {...rest}
      />
      <span>{children}</span>
    </label>
  );
});
