import pytest
from unittest.mock import MagicMock, patch
from src.fast_geoip import FastIPLocator

# 1. FIXTURE: Mockiamo la libreria maxminddb per non usare il file reale
@pytest.fixture
def mock_reader():
    # Intercettiamo la chiamata a open_database ovunque avvenga in fast_geoip
    with patch('src.fast_geoip.maxminddb.open_database') as mock_open:
        mock_db = MagicMock()
        mock_open.return_value = mock_db
        yield mock_db

# 2. TEST: Comportamento in caso di file DB mancante (nessun mock qui)
def test_init_file_not_found():
    # Diamo un nome file palesemente falso
    with pytest.raises(FileNotFoundError):
        FastIPLocator(db_filename="percorso_inesistente.mmdb")

# 3. TEST: IP trovato con tutti i dati (Nazione, Città, Coordinate)
def test_get_geo_data_success(mock_reader):
    # Simuliamo la risposta del DB per un IP ideale
    mock_reader.get.return_value = {
        'country': {'iso_code': 'IT'},
        'city': {'names': {'en': 'Rome'}},
        'location': {'latitude': 41.9028, 'longitude': 12.4964}
    }
    
    locator = FastIPLocator("dummy.mmdb") # Il nome non conta, è mockato
    result = locator.get_geo_data("8.8.8.8")
    
    assert result is not None
    assert result['country_iso'] == 'IT'
    assert result['city_name'] == 'Rome'
    assert result['latitude'] == 41.9028

# 4. TEST: IP trovato, ma dati sparsi (es. manca la città e le coordinate)
def test_get_geo_data_sparse_data(mock_reader):
    # Simuliamo la risposta del DB per un IP incompleto
    mock_reader.get.return_value = {
        'country': {'iso_code': 'US'}
    }
    
    locator = FastIPLocator("dummy.mmdb")
    result = locator.get_geo_data("1.1.1.1")
    
    # Il codice deve sopravvivere senza lanciare KeyError
    assert result is not None
    assert result['country_iso'] == 'US'
    assert result['city_name'] is None
    assert result['latitude'] is None

# 5. TEST: IP non trovato nel DB
def test_get_geo_data_ip_not_found(mock_reader):
    # maxminddb restituisce None se l'IP non c'è
    mock_reader.get.return_value = None
    
    locator = FastIPLocator("dummy.mmdb")
    result = locator.get_geo_data("255.255.255.255")
    
    assert result is None