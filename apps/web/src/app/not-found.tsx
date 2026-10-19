import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/primitives";

export default function NotFound(): React.JSX.Element {
  return (
    <EmptyState
      className="py-16"
      title="Page not found"
      action={
        <Link href="/" className={buttonVariants({ variant: "outline" })}>
          Back to home
        </Link>
      }
    >
      That page does not exist.
    </EmptyState>
  );
}
