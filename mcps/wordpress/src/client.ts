/**
 * TypeScript client for the WordPress REST API v2.
 *
 * Mirrors `lib/wp_client.py` (IMPLEMENTATION_SPEC §4.4 / §5.1 / §6.1-6.2 / §7):
 * HTTP Basic auth with an application password, the same retry policy (429/5xx with
 * independent budgets; a 401 is terminal — no token dance), the 3-step upsert lookup
 * (existing_id → slug → meta.gtin) with E8/E11 guards, and the media contract: multipart
 * uploads under a content-addressed slug, with a stored-size check that refuses a truncated
 * one. Kept dependency-free (global `fetch`/`FormData`, `node:crypto`, `node:fs`) so it is
 * unit-testable without the MCP SDK.
 *
 * Deliberate deviations from the Python client are listed in `README.md` under
 * "Parity with lib/wp_client.py" — keep that section true.
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { basename, extname } from "node:path";

/** REST base for posts of any (custom) post type (§4.4). */
export const WP_API_PREFIX = "/wp-json/wp/v2";
const MEDIA_PATH = `${WP_API_PREFIX}/media`;
/** Polylang detection route — a 200 means the plugin is active (§4.4). */
export const PLL_LANGUAGES_PATH = "/wp-json/pll/v1/languages";
/**
 * WPML detection route — its presence means WPML is active (§4.4).
 *
 * WPML's REST **namespace root**, probed rather than a concrete endpoint because WPML's routes
 * are admin-gated and version-dependent; the namespace index is the one thing that answers 200
 * unauthenticated on any WPML site. (The former probe,
 * `/wp-json/sitepress-multilingual-cms/v1/languages`, is not a namespace WPML registers — it 404s
 * even on a WPML site, so detection always fell through to "none".)
 */
export const WPML_PROBE_PATH = "/wp-json/wpml/v1";

const GTIN_META_KEY = "gtin";
const CONTENT_HASH_META_KEY = "content_sha256";
/** Hex chars of the content SHA-256 folded into a media slug (§6.2 idempotency). */
const SLUG_HASH_LEN = 12;

const RETRY_429_MAX_ATTEMPTS = 5;
const RETRY_429_BASE_MS = 1000;
const RETRY_429_MAX_MS = 60000;
const RETRY_5XX_MAX_ATTEMPTS = 3;
const RETRY_5XX_BASE_MS = 500;
const RETRY_5XX_MAX_MS = 30000;

const HTTP_SUCCESS_MIN = 200;
const HTTP_SUCCESS_MAX = 300;
const HTTP_REDIRECT_MAX = 400;
const HTTP_NOT_FOUND = 404;
const HTTP_CONFLICT = 409;
const HTTP_GONE = 410;
const HTTP_TOO_MANY_REQUESTS = 429;
const HTTP_SERVER_ERROR_MIN = 500;
const HTTP_SERVER_ERROR_MAX = 600;
const NETWORK_ERROR_STATUS = 0;

export type MultilingualPlugin = "polylang" | "wpml" | "none";

/** Fully-resolved config for one client (application password already resolved). */
export interface WordPressClientConfig {
  siteUrl: string;
  username: string;
  appPassword: string;
  postType: string;
  postStatus: string;
  multilingualPlugin: MultilingualPlugin;
  defaultLanguage: string;
  languages: string[];
}

/** A WordPress post/page as returned by the REST API (`context=edit`). */
export interface WordPressPage {
  id: number;
  slug?: string;
  status?: string;
  type?: string;
  link?: string;
  title?: { rendered?: string; raw?: string };
  content?: { rendered?: string; raw?: string };
  parent?: number;
  featured_media?: number;
  meta?: Record<string, unknown>;
}

/** A WordPress media attachment as returned by the REST API. */
export interface WordPressMedia {
  id: number;
  slug?: string;
  source_url?: string;
  /** Untrusted REST JSON — narrowed in `storedBytes`, never typed optimistically. */
  media_details?: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

/** Input to `upsertPage`, mirroring the §4.4 Python signature. */
export interface UpsertPageInput {
  post_type: string;
  slug: string;
  title: string;
  content: string;
  language: string;
  featured_media?: number;
  parent?: number;
  meta?: Record<string, unknown>;
  existing_id?: number;
  /** ACF field values. Written in a second call, never on the create — see `writeAcf`. */
  acf?: Record<string, unknown>;
}

/** Error raised for a non-success WordPress API response. Never carries the password. */
export class WordPressApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly responseBody: string,
  ) {
    super(`WordPress API error ${statusCode}`);
    this.name = "WordPressApiError";
  }
}

