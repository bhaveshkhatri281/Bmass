"""Default database seeding: steps, languages, country settings, category phrases, and templates."""

from __future__ import annotations

import sqlite3
from typing import Optional

from mailer.db import init_db, open_conn

# delay_days counts from the previous step, so 0, 3, 4 means day 0, day 3, day 7.
DEFAULT_STEPS = [
    (1, 0, 1),
    (2, 3, 1),
    (3, 4, 1),
]

DEFAULT_LANGUAGES = [
    (
        "en",
        "English",
        "{{sender_name}} · {{us_address}}\nReply \"stop\" to opt out.",
    ),
    (
        "fr",
        "French",
        "{{sender_name}} · {{us_address}}\nRépondez « stop » pour ne plus recevoir nos e-mails.",
    ),
]

DEFAULT_COUNTRIES = [
    ("United States", "en", "America/New_York", 0, 1),
    ("United Kingdom", "en", "Europe/London", 0, 2),
    ("Canada", "en", "America/Toronto", 0, 3),
    ("Australia", "en", "Australia/Sydney", 0, 4),
    ("Ireland", "en", "Europe/Dublin", 0, 5),
    ("Netherlands", "en", "Europe/Amsterdam", 0, 6),
    ("Sweden", "en", "Europe/Stockholm", 0, 7),
    ("Denmark", "en", "Europe/Copenhagen", 0, 8),
    ("Norway", "en", "Europe/Oslo", 0, 9),
    ("Finland", "en", "Europe/Helsinki", 0, 10),
    ("Iceland", "en", "Atlantic/Reykjavik", 0, 11),
    ("Switzerland", "en", "Europe/Zurich", 0, 12),
    ("Belgium", "fr", "Europe/Brussels", 0, 13),
    ("Spain", "en", "Europe/Madrid", 0, 14),
    ("Italy", "en", "Europe/Rome", 0, 15),
    ("Portugal", "en", "Europe/Lisbon", 0, 16),
    ("Poland", "en", "Europe/Warsaw", 0, 17),
    ("Austria", "en", "Europe/Vienna", 0, 18),
    ("Czechia", "en", "Europe/Prague", 0, 19),
    ("Slovakia", "en", "Europe/Bratislava", 0, 20),
    ("Hungary", "en", "Europe/Budapest", 0, 21),
    ("Greece", "en", "Europe/Athens", 0, 22),
    ("Cyprus", "en", "Asia/Nicosia", 0, 23),
    ("Romania", "en", "Europe/Bucharest", 0, 24),
    ("Bulgaria", "en", "Europe/Sofia", 0, 25),
    ("Croatia", "en", "Europe/Zagreb", 0, 26),
    ("Serbia", "en", "Europe/Belgrade", 0, 27),
    ("Estonia", "en", "Europe/Tallinn", 0, 28),
    ("Latvia", "en", "Europe/Riga", 0, 29),
    ("Lithuania", "en", "Europe/Vilnius", 0, 30),
    ("Ukraine", "en", "Europe/Kyiv", 0, 31),
    ("Moldova", "en", "Europe/Chisinau", 0, 32),
    ("Monaco", "fr", "Europe/Monaco", 0, 33),
    ("France", "fr", "Europe/Paris", 0, 40),
    ("Germany", "en", "Europe/Berlin", 1, 99),
]

