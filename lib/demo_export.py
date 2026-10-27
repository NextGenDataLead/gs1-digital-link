"""A synthetic GS1 Data Source / GDSN export for the example client.

The pipeline's own test data. ``democlient`` is defined in ``clients.example.yml`` but has never
had an export it could actually parse, so the only way to reach a populated Data screen was to
drive it against the live client — which rewrote that operator's real scope list three times.
This module is the fix: a workbook shaped like the datapool export a real client produces,
carrying nothing but invented products.

**What "faithful" means here**, since a fixture that merely satisfies the parser would teach us
nothing:

* One worksheet per GDSN module, all :data:`lib.gdsn_layout.MODULES` of them, keyed on ``Gtin`` +
  ``TargetMarketCountryCode`` + ``TradeItemUnitDescriptorCode`` like the real thing. Nineteen of
  those sheets are not in ``democlient``'s ``gdsn_map`` and are emitted anyway: an export is
  mostly sheets the config ignores, and how the reader treats them — skipping the ones with no
  digit-keyed rows, offering the rest to ``inspect_export`` — is part of what a rehearsal is for.
* Seven header rows, data from the eighth, each column's identity spread down them as a nested
  attribute path plus a label carrying the GDSN attribute number. :mod:`lib.gdsn_layout` holds
  that shape.
* The same GTIN recurs once per target market, and **which market holds a given value varies by
  product**. That is what ``market_priority`` exists to resolve, so the catalogue contains
  products that need it.
* Every edge the readers have code for is represented on purpose: a cross-market disagreement, a
  language present in one market only, a language absent everywhere, multi-slot repeated groups
  in both the localised and the language-agnostic spelling, a non-consumer-unit row, an empty
  row, and one product held by each mandatory-field rule the config arms.

**The GTINs are unallocatable by construction.** They carry GS1 prefix ``029`` — restricted
distribution, reserved for internal use and never issued to a company — so no demo barcode here
can collide with a real product anywhere. The check digits are valid all the same: an invalid one
is a trap for whatever downstream check eventually looks.

``clients.example.yml``'s ``democlient`` block is the authority for what this must resolve, and
``tests/lib/test_demo_export.py`` reads that file rather than restating it — the fixture and the
config are authored independently, and the test is what holds them together.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from lib.gdsn import CONSUMER_UNIT
from lib.gdsn_layout import (
    KEY_COLUMNS,
    MODULES,
    Column,
    header_rows,
    localised_cells,
    localised_columns,
    measurement_cells,
    measurement_columns,
    multislot_cells,
)

#: A case-level row's unit descriptor, present so that skipping it is exercised, not assumed.
_CASE_UNIT: Final = "CASE"

#: The information provider's GLN. Restricted-prefix like the GTINs, and read by nothing here.
_GLN: Final = "0290000000009"

#: Markets. ``market_priority`` in the config is what ranks them; this module only places values.
_NL: Final = "528"
_BE: Final = "056"
_DE: Final = "276"

#: Fixed, so that regenerating the workbook twice produces the same rows. Nothing reads it; a
#: ``datetime`` rather than a string only because that is what a real export puts in the cell.
_QUALIFIED: Final = datetime(2026, 1, 14, 9, 30, 0)

#: Where the placeholder hero images come from. A host that actually serves, so a media step run
#: against this export downloads a real image instead of failing on a URL invented to look right.
_IMAGE_HOST: Final = "https://placehold.co"

#: The languages the catalogue is written in. Must match ``democlient``'s ``wordpress.languages``
#: — a fixture in a language the config does not resolve teaches nothing — and
#: ``tests/lib/test_demo_export.py`` checks that against the file rather than trusting this note.
LANGUAGES: Final[tuple[str, ...]] = ("nl", "fr")

#: Millimetres, the unit the dimension extras are declared ``with_unit`` to keep.
_MM: Final = "MMT"

#: The invented brand. One brand across the catalogue, as a single client's feed has.
BRAND: Final = "Demoplast"


def check_digit(body: str) -> str:
    """The GS1 check digit for ``body`` — the code without it.

    Weights alternate 3-1 from the **right**, which is why this walks the reversed string: the
    weighting is anchored to the check digit's own position, not to the code's length, so writing
    it left to right gets GTIN-13 and GTIN-14 opposite ways round.
    """
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def _gtin(serial: int) -> str:
    """A 14-digit demo GTIN: a zero-padded EAN-13 on the restricted-distribution prefix 029."""
    body = f"029{serial:09d}"
    return f"0{body}{check_digit(body)}"


@dataclass(frozen=True)
class DemoProduct:
    """One invented product, and how its values are spread across markets.

    The spread is the point. A product whose every value sat in every market would resolve
    identically under any ``market_priority`` and would exercise none of the ranking the reader
    exists to do.

    Attributes:
        gtin: 14 digits, restricted prefix — see :func:`_gtin`.
        name: 3301 Functional Name, by language. The page title.
        logistics: 3297 DescriptionShort, by language. An internal string, carried not published.
        description: 3318 Product description, by language.
        message: 1083 marketing message, by language. Paragraph-length.
        benefits: 1067 features and benefits, by language, one entry per repeated slot.
        net_content: ``(value, unit code)`` for 3510.
        weight: ``(gross, net)`` in grams, for 3556/3559.
        dims: ``(height, width, depth)`` in millimetres, or ``None`` to leave all three blank and
            let the mandatory-extras rule hold the product.
        gpc: The GPC brick code.
        materials: 4.012 Material, one entry per repeated slot, language-agnostic.
        images: How many referenced-file groups to emit; ``0`` leaves the product with no hero.
        variation: 3332 Product variation, by language. Usually empty, as in a real feed.
        markets: The markets this product has a row in, on every sheet.
        gaps: Per market, the languages that market does **not** carry.
        name_conflicts: Per market, replacement 3301 values by language — the case where two
            markets disagree about the same product.
    """

    gtin: str
    name: Mapping[str, str]
    logistics: Mapping[str, str]
    description: Mapping[str, str]
    message: Mapping[str, str]
    benefits: Mapping[str, Sequence[str]]
    net_content: tuple[str, str]
    #: ``(gross, net)`` in grams, for 3556/3559. Carried, not published.
    weight: tuple[str, str]
    dims: tuple[str, str, str] | None
    gpc: str
    materials: Sequence[str]
    images: int = 2
    variation: Mapping[str, str] = field(default_factory=dict)
    markets: tuple[str, ...] = (_NL, _BE)
    gaps: Mapping[str, Sequence[str]] = field(default_factory=dict)
    name_conflicts: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def carried(self, market: str) -> tuple[str, ...]:
        """The languages this market's row holds values in."""
        absent = self.gaps.get(market, ())
        return tuple(language for language in LANGUAGES if language not in absent)

    def names_in(self, market: str) -> dict[str, str]:
        """3301 as this market spells it — the conflict overrides, over the base values."""
        return {**self.name, **self.name_conflicts.get(market, {})}


