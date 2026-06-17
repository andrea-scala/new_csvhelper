# PrestaImport

Uno strumento web che automatizza l'intera pipeline da un catalogo CSV grezzo di un fornitore a file pronti per l'importazione in PrestaShop — con streaming del progresso in tempo reale nel browser.

---

## Cosa fa

I cataloghi dei fornitori sono spesso caotici: riferimenti duplicati, descrizioni HTML malformate, formattazione delle caratteristiche inconsistente, migliaia di righe. PrestaImport prende un CSV grezzo e produce file di importazione puliti e suddivisi in chunk, compatibili con l'importatore nativo di PrestaShop — senza pulizia manuale, senza lavoro su foglio di calcolo.

**Output:**
- `prodotti/` — righe prodotto base, una per ID prodotto, deduplicate
- `categorie/` — albero delle categorie completo con slug e mapping dei genitori
- `combinazioni/` — combinazioni di prodotto con attributi, EAN13 e quantità

Tutti i file vengono impacchettati in un unico `.zip`, pronto per l'importazione.

---

## Pipeline

```
Upload CSV → Pulizia → Estrazione → Chunk & ZIP → Download
```

1. **Pulizia** — rimuove HTML non valido dalle descrizioni, normalizza le stringhe delle caratteristiche, applica la regola IVA italiana (ID 53), calcola gli sconti, corregge i riferimenti duplicati
2. **Estrazione** — costruisce i prodotti base (una riga per prodotto), l'albero delle categorie (con deduplicazione dei nodi omonimi a profondità diverse) e le combinazioni
3. **Chunking** — suddivide i DataFrame grandi in file CSV da 500 righe per restare nei limiti dell'importatore di PrestaShop
4. **ZIP** — impacchetta tutti i file di output preservando la struttura delle cartelle `prodotti/`, `categorie/`, `combinazioni/`

---

## Stack tecnologico

| Livello | Tecnologia |
|---|---|
| Backend | Python, Flask |
| Elaborazione dati | pandas |
| Streaming in tempo reale | Server-Sent Events (SSE) |
| Elaborazione in background | `threading` di Python |
| Frontend | Vanilla JS, HTML/CSS (senza framework) |

Il progresso viene trasmesso al browser via SSE durante l'esecuzione della pipeline. Il frontend consuma messaggi sentinella (`STEP:PULIZIA`, `STEP:ESTRAZIONE`, `FINE_PROCESSO`) per avanzare uno stepper live senza polling.

---

## Struttura del progetto

```
new_csvhelper/
├── app.py          # App Flask — route per upload, stream SSE, download ZIP
├── csvapi.py       # Nucleo della pipeline — pulizia, estrazione, chunking
└── templates/
    └── app.html    # Frontend a pagina singola
```

---

## Avvio in locale

```bash
cd new_csvhelper
python -m venv .venv && source .venv/bin/activate
pip install flask pandas
flask --app app run --debug
```

Apri `http://localhost:5000`, carica il CSV del fornitore, seleziona il separatore di colonna e clicca **Avvia elaborazione**.

---

## Formato CSV

L'input atteso è un CSV separato da pipe (`|`) con almeno queste colonne:

| Colonna | Descrizione |
|---|---|
| `ID prodotto` | ID univoco del prodotto (raggruppa le combinazioni) |
| `Riferimento` | SKU / riferimento fornitore |
| `Descrizione` | Descrizione HTML del prodotto |
| `Caratteristiche` | Stringa delle caratteristiche (`Nome:Valore:Posizione;...`) |
| `Categoria` | Percorso categoria separato da virgole (`Genitore,Figlio`) |
| `Prezzo (consigliato)` | Prezzo base |
| `Prezzo scontato (consigliato)` | Prezzo scontato |
| `Nome attributo` | Nome dell'attributo della combinazione |
| `Valore attributo` | Valore dell'attributo della combinazione |
| `EAN13` | Codice a barre |
| `Quantità` | Giacenza |
| `URL immagini` | URL delle immagini |

Il separatore può essere cambiato al momento dell'upload tramite il menu a tendina nell'interfaccia.

---

## Licenza

MIT