CATEGORY_PHRASE_GROUPS = [
    (["food & beverage"], "food & drink", "alimentaire"),
    (["beauty & personal care"], "beauty", "beauté"),
    (["fashion & accessories"], "fashion", "mode"),
    (["home & lifestyle"], "home & lifestyle", "maison & déco"),
    (["gadgets & practical accessories"], "gadget", "accessoires"),
    (["fitness, sport & outdoor"], "fitness", "sport"),
    (["skincare"], "skincare", "soin de la peau"),
    (["haircare", "haircare & grooming", "hair care"], "haircare", "soin capillaire"),
    (
        [
            "bath & body care",
            "bath, body & personal care",
            "skin & body care",
            "body & personal care",
            "soap, bath & body",
        ],
        "body care",
        "soin du corps",
    ),
    (["fragrance"], "fragrance", "parfum"),
    (
        ["makeup", "cosmetics & beauty products", "makeup & beauty tools"],
        "makeup",
        "maquillage",
    ),
    (["nails", "nail care"], "nail", "ongles"),
    (["oral care"], "oral care", "hygiène bucco-dentaire"),
    # Added from the raw categories in Mobisa's merged lead file. Multi-part
    # categories use their first or main product.
    (["multi-category beauty", "beauty mirrors & lighting"], "beauty", "beauté"),
    (["beauty devices"], "beauty device", "appareils de beauté"),
    (
        [
            "skin care",
            "skin care & beauty devices",
            "botanical skincare",
            "outdoor skincare",
            "personalized skincare",
            "tallow skincare",
            "tallow skincare & body care",
            "tallow skincare & deodorant",
            "children’s & teen skincare",
            "teen skincare & personal care",
            "skincare & aromatherapy",
            "skincare & beauty devices",
            "skincare & body care",
            "skincare & body oils",
            "skincare & facial devices",
            "skincare & fragrance",
            "skincare & haircare",
            "skincare & lip care",
            "skincare & personal care",
            "skincare & personal fragrance",
            "skincare & self-tan",
            "skincare & soap",
            "skincare & topical body care",
            "skincare, body care & haircare",
            "skincare, body care & lip care",
            "skincare, haircare & bath",
            "skincare, haircare & body care",
            "skincare, haircare & grooming",
            "skincare, lash & nail products",
            "skincare, makeup & haircare",
            "skincare, soap & bath",
        ],
        "skincare",
        "soin de la peau",
    ),
    (
        [
            "body care",
            "body care & aromatherapy",
            "body care & bath",
            "body care & deodorant",
            "body care & fragrance",
            "body care & haircare",
            "body care & skincare",
            "body care & spa products",
            "body scrubs & body care",
            "body wipes & personal care",
            "botanical body care",
            "goat milk body care",
            "herbal body care",
            "massage oils & body care",
            "bath salts & body care",
            "bath, body & fragrance",
            "hand sanitizer & body care",
        ],
        "body care",
        "soin du corps",
    ),
    (["bath products"], "bath", "bain"),
    (["aromatherapy & body care"], "aromatherapy", "aromathérapie"),
    (
        [
            "soap",
            "soap & aromatherapy",
            "soap & bath",
            "soap & bath gifts",
            "soap & bath products",
            "soap & body care",
            "soap & botanical skincare",
            "soap & grooming",
            "soap & lip care",
            "soap & men’s grooming",
            "soap & skincare",
            "soap, bath & body care",
            "soap, skincare & bath",
            "soap, skincare & fragrance",
            "botanical soap & body care",
            "fragrance-free soap",
            "goat milk soap & deodorant",
            "goat milk soap & skincare",
            "handmade soap & skincare",
        ],
        "soap",
        "savon",
    ),
    (
        [
            "deodorant",
            "deodorant & body care",
            "deodorant & skincare",
            "deodorant, body care & fragrance",
            "natural deodorant & body care",
        ],
        "deodorant",
        "déodorant",
    ),
    (
        [
            "fragrance & aromatherapy",
            "fragrance & body care",
            "fragrance & skincare",
            "natural perfume",
            "perfume & fragrance",
        ],
        "fragrance",
        "parfum",
    ),
    (
        [
            "curly haircare",
            "hair color & haircare",
            "haircare & beauty accessories",
            "haircare & body care",
            "haircare & hair accessories",
            "haircare & hair colour",
            "haircare & hair cosmetics",
            "haircare & lash care",
            "haircare & scalp care",
            "haircare & skincare",
            "haircare & styling tools",
            "haircare for locs",
            "haircare, skincare & body care",
            "haircare, soap & body care",
            "haircare, soap & deodorant",
            "haircare, soap & skincare",
        ],
        "haircare",
        "soin capillaire",
    ),
    (
        [
            "hair styling & grooming",
            "hair styling & grooming tools",
            "hair styling tools",
        ],
        "hair styling",
        "coiffure",
    ),
    (
        [
            "hair accessories & grooming tools",
            "hair brushes & grooming",
        ],
        "hair accessory",
        "accessoires pour cheveux",
    ),
    (
        [
            "hair extensions",
            "hair extensions & haircare",
            "hair extensions & wigs",
            "wigs & hair extensions",
            "wigs & hairpieces",
        ],
        "hair extension",
        "extensions capillaires",
    ),
    (
        [
            "beard care & men’s grooming",
            "men's grooming & personal care",
            "men's personal care",
            "men's skincare",
            "men’s grooming",
            "men’s skincare",
            "men’s skincare & grooming",
            "men’s skincare & makeup",
        ],
        "men's grooming",
        "soin pour hommes",
    ),
    (
        [
            "grooming & shaving",
            "grooming & shaving tools",
            "shaving & body care",
            "shaving & hair removal",
            "shaving soap & men’s grooming",
            "shaving tools",
        ],
        "shaving",
        "rasage",
    ),
    (
        [
            "makeup & body glitter",
            "makeup & cosmetics",
            "makeup & hair colour",
            "makeup & lip care",
            "makeup & nail care",
            "makeup & skincare",
            "makeup, skincare & fragrance",
        ],
        "makeup",
        "maquillage",
    ),
    (
        [
            "lip care",
            "lip & body care",
            "lip balm & personal care",
            "lip cosmetics & lip care",
            "spf lip care",
        ],
        "lip care",
        "soin des lèvres",
    ),
    (
        [
            "false eyelashes & beauty tools",
            "lash & brow care",
            "lash & brow cosmetics",
            "lash & brow products",
            "lash products",
            "lash, brow & haircare",
            "lash, nail & self-tan products",
        ],
        "lash",
        "cils",
    ),
    (["brow cosmetics", "brow products & beauty tools"], "brow", "sourcils"),
    (
        [
            "nail care & gel polish",
            "nail care & nail color",
            "nail polish",
            "nail polish & cosmetics",
            "nail polish & nail care",
            "nail polish & nailcare",
            "nail polish & press-on nails",
            "press-on nails",
            "press-on nails & lash products",
        ],
        "nail",
        "ongles",
    ),
    (
        [
            "oral & personal care",
            "oral care & deodorant",
            "oral care & personal care",
        ],
        "oral care",
        "hygiène bucco-dentaire",
    ),
    (
        [
            "baby & family body care",
            "baby & maternal body care",
            "baby & maternal personal care",
            "baby skin care",
            "baby skincare & haircare",
            "children’s bath & body care",
            "maternal & baby body care",
            "maternal & baby skincare",
        ],
        "baby care",
        "soin bébé",
    ),
    (
        [
            "mineral sunscreen",
            "sun & skin protection",
            "sun care",
            "suncare & skincare",
            "sunscreen",
            "sunscreen & skincare",
        ],
        "sun care",
        "soin solaire",
    ),
    (
        [
            "self-tan",
            "self-tan & cosmetics",
            "self-tan & skincare",
            "sunless tanning",
            "tanning",
        ],
        "self-tan",
        "autobronzant",
    ),
    (["tattoo aftercare"], "tattoo aftercare", "soin du tatouage"),
    (["temporary tattoos & body art"], "temporary tattoo", "tatouages temporaires"),
]

