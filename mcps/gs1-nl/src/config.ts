/**
 * Resolve a client's GS1 config from `clients.yml` (IMPLEMENTATION_SPEC §9.1).
 *
 * The MCP hides plumbing (`accountNumber`, `resolverSettings`, `auth_scheme`) and
 * resolves it by `client_id`. This is the TS-native counterpart to Phase 3's
 * Python `lib/config.py`; the two read the same file independently.
 */

import { readFileSync } from "node:fs";
import { load as parseYaml } from "js-yaml";

import type { Environment, GS1ClientConfig } from "./client.js";
import { HOSTS } from "./client.js";

const DEFAULT_BATCH_SIZE = 50;
const DEFAULT_CLIENTS_FILE = "clients.yml";

/**
 * Shape of the `gs1` config block (defaults + per-client), as read from YAML.
 * Account number and OAuth2 client credentials are per-environment (test vs
 * production), like the credentials themselves.
 */
interface RawGs1Block {
  environment?: Environment;
  account_number_test?: string;
  account_number_production?: string;
  client_id_env_test?: string;
  client_secret_env_test?: string;
  client_id_env_production?: string;
  client_secret_env_production?: string;
  batch_size?: number;
  resolver_settings?: {
    use_gs1_resolver?: boolean;
    resolver_domain_name?: string | null;
  };
}

interface RawClientsFile {
  defaults?: { gs1?: RawGs1Block };
  clients?: Record<string, { gs1?: RawGs1Block }>;
}

function requireEnv(env: NodeJS.ProcessEnv, name: string): string {
  const value = env[name];
  if (value === undefined || value === "") {
    throw new Error(`Environment variable ${name} is not set`);
  }
  return value;
}

/**
 * Merge defaults with a client's `gs1` block and resolve the environment-specific
 * account number and OAuth2 client credentials from `env`. Pure (no filesystem)
 * so it is unit-testable.
 */
export function resolveGS1Config(
  raw: RawClientsFile,
  clientId: string,
  env: NodeJS.ProcessEnv,
): GS1ClientConfig {
  const client = raw.clients?.[clientId];
  if (client === undefined) {
    throw new Error(`Unknown client_id: ${clientId}`);
  }
  const defaults = raw.defaults?.gs1 ?? {};
  const merged: RawGs1Block = { ...defaults, ...(client.gs1 ?? {}) };
  const environment: Environment = merged.environment ?? "test";
  const suffix = environment === "production" ? "production" : "test";

  const accountNumber =
    environment === "production" ? merged.account_number_production : merged.account_number_test;
  if (accountNumber === undefined) {
    throw new Error(`Missing gs1.account_number_${suffix} for client ${clientId}`);
  }

  const clientIdEnv =
    environment === "production" ? merged.client_id_env_production : merged.client_id_env_test;
  const clientSecretEnv =
    environment === "production"
      ? merged.client_secret_env_production
      : merged.client_secret_env_test;
  if (clientIdEnv === undefined || clientSecretEnv === undefined) {
    throw new Error(
      `Missing gs1.client_id_env_${suffix}/client_secret_env_${suffix} for client ${clientId}`,
    );
  }

  const resolver = merged.resolver_settings ?? {};
  return {
    host: HOSTS[environment],
    environment,
    accountNumber,
    clientId: requireEnv(env, clientIdEnv),
    clientSecret: requireEnv(env, clientSecretEnv),
    resolverSettings: {
      useGS1Resolver: resolver.use_gs1_resolver ?? true,
      resolverDomainName: resolver.resolver_domain_name ?? null,
    },
    batchSize: merged.batch_size ?? DEFAULT_BATCH_SIZE,
  };
}

/** Load `clients.yml` and resolve the config for one client. */
export function loadGS1ClientConfig(
  clientId: string,
  options: { clientsPath?: string; env?: NodeJS.ProcessEnv } = {},
): GS1ClientConfig {
  const path = options.clientsPath ?? process.env.GS1_CLIENTS_FILE ?? DEFAULT_CLIENTS_FILE;
  const raw = parseYaml(readFileSync(path, "utf8")) as RawClientsFile;
  return resolveGS1Config(raw, clientId, options.env ?? process.env);
}
