from __future__ import annotations

CATEGORY_TAXONOMY = {
    "womens_apparel": {
        "label": "Women\'s Apparel",
        "price": (220, 2400),
        "genders": ["women"],
        "materials": ["silk", "cashmere", "cotton", "wool", "linen", "satin"],
        "items": ["Dress", "Blazer", "Trousers", "Gown", "Blouse", "Skirt"],
    },
    "shoes": {
        "label": "Designer Shoes",
        "price": (180, 1900),
        "genders": ["women", "men"],
        "materials": ["leather", "suede", "canvas", "patent leather"],
        "items": ["Pump", "Sandal", "Boot", "Sneaker", "Loafer"],
    },
    "handbags": {
        "label": "Handbags",
        "price": (350, 5200),
        "genders": ["women"],
        "materials": ["leather", "canvas", "raffia", "quilted leather"],
        "items": ["Tote", "Clutch", "Crossbody", "Shoulder Bag", "Top Handle"],
    },
    "beauty": {
        "label": "Beauty",
        "price": (45, 420),
        "genders": ["women", "men", "unisex"],
        "materials": ["botanical blend", "vitamin complex", "mineral formula"],
        "items": ["Fragrance", "Serum", "Moisturizer", "Lip Color", "Palette"],
    },
    "mens_apparel": {
        "label": "Men\'s Apparel",
        "price": (150, 3200),
        "genders": ["men"],
        "materials": ["wool", "cashmere", "cotton", "linen", "denim"],
        "items": ["Sport Coat", "Shirt", "Trouser", "Denim", "Jacket"],
    },
    "kids": {
        "label": "Kids",
        "price": (60, 520),
        "genders": ["girls", "boys", "unisex"],
        "materials": ["cotton", "jersey", "fleece", "knit"],
        "items": ["Dress", "Set", "Sneaker", "Jacket", "Pajamas"],
    },
    "home": {
        "label": "Home",
        "price": (80, 2400),
        "genders": ["unisex"],
        "materials": ["porcelain", "linen", "glass", "wood", "metal"],
        "items": ["Throw", "Dinnerware Set", "Candle", "Accent Tray", "Pillow"],
    },
    "jewelry_accessories": {
        "label": "Jewelry & Accessories",
        "price": (120, 6800),
        "genders": ["women", "men", "unisex"],
        "materials": ["gold", "silver", "diamond", "plated brass", "pearl"],
        "items": ["Necklace", "Bracelet", "Earrings", "Watch", "Belt"],
    },
}

REAL_BRANDS = [
    "Saint Laurent",
    "Bottega Veneta",
    "Christian Louboutin",
    "Brunello Cucinelli",
    "Valentino",
    "Jimmy Choo",
    "Moncler",
    "Akris",
    "Tom Ford",
    "Balmain",
    "Chloe",
    "Etro",
]

SYNTHETIC_BRANDS = [
    "Atelier Veridian",
    "Maison Arctis",
    "Dorian Vale",
    "Lune & Ledger",
    "Noir Harbor",
    "Calder Row",
    "Solenne Studio",
    "Northline Atelier",
    "August & Mercer",
    "Riviera Foundry",
]

STORE_ASSORTMENT_PROFILES = {
    "flagship_urban": {
        "womens_apparel": 0.22,
        "mens_apparel": 0.21,
        "shoes": 0.17,
        "handbags": 0.13,
        "beauty": 0.07,
        "jewelry_accessories": 0.13,
        "home": 0.04,
        "kids": 0.03,
    },
    "resort_luxury": {
        "womens_apparel": 0.24,
        "mens_apparel": 0.10,
        "shoes": 0.16,
        "handbags": 0.17,
        "beauty": 0.14,
        "jewelry_accessories": 0.11,
        "home": 0.04,
        "kids": 0.04,
    },
    "texas_core": {
        "womens_apparel": 0.25,
        "mens_apparel": 0.18,
        "shoes": 0.16,
        "handbags": 0.13,
        "beauty": 0.08,
        "jewelry_accessories": 0.10,
        "home": 0.06,
        "kids": 0.04,
    },
    "suburban_affluent": {
        "womens_apparel": 0.23,
        "mens_apparel": 0.14,
        "shoes": 0.17,
        "handbags": 0.15,
        "beauty": 0.09,
        "jewelry_accessories": 0.09,
        "home": 0.06,
        "kids": 0.07,
    },
}

OCCASION_TO_CATEGORY = {
    "wedding": ["womens_apparel", "shoes", "jewelry_accessories", "handbags"],
    "vacation": ["womens_apparel", "mens_apparel", "beauty", "handbags"],
    "workwear": ["womens_apparel", "mens_apparel", "shoes", "jewelry_accessories"],
    "holiday_party": ["womens_apparel", "mens_apparel", "beauty", "home"],
    "everyday_luxury": ["womens_apparel", "mens_apparel", "beauty", "home", "kids"],
}

KNOWN_COLORS = [
    "Black",
    "Ivory",
    "Camel",
    "Navy",
    "Sage",
    "Rose",
    "Burgundy",
    "Chocolate",
    "Silver",
    "Gold",
]

KNOWN_SIZES = ["XS", "S", "M", "L", "XL", "6", "8", "10", "12", "One Size"]
KNOWN_SEASONS = ["spring", "summer", "fall", "winter", "all-season"]