#: The catalogue. Twelve products, nine of them cleanly publishable and three held — one by each
#: mandatory rule the config arms — because a demo in which nothing is ever held shows the
#: operator a screen they will not recognise the first time a real export disappoints them.
CATALOGUE: Final[tuple[DemoProduct, ...]] = (
    DemoProduct(
        gtin=_gtin(1),
        name={"nl": "Voegstrijker", "fr": "Lisseur à joints"},
        logistics={"nl": "Voegstrijker kunststof grs", "fr": "Lisseur joints plast grs"},
        description={"nl": "Demoplast Voegstrijker kunststof", "fr": "Demoplast Lisseur à joints"},
        message={
            "nl": "Werk voegen strak af met deze voegstrijker. Het zachte profiel volgt de "
            "voeg en laat een gelijkmatige afwerking achter, ook in hoeken.",
            "fr": "Finissez vos joints avec précision. Le profil souple épouse le joint et "
            "laisse une finition régulière, y compris dans les angles.",
        },
        benefits={
            "nl": ("Zacht profiel voor een strakke voeg", "Werkt ook in hoeken", "Afspoelbaar"),
            "fr": ("Profil souple pour un joint net", "Fonctionne aussi dans les angles"),
        },
        net_content=("1", "H87"),
        weight=("60", "45"),
        dims=("210", "45", "12"),
        gpc="10003865",
        materials=("kunststof",),
    ),
    DemoProduct(
        gtin=_gtin(2),
        name={"nl": "Oliefles", "fr": "Bouteille d'huile"},
        logistics={"nl": "Oliefles glas 500ml grs", "fr": "Bouteille huile verre grs"},
        description={"nl": "Demoplast Oliefles 500 ml", "fr": "Demoplast Bouteille d'huile"},
        message={
            "nl": "Doseer olie, azijn of dressing nauwkeurig met deze fles met doseerpomp. "
            "De glazen houder geeft meteen zicht op de inhoud.",
            "fr": "Dosez l'huile, le vinaigre ou la vinaigrette avec précision grâce à la "
            "pompe doseuse. Le récipient en verre laisse voir le contenu.",
        },
        benefits={
            "nl": ("Doseerpomp voorkomt morsen", "Duidelijke maatverdeling"),
            "fr": ("La pompe évite les éclaboussures", "Graduation lisible"),
        },
        net_content=("500", "MLT"),
        weight=("620", "560"),
        dims=("205", "95", "95"),
        gpc="10005426",
        # Two of the three Material slots, so the language-agnostic multivalue join is exercised
        # with a real gap after it rather than a full sequence.
        materials=("glas", "kunststof"),
    ),
    DemoProduct(
        gtin=_gtin(3),
        name={"nl": "Rugsteun", "fr": "Support dorsal"},
        logistics={"nl": "Rugsteun textiel grs", "fr": "Support dorsal textile grs"},
        description={"nl": "Demoplast Rugsteun", "fr": "Demoplast Support dorsal"},
        message={
            "nl": "Zit ergonomisch en comfortabel met deze rugsteun van ademend materiaal.",
            "fr": "Asseyez-vous confortablement grâce à ce support en matériau respirant.",
        },
        benefits={
            "nl": ("Ademend materiaal", "Verstelbare band"),
            "fr": ("Matériau respirant", "Sangle réglable"),
        },
        net_content=("1", "H87"),
        weight=("420", "380"),
        dims=("400", "380", "95"),
        gpc="10005844",
        materials=("textiel",),
        # The two markets disagree about the French name. Neither is blank, so nothing is missing
        # and no rule fires — it surfaces only as a cross-market inconsistency, which is exactly
        # the finding that is invisible without a report.
        name_conflicts={_BE: {"fr": "Dossier de soutien"}},
    ),
    DemoProduct(
        gtin=_gtin(4),
        name={"nl": "Snoeischaar", "fr": "Sécateur"},
        logistics={"nl": "Snoeischaar metaal grs", "fr": "Sécateur métal grs"},
        description={"nl": "Demoplast Snoeischaar", "fr": "Demoplast Sécateur"},
        message={
            "nl": "Snoei takken tot 20 mm met deze schaar met antislip handgrepen.",
            "fr": "Taillez des branches jusqu'à 20 mm avec ce sécateur à poignées antidérapantes.",
        },
        benefits={
            "nl": ("Snijdt takken tot 20 mm", "Antislip handgrepen", "Vergrendeling"),
            "fr": ("Coupe jusqu'à 20 mm", "Poignées antidérapantes", "Verrouillage"),
        },
        net_content=("1", "H87"),
        weight=("260", "230"),
        dims=("220", "85", "25"),
        gpc="10003865",
        materials=("metaal", "kunststof"),
        variation={"nl": "bypass", "fr": "bypass"},
        markets=(_NL, _BE, _DE),
    ),
    DemoProduct(
        gtin=_gtin(5),
        name={"nl": "Plantenspuit", "fr": "Pulvérisateur"},
        logistics={"nl": "Plantenspuit kunststof grs", "fr": "Pulvérisateur plast grs"},
        description={"nl": "Demoplast Plantenspuit 1 l", "fr": "Demoplast Pulvérisateur 1 l"},
        message={
            "nl": "Bevochtig planten gelijkmatig met deze drukspuit van één liter.",
            "fr": "Humidifiez vos plantes uniformément avec ce pulvérisateur d'un litre.",
        },
        benefits={
            "nl": ("Verstelbare sproeikop", "Comfortabele hendel"),
            "fr": ("Buse réglable", "Poignée confortable"),
        },
        net_content=("1", "LTR"),
        weight=("240", "195"),
        dims=("300", "120", "110"),
        gpc="10003865",
        materials=("kunststof",),
        # The ranking case: neither market is complete, and the record is only whole because
        # resolution walks the priority list per language rather than picking one market per
        # product. Dutch comes from 528, French from 056.
        gaps={_NL: ("fr",), _BE: ("nl",)},
    ),
    DemoProduct(
        gtin=_gtin(6),
        name={"nl": "Afwasborstel", "fr": "Brosse à vaisselle"},
        logistics={"nl": "Afwasborstel kunststof grs", "fr": "Brosse vaisselle plast grs"},
        description={"nl": "Demoplast Afwasborstel", "fr": "Demoplast Brosse à vaisselle"},
        message={
            "nl": "Neem hardnekkige resten mee met de stevige vezels van deze afwasborstel.",
            "fr": "Venez à bout des résidus tenaces grâce aux fibres fermes de cette brosse.",
        },
        benefits={
            "nl": ("Stevige vezels", "Ophangoog"),
            "fr": ("Fibres fermes", "Anneau de suspension"),
        },
        net_content=("1", "H87"),
        weight=("95", "80"),
        dims=("280", "70", "40"),
        gpc="10005426",
        materials=("kunststof",),
    ),
    DemoProduct(
        gtin=_gtin(7),
        name={"nl": "Vergiet", "fr": "Passoire"},
        logistics={"nl": "Vergiet rvs 24cm grs", "fr": "Passoire inox 24cm grs"},
        description={"nl": "Demoplast Vergiet 24 cm", "fr": "Demoplast Passoire 24 cm"},
        message={
            "nl": "Laat groente en pasta snel uitlekken in dit vergiet van roestvrij staal.",
            "fr": "Égouttez rapidement légumes et pâtes dans cette passoire en inox.",
        },
        benefits={
            "nl": ("Roestvrij staal", "Stabiele voet", "Vaatwasserbestendig"),
            "fr": ("Acier inoxydable", "Base stable", "Va au lave-vaisselle"),
        },
        net_content=("1", "H87"),
        weight=("340", "310"),
        dims=("110", "240", "240"),
        gpc="10005426",
        materials=("roestvrij staal",),
        # A single referenced file, and it is not flagged primary. The hero still resolves,
        # via the PRODUCT_IMAGE fallback — the path that keeps a real feed's sloppy flagging
        # from costing a product its image.
        images=1,
    ),
    DemoProduct(
        gtin=_gtin(8),
        name={"nl": "Schuurspons"},
        logistics={"nl": "Schuurspons schuim grs"},
        description={"nl": "Demoplast Schuurspons"},
        message={"nl": "Verwijder aangekoekt vuil met de schurende zijde van deze spons."},
        benefits={"nl": ("Twee zijden", "Per drie verpakt")},
        net_content=("3", "H87"),
        weight=("70", "55"),
        dims=("95", "70", "30"),
        gpc="10005426",
        materials=("schuim",),
        # French absent from every market. Not a parse failure — the default language is what the
        # reader insists on — but the product is held until someone fills it in, and it is the
        # case the generator's translate rule exists for.
        gaps={_NL: ("fr",), _BE: ("fr",)},
    ),
    DemoProduct(
        gtin=_gtin(9),
        name={"nl": "Tuinhandschoen", "fr": "Gant de jardin"},
        logistics={"nl": "Tuinhandschoen latex mt9 grs", "fr": "Gant jardin latex t9 grs"},
        description={"nl": "Demoplast Tuinhandschoen maat 9", "fr": "Demoplast Gant de jardin"},
        message={
            "nl": "Werk beschermd in de tuin met deze handschoen met gecoate handpalm.",
            "fr": "Jardinez protégé avec ce gant à paume enduite.",
        },
        benefits={
            "nl": ("Gecoate handpalm", "Ademende rug"),
            "fr": ("Paume enduite", "Dos respirant"),
        },
        net_content=("1", "PR"),
        weight=("110", "90"),
        dims=("250", "120", "20"),
        gpc="10003865",
        # "zzzanders" is the junk value a real datapool uses for "other", and downstream treats it
        # as absent — per slot, so this product's material reads as "latex, katoen".
        materials=("latex", "zzzanders", "katoen"),
    ),
    DemoProduct(
        gtin=_gtin(10),
        name={"nl": "Deurmat", "fr": "Paillasson"},
        logistics={"nl": "Deurmat rubber grs", "fr": "Paillasson caoutchouc grs"},
        description={"nl": "Demoplast Deurmat 40x60", "fr": "Demoplast Paillasson 40x60"},
        message={
            "nl": "Houd vuil buiten met deze deurmat van stevig rubber.",
            "fr": "Gardez la saleté dehors avec ce paillasson en caoutchouc robuste.",
        },
        benefits={
            "nl": ("Stevig rubber", "Antislip"),
            "fr": ("Caoutchouc robuste", "Antidérapant"),
        },
        net_content=("1", "H87"),
        weight=("1850", "1750"),
        # No dimensions in any market: the mandatory-extras hold (E23) that #101 armed.
        dims=None,
        gpc="10006459",
        materials=("rubber",),
    ),
    DemoProduct(
        gtin=_gtin(11),
        name={"nl": "Wasknijper", "fr": "Pince à linge"},
        logistics={"nl": "Wasknijper hout 24st grs", "fr": "Pince linge bois 24p grs"},
        description={"nl": "Demoplast Wasknijper 24 stuks", "fr": "Demoplast Pince à linge 24"},
        message={
            "nl": "Hang de was op met deze knijpers van onbehandeld hout.",
            "fr": "Étendez le linge avec ces pinces en bois non traité.",
        },
        benefits={"nl": ("Onbehandeld hout", "24 stuks"), "fr": ("Bois non traité", "24 pièces")},
        net_content=("24", "H87"),
        weight=("300", "265"),
        dims=("72", "12", "10"),
        gpc="10005426",
        materials=("hout",),
        # No referenced file at all: the missing-hero-image hold (E22).
        images=0,
    ),
    DemoProduct(
        gtin=_gtin(12),
        name={"nl": "Zaklamp", "fr": "Lampe de poche"},
        logistics={"nl": "Zaklamp aluminium grs", "fr": "Lampe poche aluminium grs"},
        description={"nl": "Demoplast Zaklamp LED", "fr": "Demoplast Lampe de poche LED"},
        # No 1083 anywhere. The marketing_copy group is satisfied by 1067 alone, which is the
        # whole reason that group is declared either-or rather than two required fields.
        message={},
        benefits={
            "nl": ("LED met 200 lumen", "Aluminium behuizing", "Spatwaterdicht"),
            "fr": ("LED de 200 lumens", "Boîtier en aluminium", "Résistant aux éclaboussures"),
        },
        net_content=("1", "H87"),
        weight=("180", "150"),
        dims=("145", "35", "35"),
        gpc="10006459",
        materials=("aluminium",),
    ),
)

