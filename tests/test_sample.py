from pathlib import Path

# Recupera la cartella delle risorse
resources = Path(__file__).parent / "res"

def test_edit_user(client): # <--- La fixture viene passata qui
    with (resources / "starting_csv.csv").open("rb") as csv_file:
        response = client.post("/upload", data={
            "starting_csv": csv_file,
        })
    
    assert response.status_code == 200
