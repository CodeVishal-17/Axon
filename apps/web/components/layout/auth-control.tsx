"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { LayoutDashboard, LogIn, LogOut } from "lucide-react";
import { githubLoginUrl, logout } from "@/lib/api";
import { useMe } from "@/lib/queries";
import { Button } from "@/components/ui/button";

/**
 * Header auth control: a "Sign in with GitHub" button when signed out; the
 * user's avatar + a dashboard link + logout when signed in. Client island so
 * the surrounding header can stay a server component.
 */
export function AuthControl() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { data: user, isPending } = useMe();

  async function handleLogout() {
    await logout();
    await queryClient.invalidateQueries();
    router.push("/");
  }

  if (isPending) {
    // Reserve space to avoid a layout jump when auth state resolves.
    return <div className="h-7 w-24" aria-hidden />;
  }

  if (!user) {
    return (
      <Button size="sm" render={<a href={githubLoginUrl()} />}>
        <LogIn className="size-3.5" aria-hidden />
        Sign in
      </Button>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <Button size="sm" variant="ghost" render={<Link href="/dashboard" />}>
        <LayoutDashboard className="size-3.5" aria-hidden />
        Dashboard
      </Button>
      {user.avatar_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={user.avatar_url}
          alt={user.login}
          className="border-border/60 size-6 rounded-full border"
        />
      ) : null}
      <span className="text-foreground hidden text-sm sm:inline">{user.login}</span>
      <Button
        size="icon-sm"
        variant="ghost"
        onClick={handleLogout}
        aria-label="Sign out"
      >
        <LogOut className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}