#: A barcode for the scope list that no export row carries. The Data screen has a table whose
#: only job is to show these, and a demo without one leaves that table — and the join it stands
#: for — out of the picture.
UNKNOWN_BARCODE: Final = _gtin(99)


# --- Sheet shapes -------------------------------------------------------------
#
# Only the five sheets ``democlient``'s gdsn_map reads are given attribute columns. The other
# nineteen carry their key columns and nothing else — on purpose. Populating them would mean
# inventing GDSN attribute numbers, and a fixture that states an attribute identity it cannot
# vouch for is worse than one that states none: this file is the nearest thing to a worked
# example of the format that the repository has, and someone will read it as one.

_DESCRIPTION: Final = ("TradeItemDescriptionInformation",)
_MARKETING: Final = ("MarketingInformation",)
_MEASURES: Final = ("TradeItemMeasurements",)
_BRICK: Final = ("Information[0]",)
_WEIGHT: Final = (*_MEASURES, "TradeItemWeight")

#: How many referenced-file groups the sheet has room for, whatever a product fills.
_FILE_SLOTS: Final = 3

#: The leaves of one ``ReferencedFileHeader[n]`` group, in the order the real export lists them.
#: The pipeline reads three of these — the URI, the type code and the primary flag — and the rest
#: are here because a group that carried only what we read would not be the group we resolve.
_FILE_LEAVES: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("QualificationDateTime",), "Qualification Date Time (50110)"),
    (("MediaQualificationStatus",), "Media qualification status (10002)"),
    (("MediaSourceGln",), "Media source GLN (50129)"),
    (("ReferencedFileDetail", "FilePixelHeight"), "File Pixel Height (2511)"),
    (("ReferencedFileDetail", "FilePixelWidth"), "File Pixel Width (2512)"),
    (("MimeType",), "Referenced File Header (2602)"),
    (("ReferencedFileTypeCode", "Value"), "Referenced file type code (2469)"),
    (("FileFormatName",), "Referenced File Header File Format Name (2479)"),
    (("FileName",), "File name (2481)"),
    (("UniformResourceIdentifier",), "Uniform resource identifier (2485)"),
    (("IsPrimaryFile",), "Is Primary File (4277)"),
)


