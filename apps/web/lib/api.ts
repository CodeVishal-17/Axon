/**
 * API client layer — the single module through which the frontend talks to
 * the Axon backend. Components never call fetch() directly.
 *
 * All API payload types come from `lib/api/types.generated.ts`, generated
 * from the backend's OpenAPI schema via `make types`. Hand-written API
 * interfaces are forbidden in this codebase: if the backend changes a
 * response model, the frontend build must fail until types are regenerated.
 */

import type { components } from "@/lib/api/types.generated";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// --- Generated schema aliases ------------------------------------------
// Friendly names for the generated component schemas. Add one line per new
// backend model as endpoints land; components["schemas"][...] lookups fail
// the build if the backend renames or removes a model.

export type HealthResponse = components["schemas"]["HealthResponse"];

// --- Client ------------------------------------------------------------

/** Error carrying HTTP context so callers can branch on status. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly url: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Thin typed wrapper over fetch. JSON in/out, throws ApiError on non-2xx.
 * Kept deliberately minimal — caching, retries, and invalidation belong to
 * TanStack Query (introduced with the first real data screens), not here.
 */
export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      url,
      `API ${response.status} on ${path}${body ? `: ${body.slice(0, 200)}` : ""}`,
    );
  }

  return (await response.json()) as T;
}

// --- Endpoints ---------------------------------------------------------

/** GET /healthz — used by the header status dot and integration checks. */
export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/healthz");
}
