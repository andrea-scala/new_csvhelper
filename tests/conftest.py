import sys
from pathlib import Path
import pytest

# Trova la radice del progetto (new_csvhelper) e la aggiunge al path di Python
root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import app as flask_project

@pytest.fixture
def client():
    """Inizializza il client di test di Flask."""
    # Se nel tuo file app.py hai scritto 'app = Flask(__name__)', usiamo flask_project.app
    with flask_project.app.test_client() as test_client:
        yield test_client