def _description_columns() -> list[Column]:
    return [
        *localised_columns(
            _DESCRIPTION, "DescriptionShort", 3, *(("Short product name (3297)",) * 2)
        ),
        *localised_columns(_DESCRIPTION, "FunctionalName", 3, *(("Functional Name (3301)",) * 2)),
        *localised_columns(
            _DESCRIPTION, "TradeItemDescription", 3, *(("Product description (3318)",) * 2)
        ),
        Column((*_DESCRIPTION, "BrandNameInformation", "BrandName"), "Brand Name (3336)"),
        *localised_columns(
            _DESCRIPTION, "LabelDescription", 2, *(("Label Description (3309)",) * 2)
        ),
        *localised_columns(
            _DESCRIPTION, "VariantDescription", 2, *(("Product variation (3332)",) * 2)
        ),
        *localised_columns(
            _DESCRIPTION,
            "RegulatedProductName",
            2,
            "Regulated product name (3314)",
            "Regulated product name language code (3314)",
        ),
    ]


def _description_cells(product: DemoProduct, market: str) -> list[object]:
    carried = product.carried(market)
    return [
        *localised_cells(product.logistics, LANGUAGES, 3, carried),
        *localised_cells(product.names_in(market), LANGUAGES, 3, carried),
        *localised_cells(product.description, LANGUAGES, 3, carried),
        BRAND,
        *localised_cells({}, LANGUAGES, 2, carried),
        *localised_cells(product.variation, LANGUAGES, 2, carried),
        *localised_cells({}, LANGUAGES, 2, carried),
    ]


