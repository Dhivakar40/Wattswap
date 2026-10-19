import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ChannelSync } from "@/lib/realtime/sync";

type Step =
  | { op: "message"; epoch: string; seq: number; data: number; expect: string; check?: Check }
  | { op: "snapshot"; epoch: string; seq: number; state: number[]; expect: string; check?: Check }
  | { op: "resync"; check?: Check }
  | { op: "resume_point"; expect: { epoch: string; seq: number } | null; check?: Check };

interface Check {
  state?: number[];
  epoch?: string | null;
  last_seq?: number | null;
  syncing?: boolean;
  gaps?: number;
  epoch_changes?: number;
}

interface Scenario {
  name: string;
  steps: Step[];
}

// The SAME file services/api/tests/realtime/test_sync_protocol.py::test_shared_vectors runs.
const file = resolve(__dirname, "../../../packages/shared/vectors/ws-sync.json");
const scenarios = (JSON.parse(readFileSync(file, "utf-8")) as { scenarios: Scenario[] }).scenarios;

describe("ChannelSync against the shared vectors (ws-sync.json)", () => {
  it("loads the vector file", () => {
    expect(scenarios.length).toBeGreaterThanOrEqual(8);
  });

  it.each(scenarios.map((s) => [s.name, s] as const))("%s", (_name, scenario) => {
    const s = new ChannelSync<number[], number>((state, d) => [...state, d]);
    scenario.steps.forEach((step, i) => {
      const where = `step ${i}: ${JSON.stringify(step)}`;
      switch (step.op) {
        case "message":
          expect(s.onMessage(step.epoch, step.seq, step.data), where).toBe(step.expect);
          break;
        case "snapshot":
          expect(s.loadSnapshot(step.epoch, step.seq, [...step.state]), where).toBe(step.expect);
          break;
        case "resync":
          s.onResync();
          break;
        case "resume_point":
          expect(s.resumePoint(), where).toEqual(step.expect);
          break;
      }
      const observed = {
        state: s.state,
        epoch: s.epoch,
        last_seq: s.lastSeq,
        syncing: s.syncing,
        gaps: s.gaps,
        epoch_changes: s.epochChanges,
      };
      for (const [key, want] of Object.entries(step.check ?? {})) {
        expect(observed[key as keyof typeof observed], `${where} -> ${key}`).toEqual(want);
      }
    });
  });
});
