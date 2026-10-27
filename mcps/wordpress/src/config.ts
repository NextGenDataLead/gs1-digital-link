/**
 * Resolve a client's WordPress config from `clients.yml` (IMPLEMENTATION_SPEC §9.2).
 *
 * The MCP hides plumbing and resolves the application password from the env var named
 * in `app_password_env`, by `client_id`. TS-native counterpart to `lib/config.py`'s
 * `WordPressConfig`; the two read the same file independently.
 */

import { readFileSync } from "node:fs";
import { load as parseYaml } from "js-yaml";

import type { MultilingualPlugin, WordPressClientConfig } from "./client.js";

const DEFAULT_CLIENTS_FILE = "clients.yml";
const DEFAULT_POST_TYPE = "page";
const DEFAULT_POST_STATUS = "publish";
const DEFAULT_LANGUAGE = "nl";

/** Shape of the `wordpress` config block (defaults + per-client), as read from YAML. */
interface RawWordPressBlock {
  site_url?: string;
  username?: string;
  app_password_env?: string;
  post_type?: string;
  post_status?: string;
  multilingual_plugin?: MultilingualPlugin;
  default_language?: string;
  languages?: string[];
}

interface RawClientsFile {
  defaults?: { wordpress?: RawWordPressBlock };
  clients?: Record<string, { wordpress?: RawWordPressBlock }>;
}

function requireEnv(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name];
  if (value === undefined || value === "") {
    throw new Error(`Environment variable ${name} is not set`);
  }
  return value;
}

/**
 * Merge defaults with a client's `wordpress` block and resolve the application password
 * from `env`. Pure (no filesystem) so it is unit-testable.
 */
export function resolveWordPressConfig(
  raw: RawClientsFile,
  clientId: string,
  env: NodeJS.ProcessEnv,
): WordPressClientConfig {
  const client = raw.clients?.[clientId];
  if (client === undefined) {
    throw new Error(`Unknown client_id: ${clientId}`);
  }
  const defaults = raw.defaults?.wordpress ?? {};
  const merged: RawWordPressBlock = { ...defaults, ...(client.wordpress ?? {}) };

  if (merged.site_url === undefined || merged.username === undefined) {
    throw new Error(`Missing wordpress.site_url/username for client ${clientId}`);
  }
  if (merged.app_password_env === undefined) {
    throw new Error(`Missing wordpress.app_password_env for client ${clientId}`);
  }

  return {
    siteUrl: merged.site_url,
    username: merged.username,
    appPassword: requireEnv(env, merged.app_password_env),
    postType: merged.post_type ?? DEFAULT_POST_TYPE,
    postStatus: merged.post_status ?? DEFAULT_POST_STATUS,
    multilingualPlugin: merged.multilingual_plugin ?? "none",
    defaultLanguage: merged.default_language ?? DEFAULT_LANGUAGE,
    languages: merged.languages ?? [merged.default_language ?? DEFAULT_LANGUAGE],
  };
}

/** Load `clients.yml` and resolve the config for one client. */
export function loadWordPressClientConfig(
  clientId: string,
  options: { clientsPath?: string; env?: NodeJS.ProcessEnv } = {},
): WordPressClientConfig {
  const path = options.clientsPath ?? process.env.GS1_CLIENTS_FILE ?? DEFAULT_CLIENTS_FILE;
  const raw = parseYaml(readFileSync(path, "utf8")) as RawClientsFile;
  return resolveWordPressConfig(raw, clientId, options.env ?? process.env);
}
