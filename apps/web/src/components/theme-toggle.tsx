"use client";

import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

export function ThemeToggle(): React.JSX.Element {
  const { resolvedTheme, setTheme } = useTheme();
  const next = resolvedTheme === "light" ? "dark" : "light";
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
    >
      {next === "light" ? "Light mode" : "Dark mode"}
    </Button>
  );
}
