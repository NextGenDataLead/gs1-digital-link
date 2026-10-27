<!-- prompt_version: v1 | client: democlient -->
# Democlient product-copy voice & contract (v1)

The frozen brand-voice template for generating Democlient product copy — the worked example a new
client's prompt is written from. It is loaded for one `(gtin, language)` at a time, by the
`content-generator` skill in a Claude Code session or by the headless API backend. The examples
below **are** the `v1` voice: changing them is a new voice and requires bumping `prompt_version`
(which re-fingerprints and makes existing copy read as stale).

On an installation this file lives in the data folder, at
`input/democlient/prompts/generation.v1.md`, and travels in a handover pack. The copy in the
program folder is the fallback.

Democlient sells everyday household and garden products. Its site has two languages: `nl` and `fr`.
The examples below are invented for this demo; replace them with lines from your client's own
live pages.

## What to produce

For each request, return one ranked `usps` list (and, only when asked, a `product_name`):

- **`usps[0]` = the tagline.** One short benefit line: the page headline and the opening line of
  the description. Write it *from* the marketing message; never paste the message itself. Aim for
  roughly 30–60 characters.
- **`usps[1:]` = the feature bullets.** Most important first, each ideally ≤ 80 characters. Two to
  four is typical; one strong tagline alone is valid when there is nothing more to say.
- **`product_name`**: supply it **only** when the request's `needs_name` is true (the feed has no
  name in this language, so translate the functional name). Otherwise omit it.

Never put a measurement, dimension or material into `usps`: those are assembled deterministically
downstream.

## The two modes

- **`mode = "tighten"`**: the feed already carries usable selling points in `candidates`, but
  they are too long. **Shorten and rank them** into the tagline and bullets; keep their meaning and
  invent no new claims.
- **`mode = "generate"`**: no usable selling points. Write from `inputs.marketing_message`, using
  `functional_name`, `net_content`, dimensions and `material` as context. If the marketing message
  is blank, write from `functional_name` and whatever context exists, honestly and minimally; the
  blank input is flagged for the operator separately.

## Voice, per language

Benefit-first, second person, everyday-practical: say what the product does *for the user*.

**Dutch (`nl`)**: short fragments, verb-less or a single imperative, **no trailing period**,
about 30–55 characters, addressing the user with `je`. For example:

- `Houdt je keukenkastjes netjes en overzichtelijk`
- `Makkelijk schoon te maken`
- `Stevig en toch licht`

**French (`fr`)**: imperative-led (`Rangez…`, `Nettoyez…`) or a `Un/Une + adjective` noun
phrase, about 50–90 characters, usually ending with a period, addressing the user with `vous`.
For example:

- `Rangez votre cuisine en un clin d'œil grâce à ce panier pratique.`
- `Un accessoire solide et léger pour tous les jours.`

## Worked example

For a kitchen storage basket, in Dutch:

```json
{ "usps": ["Houdt je keukenkastjes netjes en overzichtelijk", "Past in elk standaard kastje", "Makkelijk schoon te maken"] }
```

## Do not

- **Do not** paste the raw marketing message as the tagline; it is tone reference only.
- **Do not** surface placeholder or junk values (for example `zzz…`); treat them as absent.
- **Do not** name a sub-brand unless the source copy for this product already names it.
- **Do not** emit specs (size, weight, material) as selling points. A count that is genuinely a
  benefit (`Set van 3 voor de hele keuken`) is fine; a bare dimension is not.
