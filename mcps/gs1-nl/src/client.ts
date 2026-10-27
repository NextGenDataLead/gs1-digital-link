/**
 * TypeScript client for the GS1 NL Digital Link API v2.
 *
 * Mirrors `lib/gs1_dl_client.py` (IMPLEMENTATION_SPEC §4.3 / §5.1): identical
 * hosts, path prefix, path-case anomalies, auth-scheme switch, and retry policy.
 * Kept dependency-free (uses global `fetch`) so it is unit-testable without the
 * MCP SDK.
 */

/** Path prefix shared by every endpoint except ValidateDraft (§4.2). */
export const PATH_PREFIX = "/digitallinkv2/v2/";

/** GS1 Application Identifier for GTIN — GET path keys on "01", not "Gtin". */
const GTIN_AI = "01";

/** Environment-to-host mapping (§4.3). */
export const HOSTS = {
  test: "gs1nl-api-acc.gs1.nl",
  production: "gs1nl-api.gs1.nl",
} as const;

const RETRY_429_MAX_ATTEMPTS = 5;
const RETRY_429_BASE_MS = 1000;
const RETRY_429_MAX_MS = 60000;
const RETRY_5XX_MAX_ATTEMPTS = 3;
const RETRY_5XX_BASE_MS = 500;
const RETRY_5XX_MAX_MS = 30000;

const HTTP_SUCCESS_MIN = 200;
const HTTP_SUCCESS_MAX = 300;
const HTTP_BAD_REQUEST = 400;
const HTTP_UNAUTHORIZED = 401;
const HTTP_NOT_FOUND = 404;
const HTTP_TOO_MANY_REQUESTS = 429;
const HTTP_SERVER_ERROR_MIN = 500;
const HTTP_SERVER_ERROR_MAX = 600;
const NETWORK_ERROR_STATUS = 0;

/** OAuth2 client-credentials token endpoint (same host as the API). */
const TOKEN_PATH = "/authorization/token";
/** Re-mint the token this many ms before it expires. */
const TOKEN_REFRESH_SKEW_MS = 60_000;
/** Fallback token lifetime (seconds) if the response omits expires_in. */
const DEFAULT_TOKEN_TTL_S = 3600;

export type Environment = keyof typeof HOSTS;

export interface ResolverSettings {
  useGS1Resolver: boolean;
  resolverDomainName: string | null;
}

/** Fully-resolved config for one client (client credentials already resolved). */
export interface GS1ClientConfig {
  host: string;
  /**
   * Which GS1 environment this config resolved to.
   *
   * Carried on the config, not just used to pick the host, because the tools gate on it: the
   * production gate (`lib/gates.py` step 8) is mandatory and non-overridable, and a handler cannot
   * apply it without knowing the answer.
   */
  environment: "test" | "production";
  accountNumber: string;
  clientId: string;
  clientSecret: string;
  resolverSettings: ResolverSettings;
  batchSize: number;
}

interface TokenResponse {
  access_token?: string;
  expires_in?: number;
}

export interface LinkInput {
  link_type: string;
  language: string;
  link_title: string;
  target_url: string;
  default_link_type: boolean;
  public: boolean;
  media_type: string;
}

export interface AppIdentifier {
  identifier: string;
  template_variable: string;
}

export interface UpsertEntry {
  gtin: string;
  item_description: string;
  is_enabled?: boolean;
  links: LinkInput[];
  application_identifiers?: AppIdentifier[];
}

export interface BulkResult {
  total: number;
  batches: number;
  status_codes: number[];
}

/** Error raised for a non-success GS1 API response. Never carries the token. */
export class GS1ApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly responseBody: string,
    readonly errorResults: unknown[] | null = null,
  ) {
    super(`GS1 API error ${statusCode}`);
    this.name = "GS1ApiError";
  }
}

