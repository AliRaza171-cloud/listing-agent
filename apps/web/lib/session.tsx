"use client";

import { createContext, useContext } from "react";
import type { User } from "./api";

export type SessionValue = {
  user: User;
  credits: number | null;
  refreshCredits: () => Promise<unknown>;
};

export const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside the signed-in layout");
  return value;
}
