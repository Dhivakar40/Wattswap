"use client";

import * as React from "react";
import { Dialog as D, DropdownMenu as M, Tooltip as T } from "radix-ui";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/* ------------------------------------------------------------------ Dialog */
export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

export function DialogContent({
  className,
  children,
  title,
  description,
  hideClose,
  side = "center",
  ...props
}: Omit<React.ComponentProps<typeof D.Content>, "title"> & {
  title: string;
  description?: string;
  hideClose?: boolean;
  side?: "center" | "left";
}): React.JSX.Element {
  return (
    <D.Portal>
      <D.Overlay className="data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0 fixed inset-0 z-50 bg-black/60 backdrop-blur-[2px]" />
      <D.Content
        className={cn(
          "bg-popover text-popover-foreground shadow-e3 fixed z-50 border outline-none",
          "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
          side === "center"
            ? "top-1/2 left-1/2 w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-xl p-5"
            : "data-[state=open]:slide-in-from-left data-[state=closed]:slide-out-to-left inset-y-0 left-0 w-[min(20rem,85vw)] p-4",
          className,
        )}
        {...props}
      >
        <D.Title className="text-lg font-semibold">{title}</D.Title>
        {description ? (
          <D.Description className="text-muted-foreground mt-1 text-sm">
            {description}
          </D.Description>
        ) : (
          <D.Description className="sr-only">{title}</D.Description>
        )}
        <div className="mt-4">{children}</div>
        {hideClose ? null : (
          <D.Close
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground hover:bg-surface-2 absolute top-3 right-3 inline-flex size-8 items-center justify-center rounded-md"
          >
            <X aria-hidden className="size-4" />
          </D.Close>
        )}
      </D.Content>
    </D.Portal>
  );
}

/* ---------------------------------------------------------- Dropdown menu */
export const DropdownMenu = M.Root;
export const DropdownMenuTrigger = M.Trigger;

export function DropdownMenuContent({
  className,
  ...props
}: React.ComponentProps<typeof M.Content>): React.JSX.Element {
  return (
    <M.Portal>
      <M.Content
        align="end"
        sideOffset={6}
        className={cn(
          "bg-popover text-popover-foreground shadow-e2 z-50 min-w-48 rounded-lg border p-1",
          "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
          className,
        )}
        {...props}
      />
    </M.Portal>
  );
}

export function DropdownMenuItem({
  className,
  ...props
}: React.ComponentProps<typeof M.Item>): React.JSX.Element {
  return (
    <M.Item
      className={cn(
        "data-[highlighted]:bg-surface-3 flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2.5 text-sm outline-none select-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuLabel({
  className,
  ...props
}: React.ComponentProps<typeof M.Label>): React.JSX.Element {
  return (
    <M.Label className={cn("text-muted-foreground px-2.5 py-1.5 text-xs", className)} {...props} />
  );
}

export function DropdownMenuSeparator({ className }: { className?: string }): React.JSX.Element {
  return <M.Separator className={cn("bg-border -mx-1 my-1 h-px", className)} />;
}

/* ----------------------------------------------------------------- Tooltip */
export const TooltipProvider = T.Provider;

export function Tooltip({
  content,
  children,
  side = "bottom",
}: {
  content: React.ReactNode;
  children: React.ReactElement;
  side?: "top" | "bottom" | "left" | "right";
}): React.JSX.Element {
  return (
    <T.Root delayDuration={200}>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content
          side={side}
          sideOffset={6}
          className="bg-popover text-popover-foreground shadow-e2 z-50 max-w-72 rounded-md border px-2.5 py-1.5 text-xs"
        >
          {content}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}
