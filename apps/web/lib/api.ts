/**
 * API client layer — the single module through which the frontend talks to
 * the Axon backend. Components never call fetch() directly.
 *
 * Response types: `HealthResponse` is hand-written for now; T0.5 replaces
 * hand-written API types with types generated from the backend's OpenAPI
 * schema (`make types`), and this module becomes their only consumer.
 */

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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

/** Mirror of the backend's HealthResponse model (axon/api/health.py). */
export interface HealthResponse {
  status: string;
  version: string;
  environment: string;
  database: "ok" | "unavailable";
}

/** GET /healthz — used to verify frontend↔backend wiring. */
export function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/healthz");
}
