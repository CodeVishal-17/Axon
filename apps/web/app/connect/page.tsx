import type { Metadata } from "next";
import { ConnectView } from "@/components/repos/connect-view";

export const metadata: Metadata = {
  title: "Connect repositories",
  description: "Select the repositories Axon should verify.",
};

export default function ConnectPage() {
  return <ConnectView />;
}
