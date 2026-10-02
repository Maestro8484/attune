"""Load catalog ids (recording, artists, original year, style), artist similarity and the genre
family map into an Attune library database. Backs the database up first. See src/enrich.py for what
the engine does with them.

  python tools/import_catalog.py --db ..\\mixer-ng\\data\\mixer.db --ids attune_export.csv --similar artist_similarity.csv --families genre_families.csv
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import enrich  # noqa: E402

if __name__ == "__main__":
    sys.exit(enrich.import_main())
