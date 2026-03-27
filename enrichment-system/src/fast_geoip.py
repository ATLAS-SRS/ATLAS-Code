import argparse
import os
import sys
import maxminddb
from structured_logger import get_logger


logger = get_logger("enrichment-system-geoip")

class FastIPLocator:
    def __init__(self, db_filename='GeoLite2-City.mmdb'):
        """
        Inizializza il localizzatore aprendo il file MMDB.
        """

        base_dir = os.path.dirname(os.path.abspath(__file__))
        # Sali di un livello (da /app/src a /app) e poi entra in data
        project_root = os.path.dirname(base_dir) 
        db_path = os.path.join(project_root, 'data', db_filename)

        try:
            self.reader = maxminddb.open_database(db_path)
        except FileNotFoundError:
            raise FileNotFoundError(f"Database non trovato in {db_path}.")

    def locate(self, ip_address):
        """
        Geolocalizza l'IP. Ritorna i dati grezzi o None.
        """
        try:
            return self.reader.get(ip_address)
        except ValueError:
            return None

    def get_geo_data(self, ip_address):
        """
        Estrae Nazione, Città e Coordinate gestendo la sparsità dei dati del DB City.
        Ritorna un dizionario normalizzato o None.
        """
        raw = self.locate(ip_address)
        if not raw:
            return None

        # Uso intensivo di .get() con fallback a dict vuoto {} 
        # per navigare l'albero in modo sicuro senza try/except costosi
        return {
            'country_iso': raw.get('country', {}).get('iso_code'),
            'city_name': raw.get('city', {}).get('names', {}).get('en'),
            'latitude': raw.get('location', {}).get('latitude'),
            'longitude': raw.get('location', {}).get('longitude')
        }

    def close(self):
        self.reader.close()

def main():
    parser = argparse.ArgumentParser(description="Geolocalizza un IP ad alta velocità (City DB).")
    parser.add_argument("ip", help="Indirizzo IP da geolocalizzare")
    parser.add_argument("--db", default="GeoLite2-City.mmdb", help="Percorso al database MMDB")
    
    args = parser.parse_args()

    try:
        locator = FastIPLocator(args.db)
    except FileNotFoundError as e:
        logger.error("GeoIP database file not found", extra={"error": str(e), "database": args.db})
        sys.exit(1)

    result = locator.get_geo_data(args.ip)
    locator.close()

    if result:
        logger.info(
            "GeoIP lookup succeeded",
            extra={
                "ip": args.ip,
                "country_iso": result["country_iso"],
                "city_name": result["city_name"],
                "latitude": result["latitude"],
                "longitude": result["longitude"],
            },
        )
    else:
        logger.error("IP address not found or invalid", extra={"ip": args.ip})
        sys.exit(1)

if __name__ == "__main__":
    main()