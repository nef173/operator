"use client";

import { useEffect, useState } from "react";

// "Embedded" = this app is running INSIDE the NN Operations dashboard, which mounts it in an
// iframe. In that mode NN is the one and only shell, so we hide the operator's OWN cross-app
// chrome — the AppSwitcher menu ("Operation System" / Frontend·Backend), the "back to
// Dashboard" links, and the standalone landing — leaving just the app's functional pages.
//
// The signal is simply "are we in an iframe" (window.self !== window.top): NN always embeds via
// iframe, so this needs no query param and survives every client-side navigation. ?embed=1
// (persisted to localStorage) is a belt-and-suspenders backup for same-origin edge cases.
export function isEmbedded(): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (window.self !== window.top) return true;
  } catch {
    // A cross-origin frame throwing on the access IS itself proof we're framed.
    return true;
  }
  try {
    if (localStorage.getItem("nn_embed") === "1") return true;
  } catch {
    /* localStorage unavailable — fall through */
  }
  return new URLSearchParams(window.location.search).get("embed") === "1";
}

// Client hook. Returns false on the first (SSR/hydration) render, then the real value after
// mount — so components render their standalone form on the server and drop the shell on the
// client without a hydration mismatch.
export function useEmbedded(): boolean {
  const [embedded, setEmbedded] = useState(false);
  useEffect(() => {
    try {
      if (new URLSearchParams(window.location.search).get("embed") === "1") {
        localStorage.setItem("nn_embed", "1");
      }
    } catch {
      /* ignore */
    }
    setEmbedded(isEmbedded());
  }, []);
  return embedded;
}
