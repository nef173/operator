"use client";

import { useEffect, useState } from "react";

// Read a single URL query param on the client. Returns null on the first render
// (server/hydration) then the real value after mount — enough for seeding a
// keyword handoff without pulling in a Suspense boundary.
export function useQueryParam(name: string): string | null {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setValue(params.get(name));
  }, [name]);
  return value;
}
