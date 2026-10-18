import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiStatus } from "@/components/api-status";

describe("ApiStatus", () => {
  it("shows online state", () => {
    render(<ApiStatus health={{ ok: true, status: "ok" }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Online (ok)");
  });
  it("shows an error state", () => {
    render(<ApiStatus health={{ ok: false, error: "HTTP 503" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Unreachable: HTTP 503");
  });
});
