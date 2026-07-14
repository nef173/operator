"use client";

import { useState, type ReactNode } from "react";
import { Card } from "@/components/ui";
import { PageHeader } from "@/components/PageState";
import { DetailModal } from "@/components/DetailModal";

// The Guide is a friendly first-time walkthrough — written for someone opening the app for
// the very first time, with zero technical words. Two layers:
//   - the page  = a short, plain one-liner per part
//   - a click   = a popup that explains that part in simple terms (what it's for, what you do)
// It reads no API. Keep it simple if the app changes — describe what the operator sees/does,
// never how it's built.

type Part = {
  n: number;
  title: string;
  one: string; // the plain one-liner
  detail: ReactNode; // the popup body
};

function P({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <p className={`text-sm leading-relaxed text-[var(--text)] ${className}`}>{children}</p>;
}
function H({ children }: { children: ReactNode }) {
  return (
    <h4 className="mt-4 mb-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--muted)]">
      {children}
    </h4>
  );
}
function Bullets({ items }: { items: ReactNode[] }) {
  return (
    <ul className="ml-4 mt-1 list-disc space-y-1.5">
      {items.map((it, i) => (
        <li key={i} className="text-sm leading-relaxed text-[var(--text)]">
          {it}
        </li>
      ))}
    </ul>
  );
}

const PARTS: Part[] = [
  {
    n: 1,
    title: "What this app is for",
    one: "It helps you find products to sell and turn them into store listings.",
    detail: (
      <>
        <P>
          This app does two big things for you: it <b>finds products worth selling</b>, and it{" "}
          <b>builds those products into listings</b> for your store.
        </P>
        <P>
          You stay in charge the whole time. The app does the slow, repetitive work and then shows
          you the result — you just look it over and say yes or no.
        </P>
        <H>The simplest way to think about it</H>
        <P>
          The left menu is your map. The top groups (<b>Research</b>) are about finding products.
          The middle group (<b>Listings</b>) is about building and publishing them. The bottom
          group (<b>System</b>) is settings and this guide.
        </P>
      </>
    ),
  },
  {
    n: 2,
    title: "Finding products",
    one: "Four ways to discover products — they all add to one shortlist you choose from.",
    detail: (
      <>
        <P>
          There are four different ways to find products. You don&apos;t have to use all of them —
          each one looks for good products in its own way:
        </P>
        <Bullets
          items={[
            <><b>Keyword Research</b> — finds products people are actively searching for.</>,
            <><b>Trend Research</b> — spots products getting popular, so you can sell early.</>,
            <><b>Winning Products</b> — watches your competitors and what&apos;s selling for them.</>,
            <><b>Marketplace</b> — finds products that are taking off on big shopping sites.</>,
          ]}
        />
        <P>
          Whatever they find, the best ones all go into <b>one shared shortlist</b>. That way you
          have a single place to pick from instead of four separate lists.
        </P>
      </>
    ),
  },
  {
    n: 3,
    title: "The shortlist",
    one: "Your running list of products worth a closer look. You promote the good ones.",
    detail: (
      <>
        <P>
          As the app finds products, the promising ones land on a <b>shortlist</b> — think of it
          as your &quot;maybe&quot; pile.
        </P>
        <P>
          When you like one, you <b>promote</b> it. That moves it out of the maybe pile and into a
          store, ready to be built into a real listing.
        </P>
        <P className="text-[var(--muted)]">
          Nothing on the shortlist costs you anything or goes live — it&apos;s just ideas waiting
          for your pick.
        </P>
      </>
    ),
  },
  {
    n: 4,
    title: "Competitor spy",
    one: "Tracks competitor stores and keeps only the ones really advertising on Google.",
    detail: (
      <>
        <P>
          The <b>Winning Products</b> page keeps a list of competitor stores you&apos;re watching.
          For each one it shows you their traffic, what&apos;s selling best, and how many products
          they carry.
        </P>
        <H>Why some stores get rejected</H>
        <P>
          When you add a new competitor, the app checks whether that store is{" "}
          <b>actually running ads on Google Shopping</b>. If it is, it&apos;s added. If it isn&apos;t,
          it&apos;s skipped — and the app tells you why.
        </P>
        <P>
          The reason: you only want to learn from stores that are doing the same thing you are —
          selling through Google. A store with no Google ads isn&apos;t a useful example.
        </P>
      </>
    ),
  },
  {
    n: 5,
    title: "The plan (SKU Plan)",
    one: "Turns one keyword into a shopping list, then goes and finds those products for you.",
    detail: (
      <>
        <P>
          Say you want to sell around a topic — like &quot;dog cooling mat&quot;. The{" "}
          <b>SKU Plan</b> turns that one idea into a proper shopping list: how many products to
          find, and which related searches to cover.
        </P>
        <P>It tries to be smart about it:</P>
        <Bullets
          items={[
            <>It puts most of the effort on the main product, then spreads the rest across related ones.</>,
            <>It avoids finding the same product over and over.</>,
            <>It leans on Google sources first, since that&apos;s where you sell.</>,
          ]}
        />
        <P>
          When you&apos;re happy with the plan, you press go and it starts hunting for those exact
          products.
        </P>
      </>
    ),
  },
  {
    n: 6,
    title: "Building a listing",
    one: "Turns a chosen product into a draft listing — it never goes live on its own.",
    detail: (
      <>
        <P>
          Once you promote a product, the app builds it into a real listing: the title, the
          description, the images, the price.
        </P>
        <H>Two things to know</H>
        <Bullets
          items={[
            <><b>It always starts as a draft.</b> A new product is never published automatically. It waits until you decide to make it live.</>,
            <><b>It checks the images for you.</b> If a photo has a problem it can fix (like a leftover logo or the wrong language), it cleans it up. If a photo is unusable, it flags it. You look it over before anything is saved.</>,
          ]}
        />
        <P>
          Each product also moves through simple stages — <b>draft</b>, then <b>live</b>, then{" "}
          <b>testing</b>, and finally <b>winner</b> or <b>dropped</b> — so you always know where it
          stands.
        </P>
      </>
    ),
  },
  {
    n: 7,
    title: "How much it does on its own",
    one: "For each step you choose: do it myself, ask me first, or just do it.",
    detail: (
      <>
        <P>
          You decide how hands-on you want to be. On the <b>Autonomy</b> page, each step can be set
          to one of three levels:
        </P>
        <Bullets
          items={[
            <><b>Manual</b> — nothing happens unless you click. You&apos;re fully in control.</>,
            <><b>Ask me first</b> — the app gets the work ready and asks for your yes before doing it.</>,
            <><b>Automatic</b> — the app just does it, on the schedule you pick.</>,
          ]}
        />
        <P className="text-[var(--muted)]">
          A common setup: let the app handle routine work automatically, but ask you first on the
          bigger decisions like which keywords or trends to chase.
        </P>
      </>
    ),
  },
  {
    n: 8,
    title: "Keys & settings",
    one: "Enter your logins once in Connections — everything else just uses them.",
    detail: (
      <>
        <P>
          For the app to do real work, it needs a few logins — your store, and the services it uses
          to look up data and make images.
        </P>
        <P>
          You enter each of these once, in <b>Settings → Connections</b>. After that, every part of
          the app that needs them just uses them. You never have to type them again.
        </P>
        <P className="text-[var(--muted)]">
          Your logins are kept private and shown only as hidden dots — never spelled out back to
          you.
        </P>
      </>
    ),
  },
  {
    n: 9,
    title: "Keeping track",
    one: "Everything the app does is written down, so you can always see what happened.",
    detail: (
      <>
        <P>
          Every action — a product found, a listing built, a change made — is recorded. You&apos;re
          never left guessing what the app did.
        </P>
        <Bullets
          items={[
            <><b>Activity</b> shows the running history of what&apos;s been done.</>,
            <><b>Decisions</b> is your inbox — anything waiting for your yes or no sits here.</>,
            <><b>Costs</b> shows roughly what your usage adds up to.</>,
          ]}
        />
      </>
    ),
  },
  {
    n: 10,
    title: "The whole thing, start to finish",
    one: "Find products → pick the good ones → build drafts → you publish. That's the loop.",
    detail: (
      <>
        <P>Here&apos;s the full journey in one breath:</P>
        <Bullets
          items={[
            <>The app <b>finds products</b> and adds the best to your shortlist.</>,
            <>You <b>pick one</b> and promote it into a store.</>,
            <>The app <b>builds the listing</b> — title, images, details — as a draft.</>,
            <>You <b>review and publish</b> when you&apos;re ready.</>,
          ]}
        />
        <P>
          You repeat that loop to keep adding products. How much of it happens on its own is
          entirely your choice — set it on the Autonomy page.
        </P>
      </>
    ),
  },
];

export default function GuidePage() {
  const [open, setOpen] = useState<Part | null>(null);

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="Guide"
        subtitle="New here? This explains what the app does, in plain words. Click any card to learn more."
      />

      <div className="grid gap-2.5 sm:grid-cols-2">
        {PARTS.map((p) => (
          <button key={p.n} type="button" onClick={() => setOpen(p)} className="text-left">
            <Card className="h-full p-4 transition-colors hover:bg-[var(--surface-2)]">
              <div className="flex items-center gap-2.5">
                <span
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold"
                  style={{ background: "color-mix(in srgb, var(--accent) 16%, transparent)", color: "var(--accent)" }}
                >
                  {p.n}
                </span>
                <h2 className="text-sm font-semibold">{p.title}</h2>
              </div>
              <p className="mt-2 text-[13px] leading-relaxed text-[var(--muted)]">{p.one}</p>
              <span className="mt-2 inline-block text-[12px] font-medium" style={{ color: "var(--accent)" }}>
                Tell me more →
              </span>
            </Card>
          </button>
        ))}
      </div>

      <DetailModal
        open={open !== null}
        onClose={() => setOpen(null)}
        title={open ? open.title : ""}
        subtitle={open?.one}
      >
        {open ? <div className="space-y-1">{open.detail}</div> : null}
      </DetailModal>
    </div>
  );
}
