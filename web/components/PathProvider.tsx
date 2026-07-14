"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { DEFAULT_PATH, type PipelinePath } from "@/lib/pipeline";

type PathCtx = {
  path: PipelinePath;
  setPath: (p: PipelinePath) => void;
  ready: boolean;
};

const Ctx = createContext<PathCtx | null>(null);
const STORAGE_KEY = "gs-operator-path";

// Holds the operator's active path (General vs Niche), persisted to
// localStorage so it sticks across navigations + reloads. `ready` flips true
// after the stored value is read, so consumers can avoid a default-path flash.
export function PathProvider({ children }: { children: React.ReactNode }) {
  const [path, setPathState] = useState<PipelinePath>(DEFAULT_PATH);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "general" || stored === "niche") setPathState(stored);
    setReady(true);
  }, []);

  const setPath = (p: PipelinePath) => {
    setPathState(p);
    window.localStorage.setItem(STORAGE_KEY, p);
  };

  return <Ctx.Provider value={{ path, setPath, ready }}>{children}</Ctx.Provider>;
}

export function usePath(): PathCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("usePath must be used within PathProvider");
  return ctx;
}
