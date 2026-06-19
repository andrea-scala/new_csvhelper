import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, Response, request, jsonify, send_file, render_template
from csvapi import CSVApi
import uuid
import threading
import zipfile
import time
app = Flask(__name__)

STATO_PROCESSI = {}
LOG_PROCESSI = {}


def allowed_file(filename, ALLOWED={'csv'}):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED


def aggiungi_log(task_id, testo):
    LOG_PROCESSI[task_id].append(testo)


def aggiorna_stato(task_id, stato, zip=None):
    STATO_PROCESSI[task_id]["stato"] = stato
    if zip:
        STATO_PROCESSI[task_id]["zip"] = zip


def rimuovi_csv_temporaneo(path):
    if os.path.exists(path):
        os.remove(path)


def pipeline(task_id, percorso_csv, sep="|"):  # ← aggiunto sep
    """
    Esegue la pipeline CSVApi in background:
      Step 1 - Pulizia catalogo
      Step 2 - Costruzione prodotti base
      Step 3 - Costruzione categorie
      Step 4 - Costruzione combinazioni
      Step 5 - Salvataggio su disco e creazione ZIP
    Al termine aggiorna lo stato e imposta il percorso dello ZIP.
    """
    try:
        output_dir = f"/tmp/{task_id}_export"

        aggiungi_log(task_id, "Inizializzazione CSVApi...")
        api = CSVApi(source=percorso_csv, sep="|", output_dir=output_dir)

        # --- Step 1: pulizia catalogo ---
        aggiungi_log(task_id, "STEP:PULIZIA")  # ← sentinella stepper
        aggiungi_log(task_id, "Step 1 — Pulizia catalogo in corso...")
        api.source["Descrizione"]     = api._clean_description(api.source["Descrizione"])
        api.source["Caratteristiche"] = api._clean_features(api.source["Caratteristiche"])
        api._add_tax()
        api._add_discount_amount()
        api._fix_duplicate_references()
        time.sleep(2)
        aggiungi_log(task_id, "Step 1 — Pulizia completata.")

        # --- Step 2: prodotti base ---
        aggiungi_log(task_id, "STEP:ESTRAZIONE")  # ← sentinella stepper
        aggiungi_log(task_id, "Step 2 — Costruzione prodotti base...")
        df_prodotti = api._build_products()
        time.sleep(2)
        aggiungi_log(task_id, f"Step 2 — {len(df_prodotti)} prodotti base costruiti.")


        # --- Step 3: categorie ---
        aggiungi_log(task_id, "Step 3 — Costruzione categorie...")
        df_categorie = api._build_categories()
        time.sleep(2)
        aggiungi_log(task_id, f"Step 3 — {len(df_categorie)} categorie costruite.")


        # --- Step 4: combinazioni ---
        aggiungi_log(task_id, "Step 4 — Costruzione combinazioni...")
        df_combinazioni = api._build_combinations()
        time.sleep(2)
        aggiungi_log(task_id, f"Step 4 — {len(df_combinazioni)} combinazioni costruite.")


        # --- Step 5: salvataggio su disco e ZIP ---
        aggiungi_log(task_id, "Step 5 — Salvataggio file CSV su disco...")
        paths_prodotti     = api._save_chunks(df_prodotti,     "prodotti",     "|", 500, "prodotti")
        paths_categorie    = api._save_chunks(df_categorie,    "categorie",    ";", 500, "categorie")
        paths_combinazioni = api._save_chunks(df_combinazioni, "combinazioni", ";", 500, "combinazioni")

        all_paths = paths_prodotti + paths_categorie + paths_combinazioni
        n_files = len(all_paths)
        time.sleep(2)
        aggiungi_log(task_id, f"Step 5 — {n_files} file CSV generati. Creazione ZIP...")

        percorso_zip = f"/tmp/{task_id}_catalogo.zip"
        with zipfile.ZipFile(percorso_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in all_paths:
                # Mantiene la struttura prodotti/ categorie/ combinazioni/ dentro lo ZIP
                arcname = os.path.relpath(path, output_dir)
                zf.write(path, arcname)

        aggiungi_log(task_id, f"ZIP creato con {n_files} file.")
        aggiorna_stato(task_id, "Completato", zip=percorso_zip)

    except Exception as e:
        aggiorna_stato(task_id, "Errore")
        aggiungi_log(task_id, f"Errore: {e}")

    finally:
        rimuovi_csv_temporaneo(percorso_csv)


@app.route("/")
def hello_world():
    return render_template('app.html')


@app.route("/upload", methods=["POST"])
def upload():
    if 'upload' not in request.files:
        return jsonify({"error": "File non trovato nella richiesta"}), 400
    file = request.files['upload']
    if file.filename == '':
        return jsonify({"error": "Nessun file selezionato"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Il file deve essere in formato CSV"}), 400

    sep = request.form.get("sep", "|") or "|"  # ← nuovo
    
    task_id = str(uuid.uuid4())
    percorso_temp_input = f"/tmp/{task_id}_input.csv"
    file.save(percorso_temp_input)

    STATO_PROCESSI[task_id] = {"stato": None, "zip": None}
    LOG_PROCESSI[task_id] = []

    aggiorna_stato(task_id, "In corso")
    aggiungi_log(task_id, "File ricevuto. Avvio pipeline...")

    thread = threading.Thread(target=pipeline, args=(task_id, percorso_temp_input, sep))
    thread.start()

    return jsonify({"task_id": task_id})


@app.route('/stream-log/<task_id>')
def stream_log(task_id):
    """Invia i log progressivi della pipeline al browser via SSE."""
    def genera_log():
        indice_letto = 0
        while True:
            if task_id in LOG_PROCESSI and indice_letto < len(LOG_PROCESSI[task_id]):
                messaggio = LOG_PROCESSI[task_id][indice_letto]
                indice_letto += 1
                yield f"data: {messaggio}\n\n"

            if task_id in STATO_PROCESSI and STATO_PROCESSI[task_id]["stato"] in ["Completato", "Errore"]:
                yield f"data: FINE_PROCESSO\n\n"
                break

            os.sched_yield()

    return Response(genera_log(), mimetype='text/event-stream')


@app.route('/scarica-zip/<task_id>', methods=["GET", "HEAD"])
def scarica_zip(task_id):
    if task_id not in STATO_PROCESSI or STATO_PROCESSI[task_id]["stato"] != "Completato":
        return "File non pronto o ID non valido", 404

    # Per la HEAD rispondiamo solo con gli header, senza toccare i dizionari
    if request.method == "HEAD":
        return "", 200

    percorso_zip = STATO_PROCESSI[task_id]["zip"]
    response = send_file(percorso_zip, as_attachment=True, download_name="catalogo_frazionato.zip")

    @response.call_on_close
    def cancella_zip_server():
        if os.path.exists(percorso_zip):
            os.remove(percorso_zip)
        STATO_PROCESSI.pop(task_id, None)
        LOG_PROCESSI.pop(task_id, None)

    return response
