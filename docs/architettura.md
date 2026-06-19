# PrestaImport — Architettura di deployment

Documento di riferimento sulla scelta, le motivazioni e l'implementazione dello stack di produzione di PrestaImport.

---

## 1. Architettura scelta e motivi

### Lo stack finale

```
Browser
   │
   ▼
Apache (porta 80) ── serve l'app esistente su "/"
   │
   └── ProxyPass su "/tools/prestaimport" ──▶ Gunicorn (127.0.0.1:8000, worker gevent)
                                                    │
                                                    ▼
                                              Flask (app.py)
                                              └── CSVApi (csvapi.py)
```

Tenuto in vita da **systemd**, esposto al mondo tramite **Apache** come reverse proxy.

### Perché non il server di sviluppo di Flask

Il comando `flask --app app run` usa il server interno di Werkzeug, pensato esclusivamente per lo sviluppo locale. Tre limiti lo rendono inadatto alla produzione:

1. **Concorrenza limitata** — di base gestisce una richiesta alla volta. Con una route SSE che resta aperta per tutta la durata della pipeline (anche un minuto), un solo upload in corso blocca qualunque altra richiesta, inclusa la home page.
2. **Nessuna resilienza** — se il processo crasha o la sessione SSH si chiude, l'app muore e nessuno la rialza.
3. **Nessun hardening** — manca qualunque protezione contro richieste malformate o traffico anomalo, cosa che un server di produzione gestisce nativamente.

### Perché Gunicorn

Gunicorn è il server WSGI standard per applicazioni Flask in produzione su Linux: gestisce più richieste in parallelo tramite processi *worker*, espone parametri di configurazione pensati per ambienti reali (numero di worker, timeout, restart automatico) ed è lo strumento de facto per questo tipo di deployment.

### Perché il worker `gevent` (e non `sync`)

La route che genera i log in tempo reale:

```python
@app.route('/stream-log/<task_id>')
def stream_log(task_id):
    def genera_log():
        while True:
            # ...
            yield f"data: {messaggio}\n\n"
            os.sched_yield()
    return Response(genera_log(), mimetype='text/event-stream')
```

resta **aperta** per tutta la pipeline, inviando eventi SSE man mano che arrivano.

Con worker **sync** (il default di Gunicorn), ogni worker gestisce **una sola richiesta alla volta** ed è bloccato per tutta la sua durata. Con soli 2 worker, due upload contemporanei saturerebbero l'intera capacità del server: una terza persona non riuscirebbe nemmeno ad aprire la home page, pur non essendoci alcun calcolo realmente in corso — i worker sarebbero semplicemente fermi ad aspettare nuovi log.

Con worker **gevent**, un singolo worker può gestire centinaia di connessioni aperte contemporaneamente, perché quando il codice è in attesa (come il `while True` con `os.sched_yield()`), gevent libera la capacità di calcolo per servire altre connessioni nel frattempo, restando dentro lo stesso processo.

**Il discriminante non è la durata dell'operazione, ma se la connessione resta aperta in attesa di eventi multipli** (SSE, WebSocket, long-polling) oppure se è una singola richiesta → singola risposta, per quanto lenta. Senza la route SSE, sarebbero bastati worker `sync` standard.

### Perché systemd

Gunicorn da solo è "solo" un comando da lanciare a mano nel terminale — se chiudi la sessione SSH, il processo muore con essa. systemd lo trasforma in un **servizio**: si avvia automaticamente al boot del server e riparte da solo in caso di crash (`Restart=on-failure`), senza intervento manuale.

### Perché Apache come reverse proxy

Gunicorn ascolta solo su `127.0.0.1:8000` — cioè è raggiungibile esclusivamente da processi sulla stessa macchina, non da internet. Questa è una scelta di sicurezza: un solo punto d'ingresso pubblico (Apache sulla porta 80) invece di esporre Gunicorn direttamente, perdendo le protezioni che un web server "vero" offre normalmente (gestione SSL, virtual host multipli, ecc.).

Apache permette inoltre di far convivere **più applicazioni sullo stesso server e stesso IP**, ciascuna su un path diverso, senza conflitti di porta.

---

