import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ Spinner */
export function Spinner({
  className,
  label,
}: {
  className?: string;
  label?: string;
}): React.JSX.Element {
  return (
    <span role="status" className="inline-flex items-center">
      <Loader2 aria-hidden className={cn("size-4 animate-spin", className)} />
      <span className="sr-only">{label ?? "Loading"}</span>
    </span>
  );
}

/* ----------------------------------------------------------------- Skeleton */
export function Skeleton({ className, ...props }: React.ComponentProps<"div">): React.JSX.Element {
  return (
    <div
      aria-hidden
      data-slot="skeleton"
      className={cn(
        "bg-surface-3 animate-shimmer rounded-md bg-[linear-gradient(90deg,transparent,hsl(0_0%_100%/0.06),transparent)] bg-[length:200%_100%]",
        className,
      )}
      {...props}
    />
  );
}

/* --------------------------------------------------------------------- Card */
export function Card({ className, ...props }: React.ComponentProps<"section">): React.JSX.Element {
  return (
    <section
      data-slot="card"
      className={cn("bg-card text-card-foreground shadow-e1 rounded-xl border", className)}
      {...props}
    />
  );
}
export function CardHeader({
  className,
  ...props
}: React.ComponentProps<"div">): React.JSX.Element {
  return (
    <div
      className={cn("flex items-start justify-between gap-3 p-4 pb-2 sm:p-5 sm:pb-2", className)}
      {...props}
    />
  );
}
export function CardTitle({ className, ...props }: React.ComponentProps<"h2">): React.JSX.Element {
  return <h2 className={cn("text-muted-foreground text-sm font-medium", className)} {...props} />;
}
export function CardBody({ className, ...props }: React.ComponentProps<"div">): React.JSX.Element {
  return <div className={cn("p-4 pt-2 sm:p-5 sm:pt-2", className)} {...props} />;
}

/* -------------------------------------------------------------------- Badge */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "bg-surface-2 text-foreground border-border",
        success: "bg-success/10 text-success border-success/30",
        warning: "bg-warning/10 text-warning border-warning/30",
        danger: "bg-destructive/10 text-destructive border-destructive/30",
        info: "bg-info/10 text-info border-info/30",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);
export function Badge({
  className,
  tone,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>): React.JSX.Element {
  return <span data-slot="badge" className={cn(badgeVariants({ tone }), className)} {...props} />;
}

/* -------------------------------------------------------------------- Alert */
const alertVariants = cva("flex gap-3 rounded-lg border p-3 text-sm", {
  variants: {
    tone: {
      info: "border-info/40 bg-info/10",
      warning: "border-warning/40 bg-warning/10",
      danger: "border-destructive/40 bg-destructive/10",
      success: "border-success/40 bg-success/10",
    },
  },
  defaultVariants: { tone: "info" },
});
export function Alert({
  className,
  tone,
  title,
  children,
  action,
  ...props
}: Omit<React.ComponentProps<"div">, "title"> &
  VariantProps<typeof alertVariants> & {
    title?: string;
    action?: React.ReactNode;
  }): React.JSX.Element {
  return (
    <div
      role={tone === "danger" || tone === "warning" ? "alert" : "status"}
      className={cn(alertVariants({ tone }), className)}
      {...props}
    >
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className="text-muted-foreground mt-0.5">{children}</div> : null}
      </div>
      {action ? <div className="shrink-0 self-center">{action}</div> : null}
    </div>
  );
}

/* ----------------------------------------------------------------- Form bits */
export function Label({ className, ...props }: React.ComponentProps<"label">): React.JSX.Element {
  return <label className={cn("text-sm leading-none font-medium", className)} {...props} />;
}

export function Input({
  className,
  type = "text",
  ...props
}: React.ComponentProps<"input">): React.JSX.Element {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "bg-surface-1 border-input placeholder:text-muted-foreground h-9 w-full min-w-0 rounded-lg border px-3 text-sm transition-colors outline-none",
        "focus-visible:border-ring focus-visible:ring-ring/40 focus-visible:ring-2",
        "aria-invalid:border-destructive aria-invalid:ring-destructive/30 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

/* -------------------------------------------------------- Segmented control */
export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
}
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  className,
}: {
  label: string;
  value: T;
  options: ReadonlyArray<SegmentedOption<T>>;
  onChange(value: T): void;
  className?: string;
}): React.JSX.Element {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("bg-surface-2 inline-flex rounded-lg border p-0.5", className)}
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          onClick={() => onChange(o.value)}
          className={cn(
            "h-7 min-w-10 rounded-md px-2.5 text-xs font-medium transition-colors",
            o.value === value
              ? "bg-surface-1 text-foreground shadow-e1"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* --------------------------------------------------------------- Empty/Error */
export function EmptyState({
  icon,
  title,
  children,
  action,
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}): React.JSX.Element {
  return (
    <div className={cn("flex flex-col items-center gap-2 px-4 py-8 text-center", className)}>
      {icon ? (
        <div className="text-muted-foreground" aria-hidden>
          {icon}
        </div>
      ) : null}
      <p className="font-medium">{title}</p>
      {children ? <p className="text-muted-foreground max-w-sm text-sm">{children}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  error,
  onRetry,
  className,
}: {
  title?: string;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}): React.JSX.Element {
  const detail =
    error instanceof Error ? error.message : typeof error === "string" ? error : undefined;
  return (
    <div
      role="alert"
      className={cn("flex flex-col items-center gap-2 px-4 py-8 text-center", className)}
    >
      <p className="text-destructive font-medium">{title}</p>
      {detail ? (
        <p className="text-muted-foreground max-w-sm text-sm break-words">{detail}</p>
      ) : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="border-border-strong hover:bg-surface-2 mt-1 h-8 rounded-lg border px-3 text-sm font-medium"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------- Stat */
export function Stat({
  label,
  value,
  hint,
  tone,
  loading,
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: "default" | "buy" | "sell";
  loading?: boolean;
  className?: string;
}): React.JSX.Element {
  return (
    <div className={cn("min-w-0", className)}>
      <p className="text-muted-foreground text-xs font-medium">{label}</p>
      {loading ? (
        <Skeleton className="mt-1.5 h-7 w-28" />
      ) : (
        <p
          className={cn(
            "tabular mt-0.5 truncate font-mono text-2xl font-semibold tracking-tight",
            tone === "buy" && "text-buy",
            tone === "sell" && "text-sell",
          )}
        >
          {value}
        </p>
      )}
      {hint ? <p className="text-muted-foreground mt-0.5 text-xs">{hint}</p> : null}
    </div>
  );
}