DEFAULT_TEMPLATES = [
    (
        1,
        "en",
        "{quick question|idea} for {{business_name}}",
        "Hi {{business_name}} team,\n\n"
        "{{custom_line}}Who handles your Meta and TikTok ads? I make AI UGC video ads for {{category}} brands: "
        "fresh hooks and angles to test, at a fraction of creator costs.\n\n"
        "Want me to send a 15-second sample made for {{business_name}}?",
    ),
    (
        2,
        "en",
        "",
        "Quick bump on this. Happy to make the sample, it takes 30 seconds to watch.",
    ),
    (
        3,
        "en",
        "",
        "Last note from me. Here is some of my work: {{portfolio_link}}\n"
        "If ads are not your area, could you forward this to whoever runs them?",
    ),
    (
        1,
        "fr",
        "{petite question|une idée} pour {{business_name}}",
        "Bonjour l'équipe {{business_name}},\n\n"
        "{{custom_line}}Qui s'occupe de vos publicités Meta et TikTok ? Je crée des vidéos publicitaires UGC par IA pour les marques {{category}} : "
        "de nouvelles accroches à tester, pour une fraction du coût des créateurs.\n\n"
        "Je vous envoie un exemple de 15 secondes fait pour {{business_name}} ?",
    ),
    (
        2,
        "fr",
        "",
        "Je me permets de relancer. L'exemple ne prend que 30 secondes à regarder.",
    ),
    (
        3,
        "fr",
        "",
        "Dernier message de ma part. Quelques réalisations : {{portfolio_link}}\n"
        "Si la publicité n'est pas votre domaine, pourriez-vous transférer ce message à la bonne personne ?",
    ),
]


def seed_database(conn: Optional[sqlite3.Connection] = None) -> None:
    """Insert seed rows only if the corresponding tables are empty."""
    def _seed(c: sqlite3.Connection) -> None:
        # Steps
        count = c.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
        if count == 0:
            c.executemany(
                "INSERT INTO steps (step_no, delay_days, active) VALUES (?, ?, ?)",
                DEFAULT_STEPS,
            )

        # Languages
        count = c.execute("SELECT COUNT(*) FROM languages").fetchone()[0]
        if count == 0:
            c.executemany(
                "INSERT INTO languages (code, name, footer_text) VALUES (?, ?, ?)",
                DEFAULT_LANGUAGES,
            )

        # Country settings
        count = c.execute("SELECT COUNT(*) FROM country_settings").fetchone()[0]
        if count == 0:
            c.executemany(
                "INSERT INTO country_settings (country, language, timezone, skip, sort_order) VALUES (?, ?, ?, ?, ?)",
                DEFAULT_COUNTRIES,
            )

        # Category phrases
        count = c.execute("SELECT COUNT(*) FROM category_phrases").fetchone()[0]
        if count == 0:
            flattened = []
            for keys, en, fr in CATEGORY_PHRASE_GROUPS:
                for k in keys:
                    flattened.append((k.lower().strip(), en, fr))
            c.executemany(
                "INSERT OR IGNORE INTO category_phrases (raw, phrase_en, phrase_fr) VALUES (?, ?, ?)",
                flattened,
            )

        # Templates
        count = c.execute("SELECT COUNT(*) FROM templates").fetchone()[0]
        if count == 0:
            c.executemany(
                "INSERT INTO templates (step_no, language, subject, body) VALUES (?, ?, ?, ?)",
                DEFAULT_TEMPLATES,
            )

        c.commit()

    if conn is not None:
        _seed(conn)
    else:
        init_db()
        with open_conn() as c:
            _seed(c)


if __name__ == "__main__":
    seed_database()
    print("Database seeded successfully.")