def _marketing_columns() -> list[Column]:
    return [
        *localised_columns(
            _MARKETING,
            "TradeItemMarketingMessage",
            3,
            "Product marketing message (1083)",
            "Language (1083)",
        ),
        *localised_columns(
            _MARKETING,
            "TradeItemFeatureBenefit",
            6,
            "Features and benefits (1067)",
            "Language (1067)",
        ),
        Column(
            (*_MARKETING, "Season[0]", "IsTradeItemSeasonal"), "Seasonal product indicator (1095)"
        ),
    ]


def _marketing_cells(product: DemoProduct, market: str) -> list[object]:
    carried = product.carried(market)
    return [
        *localised_cells(product.message, LANGUAGES, 3, carried),
        *multislot_cells(product.benefits, LANGUAGES, 6, carried),
        None,
    ]


def _measurement_sheet_columns() -> list[Column]:
    return [
        *measurement_columns(_MEASURES, "Depth", "Depth (3492)"),
        *measurement_columns(_MEASURES, "Height", "Height (3498)"),
        *measurement_columns(_MEASURES, "NetContent[0]", "Net Content (3510)"),
        *measurement_columns(_MEASURES, "Width", "Width (3520)"),
        *measurement_columns(_WEIGHT, "GrossWeight", "Gross weight (3556)"),
        *measurement_columns(_WEIGHT, "NetWeight", "Net Weight (3559)"),
    ]