## 2. Scelte implementative e architetturali

### File coinvolti e ruolo di ciascuno

| File | Tipo | Ruolo |
|---|---|---|
| `app.py` | Codice applicativo | Route Flask, logica di orchestrazione della pipeline |
| `csvapi.py` | Codice applicativo | Classe `CSVApi`, logica di pulizia/estrazione del catalogo |
| `templates/app.html` | Frontend | Pagina singola, stepper, console log, upload |
| `/etc/systemd/system/prestaimport.service` | Configurazione di sistema | Tiene Gunicorn vivo come servizio |
| `/etc/apache2/sites-enabled/000-default.conf` | Configurazione di sistema | Inoltra le richieste pubbliche a Gunicorn |

### `prestaimport.service` — riga per riga

```ini
[Unit]
Description=PrestaImport
After=network.target

[Service]
User=vincenzo
WorkingDirectory=/var/www/prestaimport
ExecStart=/var/www/prestaimport/.venv/bin/gunicorn --worker-class gevent --workers 2 --bind 127.0.0.1:8000 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- `ExecStart=` — direttiva systemd: comando da eseguire all'avvio del servizio. Tutto ciò che segue è un comando shell, identico a quanto si scriverebbe a mano nel terminale.
- `/var/www/prestaimport/.venv/bin/gunicorn` — path **completo** al binario nel virtualenv. systemd non "attiva" il venv come fa `source .venv/bin/activate`, quindi serve il path assoluto.
- `--worker-class gevent` — istruisce Gunicorn a usare worker basati su gevent invece del default `sync`.
- `--workers 2` — quanti processi worker avviare oltre al master. Con gevent, 2 worker non servono per la concorrenza (che gevent gestisce già bene con uno solo) ma per **resilienza**: se un worker crasha, l'altro continua a rispondere mentre il primo si riavvia.
- `--bind 127.0.0.1:8000` — indirizzo e porta di ascolto, riservato al solo traffico locale.
- `app:app` — `app.py` (nome file, prima dei due punti) e `app` (nome della variabile Flask al suo interno, dopo i due punti).
- `Restart=on-failure` — riavvia automaticamente il servizio se il processo termina con errore.

### `gevent` non è nativo in Gunicorn

Gunicorn supporta diverse *worker class* in modo intercambiabile, ma alcune dipendono da librerie esterne non incluse di base:

| Worker class | Libreria esterna richiesta |
|---|---|
| `sync` | No |
| `gthread` | No |
| `gevent` | Sì — pacchetto `gevent` |
| `eventlet` | Sì — pacchetto `eventlet` |

Va quindi installato esplicitamente:

```bash
pip install gunicorn gevent
```

### Configurazione Apache — `000-default.conf`

```apache
ProxyPass /tools/prestaimport http://127.0.0.1:8000/
ProxyPassReverse /tools/prestaimport http://127.0.0.1:8000/
SetEnv proxy-nokeepalive 1
SetEnv proxy-initial-not-pooled 1
```

- `ProxyPass` — le richieste a `http://<host>/tools/prestaimport/...` non vengono cercate come file statici, ma inoltrate a `http://127.0.0.1:8000/...`. **Il prefisso viene tolto automaticamente** nell'inoltro.
- `ProxyPassReverse` — riscrive eventuali URL negli header di risposta perché restino coerenti con il path pubblico.
- `proxy-nokeepalive` / `proxy-initial-not-pooled` — disattivano ottimizzazioni di Apache che altrimenti bufferizzerebbero la risposta, rompendo lo streaming SSE (i log arriverebbero tutti insieme alla fine invece che in tempo reale).

Prima di poter usare `ProxyPass` è stato necessario abilitare i moduli Apache corrispondenti:

```bash
sudo a2enmod proxy proxy_http
sudo systemctl restart apache2
```

### Il flusso completo di una richiesta

```
Browser chiede:
http://192.168.1.36/tools/prestaimport/upload
        │
        ▼
Apache riconosce il prefisso /tools/prestaimport
        │  lo rimuove, inoltra a:
        ▼
http://127.0.0.1:8000/upload
        │
        ▼
Gunicorn riceve la richiesta, la assegna a un worker libero
        │
        ▼
Flask esegue la route @app.route("/upload", ...)
```

