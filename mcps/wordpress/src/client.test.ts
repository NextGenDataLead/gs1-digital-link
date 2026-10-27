/** Tests for the TS WordPress client: auth, detection, idempotency, E8/E11, retry, scrubbing. */

import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";

import {
  type WordPressClientConfig,
  type WordPressLogger,
  contentSlug,
  MediaIntegrityError,
  MediaOwnershipError,
  mediaSlug,
  WordPressApiError,
  WordPressClient,
  WordPressGtinMismatchError,
} from "./client.js";

const APP_PASS = "abcd EFGH ijkl MNOP";
const USERNAME = "gs1-publisher";
const SITE = "https://staging.example.com";
const POST_TYPE = "democlient";

const CONFIG: WordPressClientConfig = {
  siteUrl: SITE,
  username: USERNAME,
  appPassword: APP_PASS,
  postType: POST_TYPE,
  postStatus: "publish",
  multilingualPlugin: "none",
  defaultLanguage: "nl",
  languages: ["nl", "fr"],
};

/** A multilingual site — the only kind this tool targets, and where lang scoping is load-bearing. */
const WPML_CONFIG: WordPressClientConfig = { ...CONFIG, multilingualPlugin: "wpml" };

interface Call {
  url: string;
  init: RequestInit;
}

function stubFetch(queue: (Response | Error)[]): { fetchImpl: typeof fetch; calls: Call[] } {
  const calls: Call[] = [];
  const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const next = queue.shift();
    if (next === undefined) {
      throw new Error("no more queued responses");
    }
    if (next instanceof Error) {
      throw next;
    }
    return next;
  }) as unknown as typeof fetch;
  return { fetchImpl, calls };
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const noSleep = async (): Promise<void> => {};

/** Silences the default stderr sink; tests that care about diagnostics pass `collectLogs()`. */
const quiet: WordPressLogger = () => {};

function makeClient(
  queue: (Response | Error)[],
  config: WordPressClientConfig = CONFIG,
  logger: WordPressLogger = quiet,
) {
  const { fetchImpl, calls } = stubFetch(queue);
  return { client: new WordPressClient(config, { fetchImpl, sleep: noSleep, logger }), calls };
}

function authOf(call: Call): string | undefined {
  return (call.init.headers as Record<string, string> | undefined)?.Authorization;
}

/** Collects the client's diagnostics so a test can assert it warned rather than passed silently. */
function collectLogs(): { logger: WordPressLogger; lines: string[] } {
  const lines: string[] = [];
  return {
    logger: (level, message) => {
      lines.push(`${level}: ${message}`);
    },
    lines,
  };
}

/** The `file` part of a recorded multipart body. Reading it does not consume the FormData. */
function filePart(call: Call): File {
  const body = call.init.body;
  expect(body).toBeInstanceOf(FormData);
  const part = (body as FormData).get("file");
  if (part === null || typeof part === "string") {
    throw new Error("expected a file part in the multipart body");
  }
  return part;
}

const FIXTURE_DIR = mkdtempSync(join(tmpdir(), "wpmcp-"));
afterAll(() => {
  rmSync(FIXTURE_DIR, { recursive: true, force: true });
});

function fixture(name: string, body: string): { path: string; bytes: number; slug: string } {
  const path = join(FIXTURE_DIR, name);
  const data = Buffer.from(body);
  writeFileSync(path, data);
  const digest = createHash("sha256").update(data).digest("hex");
  // The expected slug is spelled out rather than derived with the implementation's own helpers,
  // so a regression in either one fails the test instead of moving the target with it.
  const base = name.replace(/\.[^.]+$/, "").toLowerCase();
  return { path, bytes: data.length, slug: `${base}-${digest.slice(0, 12)}` };
}