/** Raised when a matched page's meta.gtin differs from the row's GTIN (edge E8). */
export class WordPressGtinMismatchError extends Error {
  constructor(
    readonly gtin: string,
    readonly existingGtin: string,
    readonly wpPageId: number,
  ) {
    super(
      `WordPress page ${wpPageId} has meta.gtin ${existingGtin}, which does not match ` +
        `row GTIN ${gtin}; skipping to avoid overwriting`,
    );
    this.name = "WordPressGtinMismatchError";
  }
}

/**
 * Raised when WordPress stored a different number of bytes than were uploaded (#60, #72).
 *
 * A live upload was cut off mid-transfer and answered `201` anyway, leaving a 1.5 MB fragment of
 * an 8 MB video that WordPress then served happily. A half-uploaded video is worse than a failed
 * one: the page publishes, the QR resolves, and the product looks fine until someone presses play.
 */
export class MediaIntegrityError extends Error {
  constructor(
    readonly filePath: string,
    readonly sentBytes: number,
    readonly storedBytes: number,
    readonly mediaId: number,
    readonly deleted: boolean,
  ) {
    super(
      `WordPress stored ${storedBytes} bytes of ${basename(filePath)} but ${sentBytes} were sent ` +
        `(attachment ${mediaId}); ` +
        (deleted
          ? "the attachment was deleted"
          : `remove media ${mediaId} by hand — the fragment is still on the site`),
    );
    this.name = "MediaIntegrityError";
  }
}

/** Raised rather than deleting an attachment this tool did not upload (#73). */
export class MediaOwnershipError extends Error {
  constructor(
    readonly mediaId: number,
    readonly reason: string,
  ) {
    super(
      `refusing to delete media ${mediaId}: ${reason}. Only attachments this tool uploaded ` +
        `(carrying a non-empty meta.${CONTENT_HASH_META_KEY}) may be deleted`,
    );
    this.name = "MediaOwnershipError";
  }
}

export type LogLevel = "warn" | "error";
/** Diagnostics sink. Never given the application password. */
export type WordPressLogger = (level: LogLevel, message: string) => void;

/**
 * Default sink: stderr.
 *
 * This client is served over stdio by `index.ts`, where **stdout carries the MCP protocol
 * frames** — a diagnostic written there corrupts the session. `process.stderr.write` rather than
 * `console.error` keeps the channel explicit and the repo's console ban unambiguous.
 */
const defaultLogger: WordPressLogger = (level, message) => {
  process.stderr.write(`[wordpress-mcp] ${level}: WP ${message}\n`);
};

export interface WordPressClientOptions {
  /** Injectable fetch (defaults to global fetch) — used by tests. */
  fetchImpl?: typeof fetch;
  /** Injectable sleep (defaults to setTimeout) — makes retry backoff instant in tests. */
  sleep?: (ms: number) => Promise<void>;
  /** Injectable diagnostics sink (defaults to stderr) — lets tests assert on warnings. */
  logger?: WordPressLogger;
}

/** One multipart file part, mirroring Python's `files={"file": (filename, data, mime)}`. */
interface FormFile {
  fileName: string;
  data: Uint8Array;
  mime: string;
}

