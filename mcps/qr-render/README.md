# qr-render MCP

MCP server that renders GS1 Digital Link QR symbols on demand (IMPLEMENTATION_SPEC §9.3).
The TypeScript sibling of `lib/qr.py`: it applies the same uppercase-domain optimisation and
writes `{output_dir}/{gtin}.{ext}` for the requested formats.

## Tool

### `qr_render`

Render a QR symbol for a Digital Link URI.

| Field | Type | Notes |
| --- | --- | --- |
| `uri` | string | Digital Link URI (lowercase; the domain is uppercased internally). |
| `output_dir` | string | Directory to write into (created if absent). |
| `gtin` | string | Output filename stem. |
| `formats` | `("svg"\|"png"\|"eps")[]` | Formats to render; output order matches. |
| `size_mm` | integer | Target physical edge length, in millimetres. |
| `error_correction` | `"L"\|"M"\|"Q"\|"H"` | QR error-correction level. |

Returns `{ ok: true, error: null, paths: string[] }` on success, or
`{ ok: false, error }` (with `isError: true`) on failure.

## Rendering notes

- **Self-contained**: uses the npm `qrcode` package for PNG; SVG and EPS are emitted directly
  from the QR module matrix (npm `qrcode` has no EPS writer), so all three formats stay
  consistent and the `eps` enum is honoured.
- **Uppercase-domain optimisation**: scheme + host are uppercased (path preserved) so the URI
  falls inside the QR alphanumeric character set, yielding a smaller symbol.
- The byte-determinism contract (§6.4) is verified on the Python `lib/qr.py` renderer; this
  server mirrors its output shape.

## Scripts

```bash
npm -w mcps/qr-render run build      # tsc -> dist/
npm -w mcps/qr-render test           # vitest
npm -w mcps/qr-render start          # serve over stdio
```
