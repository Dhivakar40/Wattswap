"use client";

import { ErrorState } from "@/components/ui/primitives";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error;
  reset: () => void;
}): React.JSX.Element {
  return (
    <div className="mx-auto max-w-lg py-12">
      <ErrorState title="This page hit an error" error={error} onRetry={reset} />
    </div>
  );
}