def _measurement_sheet_cells(product: DemoProduct, market: str) -> list[object]:
    height, width, depth = product.dims or (None, None, None)
    content, content_unit = product.net_content
    gross, net = product.weight
    return [
        *measurement_cells(depth, _MM),
        *measurement_cells(height, _MM),
        *measurement_cells(content, content_unit),
        *measurement_cells(width, _MM),
        *measurement_cells(gross, "GRM"),
        *measurement_cells(net, "GRM"),
    ]


def _brick_columns() -> list[Column]:
    return [
        Column(("GpcCategoryCode",), "GPC classification category code (3122)"),
        # Language-agnostic: a bare ``Value`` leaf with no ``LanguageCode`` beside it. That is
        # what makes Material a scalar rather than a localised group, and why it is matched by
        # the path segment name instead of an attribute number.
        *(Column((*_BRICK, f"Material[{slot}]", "Value"), "Material (4.012)") for slot in range(3)),
        *localised_columns(_BRICK, "OtherMaterials", 2, *(("Other Materials (4.013)",) * 2)),
    ]


def _brick_cells(product: DemoProduct, market: str) -> list[object]:
    materials = list(product.materials)[:3]
    return [
        product.gpc,
        *materials,
        *[None] * (3 - len(materials)),
        *localised_cells({}, LANGUAGES, 2, product.carried(market)),
    ]


def _file_columns() -> list[Column]:
    return [
        Column((f"ReferencedFileHeader[{slot}]", *leaf), label)
        for slot in range(_FILE_SLOTS)
        for leaf, label in _FILE_LEAVES
    ]


