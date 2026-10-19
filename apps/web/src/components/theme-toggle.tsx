"use client";

import * as React from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

export function ThemeToggle(): React.JSX.Element {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  // Before hydration the real theme is unknown: render a neutral, still-labelled button.
  const next = mounted && resolvedTheme === "light" ? "dark" : "light";
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      data-testid="theme-toggle"
    >
      {mounted && resolvedTheme === "light" ? <Moon aria-hidden /> : <Sun aria-hidden />}
    </Button>
  );
}
