import { create } from "zustand";
import type { ConnectionStatus } from "@/lib/realtime/client";

interface ConnectionStore {
  status: ConnectionStatus;
  setStatus(status: ConnectionStatus): void;
}

export const useConnectionStore = create<ConnectionStore>((set) => ({
  status: "idle",
  setStatus: (status) => set({ status }),
}));
