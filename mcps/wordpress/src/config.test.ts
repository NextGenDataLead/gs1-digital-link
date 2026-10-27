/** Tests for resolving a client's WordPress config from clients.yml (§9.2). */

import { describe, expect, it } from "vitest";

import { resolveWordPressConfig } from "./config.js";

const RAW = {
  defaults: { wordpress: { post_status: "publish", multilingual_plugin: "none" } },
  clients: {
    democlient: {
      wordpress: {
        site_url: "https://www.democlient.nl",
        username: "gs1-publisher",
        app_password_env: "DEMOCLIENT_WP_APP_PASS",
        post_type: "democlient",
        multilingual_plugin: "polylang" as const,
        default_language: "nl",
        languages: ["nl", "fr"],
      },
    },
  },
};

describe("resolveWordPressConfig", () => {
  it("resolves fields and the app password, merging defaults", () => {
    const config = resolveWordPressConfig(RAW, "democlient", {
      DEMOCLIENT_WP_APP_PASS: "app-pass-value",
    });

    expect(config.siteUrl).toBe("https://www.democlient.nl");
    expect(config.username).toBe("gs1-publisher");
    expect(config.appPassword).toBe("app-pass-value");
    expect(config.postType).toBe("democlient");
    expect(config.postStatus).toBe("publish"); // inherited from defaults
    expect(config.multilingualPlugin).toBe("polylang");
    expect(config.languages).toEqual(["nl", "fr"]);
  });

  it("throws for an unknown client", () => {
    expect(() => resolveWordPressConfig(RAW, "ghost", {})).toThrow(/Unknown client_id/);
  });

  it("throws when the app-password env var is unset", () => {
    expect(() => resolveWordPressConfig(RAW, "democlient", {})).toThrow(/DEMOCLIENT_WP_APP_PASS/);
  });

  it("throws when site_url or username is missing", () => {
    const raw = { clients: { democlient: { wordpress: { app_password_env: "X" } } } };
    expect(() => resolveWordPressConfig(raw, "democlient", { X: "v" })).toThrow(
      /site_url\/username/,
    );
  });
});