export interface GS1ClientOptions {
  /** Injectable fetch (defaults to global fetch) — used by tests. */
  fetchImpl?: typeof fetch;
  /** Injectable sleep (defaults to setTimeout) — makes retry backoff instant in tests. */
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

function zfill14(gtin: string): string {
  return gtin.padStart(14, "0");
}

function linkToWire(link: LinkInput): Record<string, unknown> {
  return {
    linkType: link.link_type,
    language: link.language,
    linkTitle: link.link_title,
    targetUrl: link.target_url,
    defaultLinkType: link.default_link_type,
    public: link.public,
    mediaType: link.media_type,
  };
}

function aiToWire(ai: AppIdentifier): Record<string, unknown> {
  return { identifier: ai.identifier, templateVariable: ai.template_variable };
}

function backoff429(attempt: number, retryAfterMs: number | null): number {
  if (retryAfterMs !== null) {
    return Math.min(retryAfterMs, RETRY_429_MAX_MS);
  }
  return Math.min(RETRY_429_BASE_MS * 2 ** (attempt - 1), RETRY_429_MAX_MS);
}

function backoff5xx(attempt: number): number {
  return Math.min(RETRY_5XX_BASE_MS * 2 ** (attempt - 1), RETRY_5XX_MAX_MS);
}

function retryAfterMs(response: Response): number | null {
  const value = response.headers.get("Retry-After");
  if (value === null) {
    return null;
  }
  const seconds = Number(value);
  return Number.isFinite(seconds) ? seconds * 1000 : null;
}

/** Parse a standard v2 `ErrorResult[]` body, else null (§5.1). */
export function parseErrorResults(body: string): unknown[] | null {
  let data: unknown;
  try {
    data = JSON.parse(body);
  } catch {
    return null;
  }
  if (
    Array.isArray(data) &&
    data.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        "identifier" in item &&
        "errors" in item,
    )
  ) {
    return data;
  }
  return null;
}

export class GS1Client {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly sleep: (ms: number) => Promise<void>;
  private token: string | null = null;
  private tokenExpiry = 0;

  constructor(
    private readonly config: GS1ClientConfig,
    options: GS1ClientOptions = {},
  ) {
    this.baseUrl = `https://${config.host}`;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.sleep = options.sleep ?? defaultSleep;
  }

  private async authHeader(): Promise<Record<string, string>> {
    return { Authorization: `Bearer ${await this.getToken()}` };
  }

  /** Return a cached OAuth2 token, minting or refreshing it as needed. */
  private async getToken(): Promise<string> {
    if (this.token !== null && Date.now() < this.tokenExpiry - TOKEN_REFRESH_SKEW_MS) {
      return this.token;
    }
    return this.mintToken();
  }

  /** Mint a JWT from the GS1 Authorization API (client_id/client_secret headers). */
  private async mintToken(): Promise<string> {
    let response: Response;
    try {
      response = await this.fetchImpl(this.baseUrl + TOKEN_PATH, {
        method: "POST",
        headers: {
          client_id: this.config.clientId,
          client_secret: this.config.clientSecret,
        },
      });
    } catch (err) {
      throw new GS1ApiError(NETWORK_ERROR_STATUS, `token network error: ${String(err)}`);
    }
    if (!response.ok) {
      throw new GS1ApiError(response.status, await response.text());
    }
    const data = (await response.json()) as TokenResponse;
    if (!data.access_token) {
      throw new GS1ApiError(response.status, "token response missing access_token");
    }
    this.token = data.access_token;
    this.tokenExpiry = Date.now() + (data.expires_in ?? DEFAULT_TOKEN_TTL_S) * 1000;
    return this.token;
  }

  private buildRequestBody(entry: UpsertEntry): Record<string, unknown> {
    return {
      accountNumber: this.config.accountNumber,
      identificationKeyType: "Gtin",
      identificationKey: zfill14(entry.gtin),
      isEnabled: entry.is_enabled ?? true,
      itemDescription: entry.item_description,
      resolverSettings: {
        useGS1Resolver: this.config.resolverSettings.useGS1Resolver,
        resolverDomainName: this.config.resolverSettings.resolverDomainName,
      },
      links: entry.links.map(linkToWire),
      applicationIdentifiers: (entry.application_identifiers ?? []).map(aiToWire),
    };
  }

