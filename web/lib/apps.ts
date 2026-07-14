// The app registry — the single source of truth for the apps the operation's
// "Frontend" exposes from the Operation-System shell. Apps span the WHOLE Google
// operation (every store); store separation lives inside each app's own store/path
// selector, not at the shell. Adding a 3rd app later is one entry here plus its route.
// `status: "soon"` renders an app as coming-soon (no navigation).

export type AppStatus = "live" | "soon";

// The operation has two faces (mirrored by the shell's Frontend/Backend selector):
//   • "frontend" — the BUILD apps: finding + listing product (research, feed, multimarket).
//                  This is the "make the store" half of the operation.
//   • "backend"  — the OPERATION backend: the post-launch RUN half (P&L, product management,
//                  issue management, tasks, store management) PLUS the control plane
//                  (settings/connections, the run log, cost accounting). Everything you
//                  OPERATE rather than build.
// Each app declares which face it belongs to; the shell + switcher filter by it so the
// Frontend tab shows only build apps and the Backend tab shows the whole operation backend.
export type AppFace = "frontend" | "backend";

// Backend apps are rendered in grouped section lines on the dashboard: Finance (money),
// Backend (product / issues / stores), Operation (tasks & gameplans), App Settings
// (settings, activity log, costs). Frontend apps have no group (one line).
export type AppGroup = "finance" | "backend" | "operation" | "app-settings";

export interface OperatorApp {
  id: string;
  name: string;
  tagline: string;
  href: string; // where the app opens (ignored when status === "soon")
  status: AppStatus;
  face: AppFace;
  group?: AppGroup;
  // A short SVG path-data set is rendered by the shell; kept as an id so the shell owns
  // the markup (no JSX in this module).
  icon:
    | "research"
    | "feed"
    | "multimarket"
    | "finance"
    | "company-pl"
    | "product"
    | "issues"
    | "tasks"
    | "store"
    | "activity"
    | "settings"
    | "costs";
}

// One flat registry for the WHOLE operation. `OPERATOR_APPS` (Frontend) and `BACKEND_APPS`
// (Backend) below are just this list filtered by `face`, so adding an app or moving it
// between faces is a one-line change here and every surface (dashboard, switcher, RBAC)
// follows automatically.
export const ALL_APPS: OperatorApp[] = [
  // ── FRONTEND: the BUILD half — find product, list it, optimize the feed, localize. ──
  {
    id: "research-listing",
    name: "Research & Listing",
    tagline:
      "The full pipeline — discovery, SKU planning, sourcing match, daily listings, and the control layer.",
    href: "/home",
    status: "live",
    face: "frontend",
    icon: "research",
  },
  {
    id: "product-feed",
    name: "Product Feed & Optimization",
    tagline:
      "GMC feed-readiness for the catalog — title / category / image / SEO checks and proposed title rewrites.",
    href: "/feed/optimize",
    status: "live",
    face: "frontend",
    icon: "feed",
  },
  // ── BACKEND · the CONTROL PLANE — configure, observe, and account for the operation. ──
  {
    id: "settings",
    name: "Settings & Connections",
    tagline:
      "The one setup surface — global API/AI keys, per-store Shopify/Google credentials, integrations, roles, and system health.",
    href: "/settings",
    status: "live",
    face: "backend",
    group: "app-settings",
    icon: "settings",
  },
  {
    id: "activity",
    name: "Log / Activity",
    tagline:
      "The unified run log — every action, job, and write across the operation, newest first.",
    href: "/activity",
    status: "live",
    face: "backend",
    group: "app-settings",
    icon: "activity",
  },
  {
    id: "costs",
    name: "Costs",
    tagline:
      "Estimated operation expenses — editable unit costs × per-spec recipes, rolled up to a monthly run-rate.",
    href: "/costs",
    status: "live",
    face: "backend",
    group: "app-settings",
    icon: "costs",
  },
];

// Face-filtered views the surfaces consume. Frontend = the build apps; Backend = the whole
// operation backend (run half + control plane). Derived from ALL_APPS so a `face` change
// above is the single edit needed to move an app between tabs.
export const OPERATOR_APPS: OperatorApp[] = ALL_APPS.filter((a) => a.face === "frontend");
export const BACKEND_APPS: OperatorApp[] = ALL_APPS.filter((a) => a.face === "backend");

// The dashboard's backend section lines, in render order. Each pulls its apps from
// BACKEND_APPS by `group`, so moving an app between lines is a one-field change above.
export const BACKEND_GROUPS: { group: AppGroup; title: string; note: string }[] = [
  { group: "app-settings", title: "App Settings", note: "Settings & connections, activity log & app costs" },
];
