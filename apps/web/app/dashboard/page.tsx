import type { Metadata } from "next";
import { Dashboard } from "@/components/dashboard/dashboard";

export const metadata: Metadata = {
  title: "Dashboard",
  description: "Everything Axon has verified and fixed across your repositories.",
};

export default function DashboardPage() {
  return <Dashboard />;
}