def _file_group(product: DemoProduct, slot: int) -> list[object]:
    """One referenced-file group: a web-served PNG, flagged primary only on the second slot."""
    if slot >= product.images:
        return [None] * len(_FILE_LEAVES)
    primary = slot == 1
    size = 1200 if primary else 400
    suffix = "A1N0" if primary else "A1L1"
    return [
        _QUALIFIED,
        "ValidByMedia",
        _GLN,
        size,
        size,
        "image/png",
        "PRODUCT_IMAGE",
        "Png",
        f"{product.gtin}_{suffix}.png",
        f"{_IMAGE_HOST}/{size}x{size}/png?text={product.gtin}",
        "TRUE" if primary else "FALSE",
    ]


def _file_cells(product: DemoProduct, market: str) -> list[object]:
    return [cell for slot in range(_FILE_SLOTS) for cell in _file_group(product, slot)]


#: One product-row's attribute cells, for one market. Every sheet builder has this shape so that
#: assembly does not need to know which sheet it is filling.
_Cells = Callable[[DemoProduct, str], list[object]]


def _no_cells(product: DemoProduct, market: str) -> list[object]:
    """A key-only sheet's attribute cells: there are none."""
    return []


# --- Assembly -----------------------------------------------------------------

#: Sheet name → (its attribute columns, one product-row's attribute cells). Absent from this map
#: means a key-only sheet; see the note above :data:`_DESCRIPTION`.
_SHEETS: Final[Mapping[str, tuple[Callable[[], list[Column]], _Cells]]] = {
    "TradeItemDescription": (_description_columns, _description_cells),
    "MarketingInformation": (_marketing_columns, _marketing_cells),
    "TradeItemMeasurements": (_measurement_sheet_columns, _measurement_sheet_cells),
    "BrickGPCCommercialData": (_brick_columns, _brick_cells),
    "ReferencedFileDetailInformation": (_file_columns, _file_cells),
}

#: The case-level trade item, which every sheet carries a row for and the reader must skip. It
#: has a **GTIN of its own**, as a real case does — which is what makes the skip testable: were
#: it ever to stop happening, this GTIN would surface as a product with no name and the parse
#: would fail loudly, rather than quietly mixing case values into a consumer record.
CASE_GTIN: Final = _gtin(50)

#: Where the deliberately empty row goes, so it is not the last row on the sheet — openpyxl trims
#: trailing blanks on save, and an E4 fixture that the writer removes tests nothing.
_BLANK_ROW_AFTER: Final = 2


def sheet_grids() -> dict[str, list[list[object]]]:
    """The whole workbook as cell grids: sheet name → rows, header rows first.

    Every sheet gets every product's rows, one per market the product declares — the join key is
    (GTIN, market), and a sheet that held a different set of products from its neighbours would
    be a different fixture than the one the reader is written against.
    """
    grids: dict[str, list[list[object]]] = {}
    for module in MODULES:
        spec = _SHEETS.get(module)
        columns = [*KEY_COLUMNS, *(spec[0]() if spec else [])]
        cells: _Cells = spec[1] if spec else _no_cells
        rows = header_rows(columns)
        for index, product in enumerate(CATALOGUE):
            rows.extend(
                [product.gtin, market, _GLN, CONSUMER_UNIT, *cells(product, market)]
                for market in product.markets
            )
            if index == _BLANK_ROW_AFTER:
                rows.append([None] * len(columns))  # E4: an empty row, skipped silently
        rows.append([CASE_GTIN, _NL, _GLN, _CASE_UNIT, *[None] * (len(columns) - len(KEY_COLUMNS))])
        grids[module] = rows
    return grids


def process_list_rows() -> list[list[object]]:
    """The product scope list: the header row the config names a column in, then one row each.

    Carries the whole catalogue plus :data:`UNKNOWN_BARCODE`, and a ``Notes`` column because a
    real one does — the operator prunes this file by deleting rows, and their reasons live beside
    the barcodes. Nothing reads any column but the first.
    """
    rows: list[list[object]] = [["Barcode", "Product", "Notes"]]
    rows.extend([product.gtin, product.name["nl"], None] for product in CATALOGUE)
    rows.append([UNKNOWN_BARCODE, "Onbekend artikel", "Not in the export — joins to nothing"])
    return rows