describe("multilingual detection", () => {
  it("detects polylang when the pll route responds", async () => {
    const { client } = makeClient([json(200, [{ slug: "nl" }])]);
    expect(await client.detectMultilingualPlugin()).toBe("polylang");
  });

  it("detects wpml when only the wpml route responds", async () => {
    const { client } = makeClient([new Response("", { status: 404 }), json(200, {})]);
    expect(await client.detectMultilingualPlugin()).toBe("wpml");
  });

  it("probes WPML's namespace root, not a route it does not register", async () => {
    const { client, calls } = makeClient([new Response("", { status: 404 }), json(200, {})]);

    await client.detectMultilingualPlugin();

    // `/wp-json/sitepress-multilingual-cms/v1/languages` is not a namespace WPML registers: it
    // 404s even on a WPML site, so detection always fell through to "none".
    expect(calls[1].url).toBe(`${SITE}/wp-json/wpml/v1`);
  });

  it("detects none when neither route responds", async () => {
    const { client } = makeClient([
      new Response("", { status: 404 }),
      new Response("", { status: 404 }),
    ]);
    expect(await client.detectMultilingualPlugin()).toBe("none");
  });

  it("reports what it detected without adopting it over the configured plugin", async () => {
    const { client } = makeClient(
      [new Response("", { status: 404 }), new Response("", { status: 404 })],
      WPML_CONFIG,
    );

    // A probe can be wrong — a renamed route, a version change, an admin-gated endpoint — and
    // letting one override a configured plugin fails silently: every page publishes unlinked.
    expect(await client.detectMultilingualPlugin()).toBe("none");
    expect(client.multilingualPlugin).toBe("wpml");
  });
});

describe("multilingual lookups and writes (§4.4)", () => {
  const upsertFr = {
    post_type: POST_TYPE,
    slug: "p-1",
    title: "T",
    content: "",
    language: "fr",
    meta: { gtin: "1" },
  };

  it("scopes both lookups to the language on a multilingual site", async () => {
    const { client, calls } = makeClient(
      [json(200, []), json(200, []), json(201, { id: 7, slug: "p-1" })],
      WPML_CONFIG,
    );

    // Unscoped, a slug lookup answers for the default language only. Both languages share the
    // GTIN-derived slug *and* the meta.gtin, so the E8 guard passes and the nl page is
    // overwritten with French — no fr page created, row reports ok.
    await client.upsertPage(upsertFr);

    const gets = calls.filter((c) => c.init.method === undefined || c.init.method === "GET");
    expect(gets).toHaveLength(2);
    for (const get of gets) {
      expect(get.url).toContain("lang=fr");
    }
  });

  it("carries ?lang= on create but not on update", async () => {
    const { client, calls } = makeClient(
      [
        json(200, []),
        json(200, []),
        json(201, { id: 7, slug: "p-1" }),
        json(200, [{ id: 7, slug: "p-1", meta: { gtin: "1" } }]),
        json(200, { id: 7, slug: "p-1" }),
      ],
      WPML_CONFIG,
    );

    await client.upsertPage(upsertFr);
    await client.upsertPage({ ...upsertFr, title: "T2" });

    const posts = calls.filter((c) => c.init.method === "POST");
    // Without ?lang= the create collides with its nl sibling and WordPress appends -2, so the
    // page lands at p-1-2 while the resolver target is built as p-1 — every French QR 404s.
    expect(posts[0].url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}?lang=fr`);
    expect(posts[1].url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}/7`);
    expect(posts[1].url).not.toContain("lang=");
  });

  it("sends no lang param on a single-language site", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(200, []),
      json(201, { id: 7, slug: "p-1" }),
    ]);

    await client.upsertPage({ ...upsertFr, language: "nl" });

    expect(calls.every((c) => !c.url.includes("lang="))).toBe(true);
  });

  it("writes acf in a second call, never on the create", async () => {
    const { client, calls } = makeClient(
      [
        json(200, []),
        json(200, []),
        json(201, { id: 7, slug: "p-1" }),
        json(200, { id: 7, slug: "p-1", acf: { product_title: "Tagline" } }),
      ],
      WPML_CONFIG,
    );

    // ?lang= and acf in one create silently drop the acf — 201, fields empty, no error.
    const page = await client.upsertPage({ ...upsertFr, acf: { product_title: "Tagline" } });

    const posts = calls.filter((c) => c.init.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0].init.body as string).not.toContain("acf");
    expect(JSON.parse(posts[1].init.body as string)).toEqual({
      acf: { product_title: "Tagline" },
    });
    expect(posts[1].url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}/7`);
    // The returned page is the ACF response, so it reflects what was written.
    expect((page as { acf?: unknown }).acf).toEqual({ product_title: "Tagline" });
  });

  it("makes no acf call when no acf is given", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(200, []),
      json(201, { id: 7, slug: "p-1" }),
    ]);

    await client.upsertPage({ ...upsertFr, language: "nl" });

    expect(calls.filter((c) => c.init.method === "POST")).toHaveLength(1);
  });
});

describe("meta.gtin lookup (§6.1)", () => {
  const upsert = {
    post_type: POST_TYPE,
    slug: "p-1",
    title: "T",
    content: "B",
    language: "nl",
    meta: { gtin: "1" },
  };

  it("ignores an unfiltered response instead of adopting an arbitrary page", async () => {
    // meta_key/meta_value are not core REST features — core drops unknown params, so a site with
    // no rest_{post_type}_query enabler answers with every post. Taking pages[0] there adopts an
    // unrelated page, which E8/E11 then rejects: every would-be create becomes a bogus collision.
    const { client, calls } = makeClient([
      json(200, []), // slug lookup: nothing
      json(200, [{ id: 55, meta: { gtin: "999" } }, { id: 56, meta: { gtin: "888" } }]),
      json(201, { id: 10, meta: { gtin: "1" } }),
    ]);

    const page = await client.upsertPage(upsert);

    expect(page.id).toBe(10); // created, not adopted
    expect(calls[2].url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}`);
  });

  it("returns the matching page when the server did filter", async () => {
    const { client } = makeClient([
      json(200, []),
      json(200, [{ id: 55, meta: { gtin: "1" } }]),
      json(200, { id: 55 }),
    ]);

    expect((await client.upsertPage(upsert)).id).toBe(55);
  });

  it("tolerates pages without meta in an unfiltered response", async () => {
    const { client } = makeClient([
      json(200, []),
      json(200, [{ id: 55 }, { id: 56, meta: null }, { id: 57, meta: { gtin: "1" } }]),
      json(200, { id: 57 }),
    ]);

    expect((await client.upsertPage(upsert)).id).toBe(57);
  });
});

