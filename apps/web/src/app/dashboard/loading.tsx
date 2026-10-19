import { Skeleton } from "@/components/ui/primitives";

export default function Loading(): React.JSX.Element {
  return (
    <div className="space-y-5" aria-busy="true">
      <span role="status" className="sr-only">
        Loading dashboard
      </span>
      <Skeleton className="h-8 w-48" />
      <div className="grid gap-4 lg:grid-cols-2">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-48 rounded-xl" />
        ))}
      </div>
    </div>
  );
}
