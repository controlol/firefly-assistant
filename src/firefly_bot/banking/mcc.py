"""Map Merchant Category Codes (MCC) to Firefly categories.

MCC is a definitive, card-network-assigned signal (requirement: categorise only where a
category can be determined for sure), so this is a high-precision categoriser for card/POS
payments. Categories are Dutch and created on the fly by Firefly when first used.
"""

from __future__ import annotations

# Code (4 digits) -> category name. Curated for common NL consumer spending.
MCC_CATEGORIES: dict[str, str] = {
    # Groceries
    "5411": "Boodschappen", "5412": "Boodschappen", "5422": "Boodschappen",
    "5441": "Boodschappen", "5451": "Boodschappen", "5462": "Boodschappen",
    "5499": "Boodschappen",
    # Fuel
    "5172": "Brandstof", "5541": "Brandstof", "5542": "Brandstof", "5983": "Brandstof",
    # Dining
    "5811": "Uit eten", "5812": "Uit eten", "5813": "Uit eten", "5814": "Uit eten",
    # General retail
    "5300": "Winkels", "5310": "Winkels", "5311": "Winkels", "5331": "Winkels",
    "5399": "Winkels", "5999": "Winkels",
    # Clothing & shoes
    "5611": "Kleding", "5621": "Kleding", "5631": "Kleding", "5641": "Kleding",
    "5651": "Kleding", "5655": "Kleding", "5661": "Kleding", "5691": "Kleding",
    "5699": "Kleding",
    # Home & DIY
    "5200": "Doe-het-zelf", "5211": "Doe-het-zelf", "5231": "Doe-het-zelf",
    "5251": "Doe-het-zelf", "5712": "Wonen", "5719": "Wonen", "5722": "Wonen",
    # Electronics & media
    "5045": "Elektronica", "5732": "Elektronica", "5734": "Elektronica",
    "5192": "Boeken & media", "5942": "Boeken & media", "5815": "Digitaal",
    "5816": "Digitaal", "5817": "Digitaal", "5818": "Digitaal",
    # Leisure
    "7832": "Vrije tijd", "7841": "Vrije tijd", "7922": "Vrije tijd",
    "7991": "Vrije tijd", "7996": "Vrije tijd", "7997": "Vrije tijd", "7998": "Vrije tijd",
    # Travel & transport
    "4111": "Vervoer", "4112": "Vervoer", "4121": "Vervoer", "4131": "Vervoer",
    "4789": "Vervoer", "7512": "Vervoer", "7523": "Vervoer", "7011": "Reizen",
    "4511": "Reizen",
    # Health & personal care
    "5912": "Gezondheid", "8011": "Gezondheid", "8021": "Gezondheid",
    "8042": "Gezondheid", "8043": "Gezondheid", "8062": "Gezondheid",
    "5977": "Persoonlijke verzorging", "7230": "Persoonlijke verzorging",
    "7298": "Persoonlijke verzorging",
    # Bills & utilities
    "4814": "Telefonie & internet", "4899": "Telefonie & internet",
    "4900": "Nutsvoorzieningen",
}


def category_for_mcc(mcc: str | None) -> str | None:
    return MCC_CATEGORIES.get(mcc) if mcc else None