describe("auth", () => {
  it("sends an HTTP Basic Authorization header", async () => {
    const { client, calls } = makeClient([json(200, [])]);

    await client.findBySlug(POST_TYPE, "p-1");

    const token = Buffer.from(`${USERNAME}:${APP_PASS}`).toString("base64");
    expect(authOf(calls[0])).toBe(`Basic ${token}`);
  });
});

describe("find_by_slug", () => {
  it("returns the first matching page", async () => {
    const { client } = makeClient([json(200, [{ id: 42, slug: "p-1" }])]);
    const page = await client.findBySlug(POST_TYPE, "p-1");
    expect(page?.id).toBe(42);
  });

  it("returns null on an empty list", async () => {
    const { client } = makeClient([json(200, [])]);
    expect(await client.findBySlug(POST_TYPE, "p-1")).toBeNull();
  });

  it("returns null on a 404 route", async () => {
    const { client } = makeClient([new Response("no route", { status: 404 })]);
    expect(await client.findBySlug(POST_TYPE, "p-1")).toBeNull();
  });
});

describe("upsert idempotency (§6.1)", () => {
  it("creates at the collection when absent", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(200, []),
      json(201, { id: 10, meta: { gtin: "1" } }),
    ]);

    const page = await client.upsertPage({
      post_type: POST_TYPE,
      slug: "p-1",
      title: "T",
      content: "B",
      language: "nl",
      meta: { gtin: "1" },
    });

    expect(page.id).toBe(10);
    const post = calls.find((c) => c.init.method === "POST");
    expect(post?.url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}`);
  });

  it("updates by id when found, keeping the same id", async () => {
    const { client, calls } = makeClient([
      json(200, [{ id: 10, slug: "p-1", meta: { gtin: "1" } }]),
      json(200, { id: 10 }),
    ]);

    const page = await client.upsertPage({
      post_type: POST_TYPE,
      slug: "p-1",
      title: "T",
      content: "NEW",
      language: "nl",
      meta: { gtin: "1" },
    });

    expect(page.id).toBe(10);
    const post = calls.find((c) => c.init.method === "POST");
    expect(post?.url).toBe(`${SITE}/wp-json/wp/v2/${POST_TYPE}/10`);
    expect(JSON.parse(post?.init.body as string).content).toBe("NEW");
  });
});

describe("edge cases E8, E11 (§7)", () => {
  it("raises GtinMismatchError on a different meta.gtin, without writing (E8)", async () => {
    const { client, calls } = makeClient([json(200, [{ id: 99, meta: { gtin: "999" } }])]);

    await expect(
      client.upsertPage({
        post_type: POST_TYPE,
        slug: "p-1",
        title: "T",
        content: "B",
        language: "nl",
        meta: { gtin: "1" },
      }),
    ).rejects.toBeInstanceOf(WordPressGtinMismatchError);
    expect(calls.some((c) => c.init.method === "POST")).toBe(false);
  });

  it("raises WordPressApiError on a non-GTIN slug collision (E11 proactive)", async () => {
    const { client } = makeClient([json(200, [{ id: 7, meta: {} }])]);

    await expect(
      client.upsertPage({
        post_type: POST_TYPE,
        slug: "p-1",
        title: "T",
        content: "B",
        language: "nl",
        meta: { gtin: "1" },
      }),
    ).rejects.toMatchObject({ statusCode: 409 });
  });

  it("raises WordPressApiError on a create-time 409, not retried (E11)", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(200, []),
      new Response("slug exists", { status: 409 }),
    ]);

    await expect(
      client.upsertPage({
        post_type: POST_TYPE,
        slug: "p-1",
        title: "T",
        content: "B",
        language: "nl",
        meta: { gtin: "1" },
      }),
    ).rejects.toMatchObject({ statusCode: 409 });
    expect(calls.filter((c) => c.init.method === "POST")).toHaveLength(1);
  });
});

const MEDIA_URL = `${SITE}/wp-json/wp/v2/media`;
const PNG = fixture("photo.png", "PNGDATA");
const PNG_DIGEST = createHash("sha256").update(Buffer.from("PNGDATA")).digest("hex");
const CLIP = fixture("clip.mp4", "VIDEODATA".repeat(100));

describe("media slug (§6.2)", () => {
  it("derives the base slug from the title, falling back to the filename", () => {
    expect(mediaSlug("Spray Jet", "Spray Jet NL.mp4")).toBe("spray-jet");
    expect(mediaSlug(undefined, "Spray Jet NL.mp4")).toBe("spray-jet-nl");
  });

  it("folds a 12-char hash prefix into the slug", () => {
    expect(contentSlug("spray-jet", "abcdef1234567890deadbeef")).toBe("spray-jet-abcdef123456");
  });
});

describe("upload_media idempotency (§6.2)", () => {
  it("uploads under a content-addressed slug, as multipart", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(201, { id: 5, media_details: { filesize: PNG.bytes } }),
      json(200, { id: 5 }),
    ]);

    expect(await client.uploadMedia(PNG.path, "Photo")).toBe(5);

    expect(calls[0].url).toContain(`slug=${PNG.slug}`);
    const creates = calls.filter((c) => c.init.method === "POST" && c.url === MEDIA_URL);
    expect(creates).toHaveLength(1);

    // The physical filename is content-addressed too, so WordPress never churns -1/-2 suffixes.
    const part = filePart(creates[0]);
    expect(part.name).toBe(`${PNG.slug}.png`);
    expect(part.type).toBe("image/png");
    expect(await part.text()).toBe("PNGDATA");

    // The boundary Content-Type is written when the body is *encoded*, so it can never appear in
    // the recorded init.headers. What carries the same weight: the client sets no content type of
    // its own (which would clobber the boundary) and no Content-Disposition — the raw-body form
    // that #62 removed, after a security plugin 403'd it on the live site.
    const headers = creates[0].init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
    expect(headers["Content-Disposition"]).toBeUndefined();
    // …and that what the client handed to fetch does in fact encode as multipart. This is the
    // literal mirror of the Python assertion, obtained one layer down.
    expect(
      new Request(SITE, { method: "POST", body: creates[0].init.body }).headers.get("content-type"),
    ).toMatch(/^multipart\/form-data; boundary=/);

    const finalise = calls.find((c) => c.url === `${MEDIA_URL}/5`);
    expect(JSON.parse(finalise?.init.body as string)).toMatchObject({
      slug: PNG.slug,
      meta: { content_sha256: PNG_DIGEST },
    });
  });

  it("reuses on the content slug even when the attachment carries no meta at all", async () => {
    // No `meta` on the item on purpose: dedup must not depend on attachment meta being exposed
    // in REST, which on the live site it was not.
    const { client, calls } = makeClient([
      json(200, [{ id: 5, slug: PNG.slug, media_details: { filesize: PNG.bytes } }]),
    ]);

    expect(await client.uploadMedia(PNG.path, "Photo")).toBe(5);
    expect(calls.some((c) => c.init.method === "POST")).toBe(false);
  });

  it("declares an unknown extension as application/octet-stream", async () => {
    const odd = fixture("thing.xyz", "ODDBYTES");
    const { client, calls } = makeClient([
      json(200, []),
      json(201, { id: 5, media_details: { filesize: odd.bytes } }),
      json(200, { id: 5 }),
    ]);

    await client.uploadMedia(odd.path);

    const part = filePart(calls[1]);
    expect(part.type).toBe("application/octet-stream");
    expect(part.name).toBe(`${odd.slug}.xyz`);
  });

  it("declares a video part as video/mp4", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(201, { id: 5, media_details: { filesize: CLIP.bytes } }),
      json(200, { id: 5 }),
    ]);

    await client.uploadMedia(CLIP.path);

    expect(filePart(calls[1]).type).toBe("video/mp4");
  });
});

describe("upload_media integrity (#72)", () => {
  it("deletes and raises when WordPress stored a fragment", async () => {
    const { client, calls } = makeClient([
      json(200, []),
      json(201, { id: 7, media_details: { filesize: 150 } }),
      json(200, { deleted: true }),
    ]);

    await expect(client.uploadMedia(CLIP.path)).rejects.toMatchObject({
      name: "MediaIntegrityError",
      sentBytes: CLIP.bytes,
      storedBytes: 150,
      mediaId: 7,
      deleted: true,
    });

    expect(calls[2].init.method).toBe("DELETE");
    expect(calls[2].url).toBe(`${MEDIA_URL}/7?force=true`);
    // The finalise never went out — so the fragment never claimed the content-addressed slug.
    expect(calls).toHaveLength(3);
  });

  it("still reports the truncation when the cleanup itself fails", async () => {
    const { client } = makeClient([
      json(200, []),
      json(201, { id: 7, media_details: { filesize: 1 } }),
      json(500, {}),
      json(500, {}),
      json(500, {}),
    ]);

    // The delete error must not replace the integrity failure it is cleaning up after.
    const err = await client.uploadMedia(CLIP.path).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(MediaIntegrityError);
    expect((err as MediaIntegrityError).deleted).toBe(false);
    expect((err as MediaIntegrityError).message).toMatch(/remove media 7 by hand/);
  });

  it("falls back to an unauthenticated HEAD when WordPress will not say", async () => {
    const src = `${SITE}/wp-content/uploads/clip.mp4`;
    const { client, calls } = makeClient([
      json(200, []),
      json(201, { id: 7, source_url: src }),
      new Response("", { headers: { "Content-Length": "450" } }),
      json(200, { deleted: true }),
    ]);

    await expect(client.uploadMedia(CLIP.path)).rejects.toMatchObject({ storedBytes: 450 });

    expect(calls[2].url).toBe(src); // absolute, not prefixed with the API base
    expect(calls[2].init.method).toBe("HEAD");
    expect(authOf(calls[2])).toBeUndefined(); // a second opinion, not an API call
  });

  it("treats a HEAD with no Content-Length as unverified, not as zero bytes", async () => {
    const src = `${SITE}/wp-content/uploads/clip.mp4`;
    const { logger, lines } = collectLogs();
    const { client, calls } = makeClient(
      [
        json(200, []),
        json(201, { id: 7, source_url: src }),
        new Response("", { status: 200 }), // 200, but the proxy omitted the header
        json(200, { id: 7 }),
      ],
      CONFIG,
      logger,
    );

    // `Number(null)` is 0, and "0 bytes stored" reads as a mismatch — which would delete a
    // perfectly good upload on evidence nobody supplied.
    expect(await client.uploadMedia(CLIP.path)).toBe(7);
    expect(calls.some((c) => c.init.method === "DELETE")).toBe(false);
    expect(lines.join("\n")).toMatch(/no usable Content-Length/);
  });

  it("warns rather than passing silently when the size cannot be established", async () => {
    const { logger, lines } = collectLogs();
    const { client } = makeClient(
      [json(200, []), json(201, { id: 7 }), json(200, { id: 7 })],
      CONFIG,
      logger,
    );

    expect(await client.uploadMedia(CLIP.path)).toBe(7);
    expect(lines.join("\n")).toMatch(/unverified/);
    expect(lines.join("\n")).toMatch(/clip\.mp4/);
  });

  it("sends diagnostics to stderr by default, never stdout", async () => {
    // stdout carries the MCP protocol frames — a diagnostic written there corrupts the session.
    const stderr = vi.spyOn(process.stderr, "write").mockReturnValue(true);
    const stdout = vi.spyOn(process.stdout, "write").mockReturnValue(true);
    try {
      const { fetchImpl } = stubFetch([json(200, []), json(201, { id: 7 }), json(200, { id: 7 })]);
      // No logger injected: this is the production default.
      await new WordPressClient(CONFIG, { fetchImpl, sleep: noSleep }).uploadMedia(CLIP.path);

      expect(stderr.mock.calls.map(String).join("")).toMatch(/wordpress-mcp.+unverified/);
      expect(stdout).not.toHaveBeenCalled();
    } finally {
      stderr.mockRestore();
      stdout.mockRestore();
    }
  });

  it("discards a deduped fragment and re-uploads", async () => {
    const { client, calls } = makeClient([
      json(200, [
        {
          id: 7,
          slug: CLIP.slug,
          media_details: { filesize: 150 },
          meta: { content_sha256: "abc123" },
        },
      ]),
      json(200, { deleted: true }),
      json(201, { id: 8, media_details: { filesize: CLIP.bytes } }),
      json(200, { id: 8 }),
    ]);

    // Re-running is the only thing that can repair a truncated upload, so the fragment must go.
    expect(await client.uploadMedia(CLIP.path)).toBe(8);
    expect(calls[1].init.method).toBe("DELETE");
    expect(calls.some((c) => c.init.method === "POST" && c.url === MEDIA_URL)).toBe(true);
  });

  it("never deletes a same-slug attachment that is not ours", async () => {
    // meta.content_sha256 is registered site-wide on the pilot, so it is present and empty on the
    // client's own 366 attachments — which is why non-empty is the test, not present.
    const { client, calls } = makeClient([
      json(200, [
        {
          id: 7,
          slug: CLIP.slug,
          media_details: { filesize: 150 },
          meta: { _acf_changed: false, content_sha256: "" },
        },
      ]),
    ]);

    await expect(client.uploadMedia(CLIP.path)).rejects.toBeInstanceOf(MediaOwnershipError);
    expect(calls).toHaveLength(1); // no delete, no upload
  });

  it("reuses an unverifiable dedup hit rather than deleting it", async () => {
    const { client, calls } = makeClient([json(200, [{ id: 7, slug: CLIP.slug }])]);

    // Deleting on a number nobody supplied would remove a live page's media on a guess.
    expect(await client.uploadMedia(CLIP.path)).toBe(7);
    expect(calls.some((c) => c.init.method === "DELETE")).toBe(false);
  });
});

describe("verify_url", () => {
  it("returns true for a 2xx", async () => {
    const { client } = makeClient([new Response("", { status: 200 })]);
    expect(await client.verifyUrl(`${SITE}/p/1`)).toBe(true);
  });

  it("throws for a 404", async () => {
    const { client } = makeClient([new Response("", { status: 404 })]);
    await expect(client.verifyUrl(`${SITE}/p/1`)).rejects.toMatchObject({ statusCode: 404 });
  });
});

describe("retry policy (§5.1)", () => {
  it("retries once on 429 then succeeds", async () => {
    const { client, calls } = makeClient([
      new Response("", { status: 429, headers: { "Retry-After": "0" } }),
      json(200, []),
    ]);
    await client.findBySlug(POST_TYPE, "p-1");
    expect(calls).toHaveLength(2);
  });

  it("throws after 5xx retries are exhausted", async () => {
    const { client, calls } = makeClient([json(500, {}), json(500, {}), json(500, {})]);
    await expect(client.findBySlug(POST_TYPE, "p-1")).rejects.toBeInstanceOf(WordPressApiError);
    expect(calls).toHaveLength(3);
  });

  it("treats a 401 as terminal (not retried)", async () => {
    const { client, calls } = makeClient([new Response("", { status: 401 })]);
    await expect(client.findBySlug(POST_TYPE, "p-1")).rejects.toMatchObject({ statusCode: 401 });
    expect(calls).toHaveLength(1);
  });

  it("retries network errors then raises status 0", async () => {
    const { client } = makeClient([
      new TypeError("fetch failed"),
      new TypeError("fetch failed"),
      new TypeError("fetch failed"),
    ]);
    await expect(client.findBySlug(POST_TYPE, "p-1")).rejects.toMatchObject({ statusCode: 0 });
  });
});

describe("scrubbing", () => {
  it("never leaks the application password in thrown errors", async () => {
    const { client } = makeClient([json(400, { code: "rest_invalid" })]);

    let caught: unknown;
    try {
      await client.findBySlug(POST_TYPE, "p-1");
    } catch (err) {
      caught = err;
    }
    const serialized = JSON.stringify({
      message: (caught as Error).message,
      body: (caught as WordPressApiError).responseBody,
    });
    expect(serialized).not.toContain(APP_PASS);
    expect(serialized).not.toContain(Buffer.from(`${USERNAME}:${APP_PASS}`).toString("base64"));
  });
});