interface RequestOptions {
  params?: Record<string, string>;
  jsonBody?: unknown;
  /**
   * Sent as `multipart/form-data` under field `file`.
   *
   * There is deliberately no raw-body mode. That is the form #62 removed after a security plugin
   * on the live site refused an ordinary H.264 video with a bare HTML `403`, while accepting the
   * identical bytes as multipart — so its absence here is a guarantee, not an oversight.
   */
  formFile?: FormFile;
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

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

function metaGtin(meta: Record<string, unknown> | undefined): string | null {
  const value = meta?.[GTIN_META_KEY];
  return value === undefined || value === null || value === "" ? null : String(value);
}

/** Derive a deterministic media slug from the title (or filename) (§6.2). */
export function mediaSlug(title: string | undefined, fileName: string): string {
  const source = title && title.length > 0 ? title : fileName.replace(/\.[^.]+$/, "");
  const slug = source
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug.length > 0 ? slug : "media";
}

/**
 * A content-addressed media slug: the base slug plus a prefix of the content hash (§6.2).
 *
 * Folding the hash into the slug makes dedup a pure slug lookup — identical bytes always map to
 * the same slug and different bytes to a different one. So `uploadMedia` needs no read-back of the
 * stored `content_sha256` meta (it works whether or not that meta is exposed in REST, and on the
 * live site it was not), and a stale attachment sharing only the *base* slug cannot shadow the
 * match. Both were seen live.
 */
export function contentSlug(base: string, digest: string): string {
  return `${base}-${digest.slice(0, SLUG_HASH_LEN)}`;
}

/**
 * Node ships no `mimetypes` equivalent and this module is deliberately dependency-free, so a small
 * map is the alternative to a package. It covers all six of `lib/media_video.py`'s `_VIDEO_EXTS`
 * plus the web image set, and falls back to what Python's `guess_type` returns for anything
 * unknown. WordPress re-sniffs the upload itself, so the part MIME is a hint, not the decider.
 */
const MIME_BY_EXTENSION: Readonly<Record<string, string>> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".svg": "image/svg+xml",
  ".mp4": "video/mp4",
  ".m4v": "video/x-m4v",
  ".mov": "video/quicktime",
  ".webm": "video/webm",
  ".mpg": "video/mpeg",
  ".mpeg": "video/mpeg",
  ".pdf": "application/pdf",
};
const DEFAULT_MIME = "application/octet-stream";

function guessMime(fileName: string): string {
  return MIME_BY_EXTENSION[extname(fileName).toLowerCase()] ?? DEFAULT_MIME;
}

function multipartBody(file: FormFile): FormData {
  const form = new FormData();
  form.append("file", new File([file.data], file.fileName, { type: file.mime }));
  return form;
}