  /** Issue one HTTP call with the retry policy in §4.3 / §5.1. */
  private async request(method: string, path: string, body: unknown): Promise<Response> {
    const url = this.baseUrl + path;
    let tokenRefreshed = false;
    let attempts429 = 0;
    let attempts5xx = 0;

    for (;;) {
      const init: RequestInit = {
        method,
        headers: {
          "Content-Type": "application/json",
          ...(await this.authHeader()),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      };

      let response: Response;
      try {
        response = await this.fetchImpl(url, init);
      } catch (err) {
        attempts5xx += 1;
        if (attempts5xx >= RETRY_5XX_MAX_ATTEMPTS) {
          throw new GS1ApiError(NETWORK_ERROR_STATUS, `network error: ${String(err)}`);
        }
        await this.sleep(backoff5xx(attempts5xx));
        continue;
      }

      const status = response.status;

      if (status >= HTTP_SUCCESS_MIN && status < HTTP_SUCCESS_MAX) {
        return response;
      }
      if (status === HTTP_UNAUTHORIZED && !tokenRefreshed) {
        // Cached token likely expired: force a re-mint and retry once.
        tokenRefreshed = true;
        this.token = null;
        continue;
      }
      if (status === HTTP_TOO_MANY_REQUESTS) {
        attempts429 += 1;
        if (attempts429 >= RETRY_429_MAX_ATTEMPTS) {
          throw await this.toError(response);
        }
        await this.sleep(backoff429(attempts429, retryAfterMs(response)));
        continue;
      }
      if (status >= HTTP_SERVER_ERROR_MIN && status < HTTP_SERVER_ERROR_MAX) {
        attempts5xx += 1;
        if (attempts5xx >= RETRY_5XX_MAX_ATTEMPTS) {
          throw await this.toError(response);
        }
        await this.sleep(backoff5xx(attempts5xx));
        continue;
      }
      throw await this.toError(response);
    }
  }

  private async toError(response: Response): Promise<GS1ApiError> {
    const text = await response.text();
    return new GS1ApiError(response.status, text, parseErrorResults(text));
  }

  /** POST /digitallinkv2/v2/digitallink (lowercase). Idempotent (§6.3). */
  async upsert(entry: UpsertEntry): Promise<void> {
    await this.request("POST", `${PATH_PREFIX}digitallink`, this.buildRequestBody(entry));
  }

  /** POST /digitallinkv2/v2/digitallinks, batched into config.batchSize (§4.3). */
  async upsertBulk(entries: UpsertEntry[]): Promise<BulkResult> {
    const statusCodes: number[] = [];
    for (let start = 0; start < entries.length; start += this.config.batchSize) {
      const chunk = entries.slice(start, start + this.config.batchSize);
      const response = await this.request(
        "POST",
        `${PATH_PREFIX}digitallinks`,
        chunk.map((entry) => this.buildRequestBody(entry)),
      );
      statusCodes.push(response.status);
    }
    return { total: entries.length, batches: statusCodes.length, status_codes: statusCodes };
  }

  /**
   * GET /digitallinkv2/v2/digitalLink/01/{gtin14} — the path segment is the GTIN
   * application identifier `01` (not "Gtin"), capital-L `digitalLink`. A missing GTIN
   * returns 400 "No valid contract found …" (mapped to null), not 404.
   */
  async get(gtin: string): Promise<Record<string, unknown> | null> {
    const path = `${PATH_PREFIX}digitalLink/${GTIN_AI}/${zfill14(gtin)}`;
    try {
      const response = await this.request("GET", path, undefined);
      return (await response.json()) as Record<string, unknown>;
    } catch (err) {
      if (isNotFound(err)) {
        return null;
      }
      throw err;
    }
  }
}

/** Whether an error represents a missing GTIN: 400 "No valid contract found" (or 404). */
function isNotFound(err: unknown): boolean {
  if (!(err instanceof GS1ApiError)) {
    return false;
  }
  if (err.statusCode === HTTP_NOT_FOUND) {
    return true;
  }
  return err.statusCode === HTTP_BAD_REQUEST && /no valid contract found/i.test(err.responseBody);
}
