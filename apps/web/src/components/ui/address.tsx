"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

export function shortAddress(address: string, head = 6, tail = 4): string {
  return address.length <= head + tail + 1
    ? address
    : `${address.slice(0, head)}…${address.slice(-tail)}`;
}

/** An address or hash, shortened, with an accessible copy button. The full value is in `title`. */
export function Address({
  value,
  href,
  className,
}: {
  value: string;
  href?: string | undefined;
  className?: string;
}): React.JSX.Element {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  React.useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (insecure context): the full value stays selectable in the title */
    }
  }
  const text = shortAddress(value);
  return (
    <span className={cn("inline-flex items-center gap-1 font-mono text-sm", className)}>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          title={value}
          className="hover:underline"
        >
          {text}
        </a>
      ) : (
        <span title={value}>{text}</span>
      )}
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={copied ? "Copied" : `Copy ${value}`}
        className="text-muted-foreground hover:text-foreground hover:bg-surface-2 inline-flex size-6 items-center justify-center rounded-md"
      >
        {copied ? (
          <Check aria-hidden className="size-3.5" />
        ) : (
          <Copy aria-hidden className="size-3.5" />
        )}
      </button>
    </span>
  );
}