**`app.py` non è consapevole del prefisso** `/tools/prestaimport` — le sue route restano sempre `/`, `/upload`, `/stream-log/<task_id>`, `/scarica-zip/<task_id>`. È Apache a fare la traduzione, in modo completamente trasparente al codice Flask.

Di conseguenza, il frontend (`app.html`) è l'unico punto dove il prefisso pubblico deve essere esplicitato, perché è da lì che partono le chiamate `fetch`/`EventSource` verso il browser:

```javascript
var UPLOAD_URL   = "/tools/prestaimport/upload";
var STREAM_URL   = "/tools/prestaimport/stream-log/";
var DOWNLOAD_URL = "/tools/prestaimport/scarica-zip/";
```

---

## 3. FAQ

**D: Quanti worker stiamo usando, e dove lo verifico?**
2 worker, definiti in `--workers 2` dentro `prestaimport.service`. Per verificarli a runtime:
```bash
sudo systemctl status prestaimport
```
Tre processi compaiono: 1 master (supervisore) + 2 worker (quelli che rispondono davvero alle richieste).

**D: Potevamo usare un solo worker?**
Sì, con `gevent` un solo worker regge già moltissime connessioni concorrenti. La scelta di usarne 2 è per resilienza: se un worker crasha, l'altro continua a rispondere mentre il primo si riavvia tramite `Restart=on-failure`.

**D: Potevamo usare più worker sync invece di gevent?**
Tecnicamente sì, ma in modo molto più costoso. Con `sync`, ogni connessione SSE aperta occupa un intero worker per tutta la sua durata — per reggere 20 connessioni SSE simultanee servirebbero 20 worker, ciascuno un processo Python separato con la sua copia in memoria di Flask e pandas. Con `gevent`, lo stesso risultato si ottiene con 1-2 worker, perché le connessioni in attesa non bloccano il processo.

**D: Gunicorn ascolta sempre su `127.0.0.1:8000`?**
No, è una configurazione scelta tramite `--bind`, non un default fisso. Avremmo potuto usare `0.0.0.0` (raggiungibile anche dall'esterno) o una porta diversa. È stato scelto `127.0.0.1` per tenere Gunicorn raggiungibile solo localmente, lasciando ad Apache il ruolo di unico punto d'ingresso pubblico.

**D: `.venv` dove si trova?**
Dentro la cartella del progetto: `/var/www/prestaimport/.venv`. Creato con `python3 -m venv .venv` direttamente in quella directory, per questo il path nel file systemd è `/var/www/prestaimport/.venv/bin/gunicorn`.

**D: L'introduzione di gevent è legata alla route SSE?**
Sì, direttamente. Senza una route che genera un flusso continuo di eventi (come `/stream-log/<task_id>`), non ci sarebbe stato bisogno di gevent: sarebbe bastato `gunicorn --workers 3 --bind 127.0.0.1:8000 app:app` con worker `sync` di default. Il discriminante è se una connessione resta aperta in attesa di eventi multipli (SSE, WebSocket) oppure se è una richiesta classica, per quanto lenta da elaborare.

**D: Perché il path pubblico è `/tools/prestaimport` e non semplicemente `/`?**
Per evitare conflitti con l'applicazione preesistente sullo stesso server/IP, che già rispondeva su `/`. Inizialmente si era tentato anche `/prestaimport`, ma Apache serviva già una cartella statica chiamata `upload/` sotto `/var/www/html`, in conflitto con la route Flask `/upload`. Spostando tutto sotto `/tools/prestaimport` si è eliminato ogni conflitto di path.

**D: Perché non usare `DispatcherMiddleware` di Werkzeug per gestire il prefisso lato Flask?**
È stato provato, ma introduce complessità inutile quando il prefisso può essere gestito in modo più semplice e trasparente direttamente da Apache con `ProxyPass`. Con quest'ultimo approccio, il codice Flask resta agnostico rispetto al path pubblico — più semplice da mantenere e da spostare in futuro su un path diverso, modificando solo la configurazione Apache.

EOF