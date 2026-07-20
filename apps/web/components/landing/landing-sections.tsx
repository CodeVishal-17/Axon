import { ArrowDown, ArrowRight, FileSearch, GitPullRequest, Network, Radar, ShieldCheck, Sparkles } from "lucide-react";
import { FeedSkeleton } from "@/components/feed/feed-skeleton";
import { PageContainer } from "@/components/layout/page-container";

const benefits = [
  { icon: Radar, title: "Detect documentation drift", body: "Know when a claim in your docs no longer matches the branch your team ships." },
  { icon: FileSearch, title: "Surface stale engineering knowledge", body: "Find issues, decisions, and operating guidance that have quietly stopped being true." },
  { icon: GitPullRequest, title: "Generate grounded pull requests", body: "Turn verified contradictions into minimal, reviewable corrections with evidence attached." },
];

const workflow = ["Repository", "Claims", "Verification", "Findings", "Pull Request"];

function SectionEyebrow({ children }: { children: React.ReactNode }) {
  return <p className="text-emerald-300 font-mono text-[11px] font-semibold tracking-[0.16em] uppercase">{children}</p>;
}

export function LandingSections() {
  return (
    <div>
      <section className="border-b border-border/60">
        <PageContainer className="py-20 sm:py-28">
          <div className="mx-auto max-w-xl text-center">
            <SectionEyebrow>The loop</SectionEyebrow>
            <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">From engineering knowledge to a source of truth.</h2>
          </div>
          <div className="mx-auto mt-12 flex max-w-5xl flex-col items-center gap-3 md:flex-row md:items-stretch md:gap-4">
            <JourneyCard step="01" title="Knowledge" body="Docs, issues, ADRs, and pull requests become clear, testable beliefs." icon={<Network className="size-5" />} />
            <ArrowDown className="text-muted-foreground size-5 md:hidden" aria-hidden />
            <ArrowRight className="text-muted-foreground mt-12 hidden size-5 shrink-0 md:block" aria-hidden />
            <JourneyCard step="02" title="Verification" body="Axon links each belief to the code and checks it against today’s reality." icon={<ShieldCheck className="size-5" />} />
            <ArrowDown className="text-muted-foreground size-5 md:hidden" aria-hidden />
            <ArrowRight className="text-muted-foreground mt-12 hidden size-5 shrink-0 md:block" aria-hidden />
            <JourneyCard step="03" title="Truth Feed" body="Contradictions arrive with the evidence and next action already prepared." icon={<Sparkles className="size-5" />} />
          </div>
        </PageContainer>
      </section>

      <section className="border-b border-border/60 bg-card/[0.16]">
        <PageContainer className="py-20 sm:py-28">
          <div className="max-w-xl">
            <SectionEyebrow>Why Axon</SectionEyebrow>
            <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">Keep the knowledge that guides your team as current as the code.</h2>
          </div>
          <div className="mt-12 grid gap-4 md:grid-cols-3">
            {benefits.map(({ icon: Icon, title, body }) => (
              <article key={title} className="border-border/70 bg-background/50 group rounded-xl border p-6 transition-colors hover:border-emerald-400/30 hover:bg-card motion-reduce:transition-none">
                <span className="border-border/60 bg-card text-emerald-300 inline-flex size-10 items-center justify-center rounded-lg border"><Icon className="size-5" aria-hidden /></span>
                <h3 className="mt-5 text-base font-semibold tracking-tight">{title}</h3>
                <p className="text-muted-foreground mt-2 text-sm leading-6">{body}</p>
              </article>
            ))}
          </div>
        </PageContainer>
      </section>

      <section className="border-b border-border/60">
        <PageContainer className="py-20 sm:py-28">
          <div className="mx-auto max-w-xl text-center">
            <SectionEyebrow>How it works</SectionEyebrow>
            <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">A continuous path from change to correction.</h2>
          </div>
          <ol className="mx-auto mt-12 flex max-w-5xl flex-col items-center justify-between gap-2 sm:flex-row sm:gap-0">
            {workflow.map((step, index) => (
              <li key={step} className="flex flex-1 flex-col items-center sm:flex-row">
                <div className="border-border/70 bg-card flex min-h-24 w-full max-w-40 flex-col items-center justify-center rounded-xl border px-4 text-center shadow-sm">
                  <span className="text-emerald-300 font-mono text-[10px] tracking-widest">0{index + 1}</span>
                  <span className="mt-1 text-sm font-medium">{step}</span>
                </div>
                {index < workflow.length - 1 ? <span className="bg-border/70 h-8 w-px sm:h-px sm:flex-1" aria-hidden /> : null}
              </li>
            ))}
          </ol>
        </PageContainer>
      </section>

      <section id="product" className="scroll-mt-20 border-b border-border/60 bg-card/[0.16]">
        <PageContainer className="py-20 sm:py-28">
          <div className="mx-auto max-w-xl text-center">
            <SectionEyebrow>The Truth Feed</SectionEyebrow>
            <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">Evidence first. Action second.</h2>
            <p className="text-muted-foreground mt-4 text-sm leading-6">The live feed is built from the same components you see after connecting a repository. No fabricated findings.</p>
          </div>
          <div className="border-border/70 bg-background mx-auto mt-12 max-w-4xl overflow-hidden rounded-2xl border shadow-2xl shadow-black/20">
            <div className="border-border/60 bg-card/70 flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2"><span className="bg-emerald-400 size-2 rounded-full" /><span className="font-mono text-xs">Truth Feed</span></div>
              <span className="text-muted-foreground text-[11px]">Live after connection</span>
            </div>
            <div className="p-4 sm:p-6"><FeedSkeleton /></div>
          </div>
        </PageContainer>
      </section>

      <footer className="border-b border-border/60">
        <PageContainer className="flex flex-col items-start justify-between gap-3 py-8 text-sm sm:flex-row sm:items-center">
          <p className="text-muted-foreground">Axon · Build Week · v0.1.0</p>
          <a className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/60 rounded focus-visible:ring-2 focus-visible:outline-none" href="https://github.com/CodeVishal-17/Axon" target="_blank" rel="noreferrer">GitHub</a>
        </PageContainer>
      </footer>
    </div>
  );
}

function JourneyCard({ step, title, body, icon }: { step: string; title: string; body: string; icon: React.ReactNode }) {
  return (
    <article className="border-border/70 bg-card/70 flex min-h-56 flex-1 flex-col rounded-xl border p-6 shadow-sm">
      <div className="flex items-center justify-between"><span className="text-muted-foreground font-mono text-[11px]">{step}</span><span className="text-emerald-300">{icon}</span></div>
      <h3 className="mt-auto pt-10 text-xl font-semibold tracking-tight">{title}</h3>
      <p className="text-muted-foreground mt-2 text-sm leading-6">{body}</p>
    </article>
  );
}
