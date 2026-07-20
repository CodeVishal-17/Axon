import { LandingHero } from "@/components/landing/landing-hero";
import { LandingSections } from "@/components/landing/landing-sections";

/** Product entry point: explanation first, connection second. */
export default function HomePage() {
  return (
    <>
      <LandingHero />
      <LandingSections />
    </>
  );
}