export class WordPressClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly logger: WordPressLogger;
  multilingualPlugin: MultilingualPlugin;

  constructor(
    private readonly config: WordPressClientConfig,
    options: WordPressClientOptions = {},
  ) {
    this.baseUrl = config.siteUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.sleep = options.sleep ?? defaultSleep;
    this.logger = options.logger ?? defaultLogger;
    this.multilingualPlugin = config.multilingualPlugin;
  }

  private authHeader(): Record<string, string> {
    const token = Buffer.from(`${this.config.username}:${this.config.appPassword}`).toString(
      "base64",
    );
    return { Authorization: `Basic ${token}` };
  }

  /**
   * Detect which multilingual plugin the site runs, by probing (§4.4).
   *
   * Probes the Polylang route first, then WPML's namespace root; a 200 wins.
   *
   * **Reports; does not adopt.** The configured value stays in force for writes, mirroring
   * Python's `_resolve_plugin`: a probe can be wrong for reasons that have nothing to do with the
   * site's real setup — a renamed route, a version change, an admin-gated endpoint — and letting
   * one override a configured plugin fails silently, publishing every page unlinked.
   */
  async detectMultilingualPlugin(): Promise<MultilingualPlugin> {
    if (await this.probe(PLL_LANGUAGES_PATH)) {
      return "polylang";
    }
    if (await this.probe(WPML_PROBE_PATH)) {
      return "wpml";
    }
    return "none";
  }

  /**
   * Return the page with `slug` under `postType`, or null (§4.4).
   *
   * `language` scopes the lookup and is **required on a multilingual site** — see
   * {@link langParams}.
   */
  async findBySlug(
    postType: string,
    slug: string,
    language?: string,
  ): Promise<WordPressPage | null> {
    const pages = await this.getList(`${WP_API_PREFIX}/${postType}`, {
      slug,
      context: "edit",
      ...this.langParams(language),
    });
    return pages.length > 0 ? (pages[0] as WordPressPage) : null;
  }

  /**
   * Create or update one product page, idempotently (§6.1).
   *
   * Lookups are scoped to `input.language`. On a multilingual site a create is issued with
   * `?lang=`, and `acf` is written in a **second call** — both are load-bearing and both have a
   * silent failure mode; see {@link writePage} and {@link writeAcf}.
   */
  async upsertPage(input: UpsertPageInput): Promise<WordPressPage> {
    const gtin = metaGtin(input.meta);
    const found = await this.lookupExisting(
      input.post_type,
      input.slug,
      gtin,
      input.existing_id,
      input.language,
    );
    let page: WordPressPage;
    if (found !== null) {
      this.guardGtinMatch(found, gtin);
      page = await this.writePage(input, found.id);
    } else {
      page = await this.writePage(input, null);
    }
    if (input.acf !== undefined && Object.keys(input.acf).length > 0) {
      page = await this.writeAcf(input.post_type, page.id, input.acf);
    }
    return page;
  }

  /**
   * Upload a media file, idempotently by a content-addressed slug (§6.2).
   *
   * The slug folds the SHA-256 of the bytes into a base derived from `title` (or the filename) —
   * see {@link contentSlug}. Dedup is then a pure slug lookup: a hit means the bytes are
   * identical, so the existing id is returned without re-uploading.
   *
   * **A content match is checked for length before it is trusted.** The slug proves what the bytes
   * were meant to be, not what arrived; a hit whose stored size disagrees is deleted and
   * re-uploaded rather than reused, or re-running could never repair a truncated upload.
   */
  async uploadMedia(filePath: string, title?: string): Promise<number> {
    const data = readFileSync(filePath);
    const digest = createHash("sha256").update(data).digest("hex");
    const slug = contentSlug(mediaSlug(title, basename(filePath)), digest);

    const existing = await this.findMediaBySlug(slug);
    if (existing !== null && (await this.isWhole(existing, filePath, data.length, slug))) {
      return existing.id;
    }
    return this.createMedia(filePath, data, title, slug, digest);
  }

  /** Return whether `url` resolves to a 2xx/3xx via HEAD (§4.4, §5.1). */
  async verifyUrl(url: string): Promise<boolean> {
    let response: Response;
    try {
      response = await this.fetchImpl(url, { method: "HEAD" });
    } catch (err) {
      throw new WordPressApiError(NETWORK_ERROR_STATUS, `verify_url network error: ${String(err)}`);
    }
    if (response.status >= HTTP_SUCCESS_MIN && response.status < HTTP_REDIRECT_MAX) {
      return true;
    }
    throw new WordPressApiError(response.status, await response.text());
  }

  // -- Lookup / write internals -------------------------------------------

  /**
   * The `lang` query param scoping a lookup to one language (§4.4).
   *
   * On a multilingual site the same slug exists once per language — `p-{gtin}` is GTIN-derived
   * and carries no language component — and an unscoped collection query answers with the
   * **default language only**. Verified against a live WPML site: `?slug=p-X` returned the nl
   * page while the fr page was invisible.
   *
   * Omitting this is not a near miss, it is data loss. Both pages share a `meta.gtin` (same
   * product), so the E8 guard cannot catch it: upserting the fr row finds the nl page by slug,
   * the GTIN matches, and **the nl page is overwritten with French** — no fr page is created and
   * the row reports ok.
   *
   * Sent for Polylang as well as WPML. Only WPML is verified; for Polylang this is at worst
   * inert, since WordPress silently ignores query params it does not know.
   */
  private langParams(language: string | undefined): Record<string, string> {
    if (
      language !== undefined &&
      language.length > 0 &&
      (this.multilingualPlugin === "wpml" || this.multilingualPlugin === "polylang")
    ) {
      return { lang: language };
    }
    return {};
  }

  /**
   * Resolve the existing page by id, then slug, then `meta.gtin` (§6.1).
   *
   * Both collection lookups are scoped to `language`: on a multilingual site they would otherwise
   * answer for the default language only, and adopt the wrong page (see {@link langParams}).
   * `existingId` needs no scope — an id is unambiguous.
   */
  private async lookupExisting(
    postType: string,
    slug: string,
    gtin: string | null,
    existingId: number | undefined,
    language: string | undefined,
  ): Promise<WordPressPage | null> {
    if (existingId !== undefined) {
      const page = await this.getPage(postType, existingId);
      if (page !== null) {
        return page;
      }
    }
    const bySlug = await this.findBySlug(postType, slug, language);
    if (bySlug !== null) {
      return bySlug;
    }
    if (gtin !== null) {
      return this.findByMetaGtin(postType, gtin, language);
    }
    return null;
  }

  private guardGtinMatch(found: WordPressPage, gtin: string | null): void {
    if (gtin === null) {
      return;
    }
    const existingGtin = metaGtin(found.meta);
    if (existingGtin === null) {
      // E11: slug collision with a non-GTIN page — needs human intervention.
      throw new WordPressApiError(
        HTTP_CONFLICT,
        `slug collision with non-GTIN WordPress page ${found.id}`,
      );
    }
    if (existingGtin !== gtin) {
      throw new WordPressGtinMismatchError(gtin, existingGtin, found.id); // E8
    }
  }

  private async getPage(postType: string, pageId: number): Promise<WordPressPage | null> {
    try {
      const response = await this.request("GET", `${WP_API_PREFIX}/${postType}/${pageId}`, {
        params: { context: "edit" },
      });
      return (await response.json()) as WordPressPage;
    } catch (err) {
      if (err instanceof WordPressApiError && err.statusCode === HTTP_NOT_FOUND) {
        return null;
      }
      throw err;
    }
  }

  /**
   * GET the page whose `meta.gtin` equals `gtin`, or null (§6.1).
   *
   * The `meta_key`/`meta_value` params are **not** core WordPress REST features: core silently
   * drops unknown query params rather than erroring, so a site without a `rest_{post_type}_query`
   * enabler answers this request with an unfiltered page of *every* post. Never trust the server
   * to have filtered — verify `meta.gtin` on the way out. Taking `pages[0]` on an unfiltered list
   * returns an arbitrary unrelated page, which the E8/E11 guards then reject, turning every
   * would-be create into a bogus "slug collision" error.
   *
   * Returning null when nothing matches is also the right fallback on a site with no enabler: the
   * caller creates the page instead of adopting the wrong one.
   */
  private async findByMetaGtin(
    postType: string,
    gtin: string,
    language: string | undefined,
  ): Promise<WordPressPage | null> {
    const pages = await this.getList(`${WP_API_PREFIX}/${postType}`, {
      meta_key: GTIN_META_KEY,
      meta_value: gtin,
      context: "edit",
      ...this.langParams(language),
    });
    for (const page of pages) {
      const candidate = page as WordPressPage;
      if (metaGtin(candidate.meta) === gtin) {
        return candidate;
      }
    }
    return null;
  }

  private async findMediaBySlug(slug: string): Promise<WordPressMedia | null> {
    const items = await this.getList(MEDIA_PATH, { slug, context: "edit" });
    return items.length > 0 ? (items[0] as WordPressMedia) : null;
  }

  private async getList(path: string, params: Record<string, string>): Promise<unknown[]> {
    try {
      const response = await this.request("GET", path, { params });
      const data = await response.json();
      return Array.isArray(data) ? data : [];
    } catch (err) {
      if (err instanceof WordPressApiError && err.statusCode === HTTP_NOT_FOUND) {
        return [];
      }
      throw err;
    }
  }

  /**
   * POST a create (`pageId === null`) or update to the post-type endpoint.
   *
   * A create on a multilingual site carries `?lang=` so the page is created **as** that language.
   * This is what keeps the slug intact: the slug is GTIN-derived and identical across languages,
   * so a page created without a language collides with its sibling and WordPress silently appends
   * `-2`. The French page would then live at `/fr/…/p-{gtin}-2/` while the resolver target is
   * built as `/fr/…/p-{gtin}/` — and every French QR would point at a 404. Verified live: with
   * `?lang=` both pages keep `p-{gtin}`.
   *
   * Deliberately no `acf` here — see {@link writeAcf}.
   */
  private async writePage(input: UpsertPageInput, pageId: number | null): Promise<WordPressPage> {
    const body: Record<string, unknown> = {
      title: input.title,
      content: input.content,
      status: this.config.postStatus,
      slug: input.slug,
    };
    if (input.meta !== undefined) {
      body.meta = input.meta;
    }
    if (input.featured_media !== undefined) {
      body.featured_media = input.featured_media;
    }
    if (input.parent !== undefined) {
      body.parent = input.parent;
    }
    if (this.multilingualPlugin === "polylang") {
      body.lang = input.language;
    }
    // Only on create: an existing page already has its language, and re-asserting it here is not
    // this method's job (translation linking owns it).
    const path =
      pageId === null
        ? `${WP_API_PREFIX}/${input.post_type}`
        : `${WP_API_PREFIX}/${input.post_type}/${pageId}`;
    const params = pageId === null ? this.langParams(input.language) : {};
    const response = await this.request("POST", path, { jsonBody: body, params });
    return (await response.json()) as WordPressPage;
  }

  /**
   * Write ACF field values in their own call, after the page exists (§3.1).
   *
   * ACF cannot ride along on a create that carries `?lang=`: the values are **silently dropped** —
   * `201 Created`, fields empty, no error. Measured three ways against the live site: create
   * without `?lang` + acf persists; create with `?lang=fr` + acf is empty; create with `?lang=fr`
   * *then* acf in a second call persists. Unhandled, this yields pages with a correct URL and no
   * content, which `verifyUrl` passes as ok.
   *
   * Done for updates too, not just creates: one path is easier to keep correct than two, and the
   * extra call is cheap next to publishing a blank page.
   */
  private async writeAcf(
    postType: string,
    pageId: number,
    acf: Record<string, unknown>,
  ): Promise<WordPressPage> {
    const response = await this.request("POST", `${WP_API_PREFIX}/${postType}/${pageId}`, {
      jsonBody: { acf },
    });
    return (await response.json()) as WordPressPage;
  }

  /** Upload media bytes, then set its slug/title and content-hash meta (§6.2). */
  private async createMedia(
    filePath: string,
    data: Uint8Array,
    title: string | undefined,
    slug: string,
    digest: string,
  ): Promise<number> {
    // Name the stored file after the content-addressed slug so re-uploads of the same bytes reuse
    // (before reaching here) and the physical filename never churns -1/-2 suffixes.
    const response = await this.request("POST", MEDIA_PATH, {
      formFile: {
        fileName: `${slug}${extname(filePath)}`,
        data,
        mime: guessMime(filePath),
      },
    });
    const media = (await response.json()) as WordPressMedia;

    // Before the finalise call, not after: the finalise is what claims the content-addressed slug,
    // and a fragment that holds that slug is returned by every later run as a content match.
    // Checking here means a truncated upload never becomes the answer to its own hash.
    await this.assertWhole(media, filePath, data.length);

    const updateBody: Record<string, unknown> = {
      slug,
      meta: { [CONTENT_HASH_META_KEY]: digest },
    };
    if (title !== undefined) {
      updateBody.title = title;
    }
    await this.request("POST", `${MEDIA_PATH}/${media.id}`, { jsonBody: updateBody });
    return media.id;
  }

  // -- Media integrity (#60, #72, #73) -------------------------------------

  /** Delete and raise if WordPress stored fewer (or more) bytes than were sent (#72). */
  private async assertWhole(
    media: WordPressMedia,
    filePath: string,
    sent: number,
  ): Promise<void> {
    const stored = await this.storedBytes(media);
    if (stored === null) {
      this.logger(
        "warn",
        `stored size of media ${media.id} (${basename(filePath)}) could not be established; ` +
          `upload unverified`,
      );
      return;
    }
    if (stored === sent) {
      return;
    }
    this.logger(
      "error",
      `stored ${stored} bytes of ${basename(filePath)} but ${sent} were sent; ` +
        `deleting attachment ${media.id}`,
    );
    throw new MediaIntegrityError(
      filePath,
      sent,
      stored,
      media.id,
      await this.discardMedia(media.id),
    );
  }

  /**
   * Whether a deduped attachment may be reused, discarding it if it is a fragment.
   *
   * The counterpart to {@link assertWhole} on the path that does *no* upload. It returns rather
   * than raises, because the caller has the bytes in hand and can simply upload them again — a
   * stale fragment is repairable here, and only here.
   *
   * Unverifiable is reuse, not deletion: deleting on a number nobody supplied would remove a live
   * page's media on a guess. The bytes match by hash; absent evidence to the contrary, the
   * attachment is the file.
   *
   * **Ownership is checked before anything is deleted here**, unlike on the create path: this id
   * came from a slug *lookup*, so it is whatever the site had under that slug, not something this
   * call minted. The lookup already fetched `meta` (`context=edit`), so the check costs no request.
   */
  private async isWhole(
    existing: WordPressMedia,
    filePath: string,
    sent: number,
    slug: string,
  ): Promise<boolean> {
    const stored = await this.storedBytes(existing);
    if (stored === null || stored === sent) {
      return true;
    }
    if (!isOurs(existing)) {
      throw new MediaOwnershipError(
        existing.id,
        `it holds ${stored} bytes rather than ${sent}, but is not ours to replace`,
      );
    }
    this.logger(
      "warn",
      `media ${slug} matches by content hash but holds ${stored} bytes, not ${sent} — deleting ` +
        `the fragment (attachment ${existing.id}) and re-uploading ${basename(filePath)}`,
    );
    if (await this.discardMedia(existing.id)) {
      return false;
    }
    throw new MediaIntegrityError(filePath, sent, stored, existing.id, false);
  }

  /**
   * How many bytes WordPress holds for an attachment, or null if it will not say.
   *
   * `media_details.filesize` is the cheap answer — it rides along on the create response, and it
   * is the size of the *original* file rather than of any derivative. It is not guaranteed for
   * every attachment type on every WordPress version, so a HEAD on the public `source_url` is the
   * fallback: the webserver's `Content-Length` is an independent measurement.
   *
   * `null` is a real answer and callers must treat it as one — "unverified", never "fine".
   */
  private async storedBytes(media: WordPressMedia): Promise<number | null> {
    const filesize = media.media_details?.filesize;
    if (typeof filesize === "number" && Number.isInteger(filesize)) {
      return filesize;
    }
    if (typeof media.source_url === "string" && media.source_url.length > 0) {
      return this.contentLength(media.source_url);
    }
    return null;
  }

  /**
   * `Content-Length` of `url` via an unauthenticated HEAD, or null.
   *
   * Deliberately forgiving, and deliberately *not* through {@link request}: this is an absolute
   * URL with no base to prepend, and a second opinion rather than an API call. A proxy that
   * answers without the header, or does not answer at all, must not turn into an upload failure.
   * It only ever adds certainty.
   */
  private async contentLength(url: string): Promise<number | null> {
    let response: Response;
    try {
      response = await this.fetchImpl(url, { method: "HEAD" });
    } catch (err) {
      this.logger("warn", `HEAD ${url} failed (${String(err)}); upload size unverified`);
      return null;
    }
    if (response.status < HTTP_SUCCESS_MIN || response.status >= HTTP_SUCCESS_MAX) {
      this.logger("warn", `HEAD ${url} -> ${response.status}; upload size unverified`);
      return null;
    }
    const header = response.headers.get("Content-Length");
    // `Number(null)` and `Number("")` are both 0, and "0 bytes stored" reads as a mismatch — which
    // would delete a perfectly good upload. A missing header must reach the null branch instead.
    const value = header === null ? Number.NaN : Number(header);
    if (!Number.isInteger(value)) {
      this.logger("warn", `HEAD ${url} gave no usable Content-Length`);
      return null;
    }
    return value;
  }

  /**
   * Delete an attachment we know is corrupt, without masking why we are deleting it.
   *
   * A delete raises on any non-2xx, and that exception would replace the integrity failure it is
   * cleaning up after — leaving the operator with a delete error and no statement of what was
   * wrong with the file.
   */
  private async discardMedia(mediaId: number): Promise<boolean> {
    try {
      return await this.forceDeleteMedia(mediaId);
    } catch (err) {
      this.logger("error", `could not delete corrupt media ${mediaId}: ${String(err)}`);
      return false;
    }
  }

  /**
   * Delete one attachment with **no ownership check**; idempotent. Private, and reachable from no
   * MCP tool — `src/server.test.ts` pins the exposed tool list, which is the standing guard.
   *
   * Both callers already hold the proof an ownership guard would have to fetch. {@link assertWhole}
   * has an id from its own POST response moments earlier — provenance no lookup could improve on,
   * and which the guard would in fact *fail*, since the content hash is not written until the
   * finalise call a bad upload never reaches. {@link isWhole} has run `isOurs` itself.
   *
   * Always force-deletes: WordPress refuses to trash attachments (`rest_trash_not_supported`,
   * HTTP 501), so bypassing the trash is the only mode there is.
   */
  private async forceDeleteMedia(mediaId: number): Promise<boolean> {
    try {
      await this.request("DELETE", `${MEDIA_PATH}/${mediaId}`, { params: { force: "true" } });
    } catch (err) {
      if (
        err instanceof WordPressApiError &&
        (err.statusCode === HTTP_NOT_FOUND || err.statusCode === HTTP_GONE)
      ) {
        return false; // already gone; idempotent
      }
      throw err;
    }
    this.logger("warn", `deleted media ${mediaId}`);
    return true;
  }

  private async probe(path: string): Promise<boolean> {
    try {
      await this.request("GET", path, {});
      return true;
    } catch (err) {
      if (err instanceof WordPressApiError) {
        return false;
      }
      throw err;
    }
  }

  /** Issue one HTTP call with the retry policy in §5.1. */
  private async request(method: string, path: string, opts: RequestOptions): Promise<Response> {
    // An empty params object must produce no query string at all, not a bare "?" — callers pass
    // `{}` for "no language scope" and a trailing ? is noise in every log and test assertion.
    const search = opts.params === undefined ? "" : new URLSearchParams(opts.params).toString();
    const query = search === "" ? "" : `?${search}`;
    const url = this.baseUrl + path + query;
    let attempts429 = 0;
    let attempts5xx = 0;
    // Built once, above the retry loop. A File-backed FormData can be re-extracted per attempt
    // (each gets a fresh boundary), whereas rebuilding it would re-copy the bytes — an 8 MB video,
    // three times, on a flaky connection.
    const formBody = opts.formFile === undefined ? undefined : multipartBody(opts.formFile);

    for (;;) {
      const headers: Record<string, string> = { ...this.authHeader() };
      if (opts.jsonBody !== undefined) {
        headers["Content-Type"] = "application/json";
      }
      const init: RequestInit = { method, headers };
      if (formBody !== undefined) {
        // No Content-Type set here on purpose: fetch must write `multipart/form-data; boundary=…`
        // itself, and any value set here would clobber the boundary.
        init.body = formBody;
      } else if (opts.jsonBody !== undefined) {
        init.body = JSON.stringify(opts.jsonBody);
      }

      let response: Response;
      try {
        response = await this.fetchImpl(url, init);
      } catch (err) {
        attempts5xx += 1;
        if (attempts5xx >= RETRY_5XX_MAX_ATTEMPTS) {
          throw new WordPressApiError(NETWORK_ERROR_STATUS, `network error: ${String(err)}`);
        }
        await this.sleep(backoff5xx(attempts5xx));
        continue;
      }

      const status = response.status;
      if (status >= HTTP_SUCCESS_MIN && status < HTTP_SUCCESS_MAX) {
        return response;
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
      // Any other 4xx (400/401/403/404/409): terminal per §5.1.
      throw await this.toError(response);
    }
  }

  private async toError(response: Response): Promise<WordPressApiError> {
    return new WordPressApiError(response.status, await response.text());
  }
}

/**
 * Whether this tool uploaded an attachment, from the content hash it stamps on its own (#73).
 *
 * `uploadMedia` writes `meta.content_sha256` on everything it creates. On the pilot site the key
 * is registered site-wide, so it is *present* on all 406 attachments and non-empty on only the 40
 * that came from here — which makes "non-empty" the test, not "present".
 *
 * Anything unreadable or empty answers false. That is the safe direction: at worst this declines
 * to touch an orphan of our own, which is recoverable, rather than deleting a client's product
 * photo, which is not.
 */
function isOurs(media: WordPressMedia): boolean {
  const digest = media.meta?.[CONTENT_HASH_META_KEY];
  return typeof digest === "string" && digest.length > 0;
}
