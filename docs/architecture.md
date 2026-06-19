# PrestaImport — Deployment Architecture

*[Italiano sotto / Italian below](#prestaimport--architettura-di-deployment-italiano)*

Reference document on the choices, rationale, and implementation of PrestaImport's production stack.

---

## 1. Architecture chosen and rationale

### Final stack

```
Browser
   │
   ▼
Apache (port 80) ── serves the existing app on "/"
   │
   └── ProxyPass on "/tools/prestaimport" ──▶ Gunicorn (127.0.0.1:8000, gevent worker)
                                                    │
                                                    ▼
                                              Flask (app.py)
                                              └── CSVApi (csvapi.py)
```

Kept alive by **systemd**, exposed to the world through **Apache** as a reverse proxy.

### Why not Flask's development server

The `flask --app app run` command uses Werkzeug's built-in server, designed exclusively for local development. Three limits make it unsuitable for production:

1. **Limited concurrency** — by default it handles one request at a time. With an SSE route that stays open for the entire pipeline duration (up to a minute), a single upload in progress blocks every other request, including the home page.
2. **No resilience** — if the process crashes or the SSH session closes, the app dies and nothing brings it back.
3. **No hardening** — it lacks any protection against malformed requests or abnormal traffic, which a production server handles natively.

### Why Gunicorn

Gunicorn is the standard WSGI server for Flask applications in production on Linux: it handles multiple requests in parallel through *worker* processes, exposes configuration parameters designed for real-world environments (worker count, timeouts, automatic restarts), and is the de facto tool for this kind of deployment.

### Why the `gevent` worker (and not `sync`)

The route that streams logs in real time:

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

stays **open** for the whole pipeline, sending SSE events as they arrive.

With a **sync** worker (Gunicorn's default), each worker handles **one request at a time** and is blocked for its entire duration. With only 2 workers, two concurrent uploads would saturate the server's entire capacity: a third visitor wouldn't even be able to load the home page, even though no actual computation is happening — the workers are simply sitting idle, waiting for new log lines.

With a **gevent** worker, a single worker can handle hundreds of open connections concurrently, because when the code is waiting (like the `while True` loop with `os.sched_yield()`), gevent frees up compute capacity to serve other connections in the meantime, all within the same process.

**The deciding factor isn't how long an operation takes, but whether the connection stays open waiting for multiple events** (SSE, WebSocket, long-polling) versus a single request → single response, however slow. Without the SSE route, standard `sync` workers would have been enough.

### Why systemd

Gunicorn on its own is "just" a command to run by hand in the terminal — close the SSH session and the process dies with it. systemd turns it into a **service**: it starts automatically on server boot and restarts itself on crash (`Restart=on-failure`), with no manual intervention.

### Why Apache as a reverse proxy

Gunicorn listens only on `127.0.0.1:8000` — reachable exclusively by processes on the same machine, not from the internet. This is a security choice: a single public entry point (Apache on port 80) instead of exposing Gunicorn directly, which would lose the protections a "real" web server normally offers (SSL handling, multiple virtual hosts, etc.).

Apache also allows **multiple applications to coexist on the same server and IP**, each on a different path, with no port conflicts.

---

## 2. Implementation and architectural choices

### Files involved and their role

| File | Type | Role |
|---|---|---|
| `app.py` | Application code | Flask routes, pipeline orchestration logic |
| `csvapi.py` | Application code | `CSVApi` class, catalog cleaning/extraction logic |
| `templates/app.html` | Frontend | Single-page app, stepper, log console, upload |
| `/etc/systemd/system/prestaimport.service` | System configuration | Keeps Gunicorn alive as a service |
| `/etc/apache2/sites-enabled/000-default.conf` | System configuration | Forwards public requests to Gunicorn |

### `prestaimport.service` — line by line

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

- `ExecStart=` — systemd directive: the command to run when the service starts. Everything after it is a shell command, identical to what you'd type by hand in the terminal.
- `/var/www/prestaimport/.venv/bin/gunicorn` — the **full** path to the binary inside the virtualenv. systemd doesn't "activate" the venv the way `source .venv/bin/activate` does, so an absolute path is required.
- `--worker-class gevent` — tells Gunicorn to use gevent-based workers instead of the default `sync`.
- `--workers 2` — how many worker processes to spawn besides the master. With gevent, 2 workers aren't needed for concurrency (which gevent already handles well with just one) but for **resilience**: if one worker crashes, the other keeps responding while the first restarts.
- `--bind 127.0.0.1:8000` — listening address and port, restricted to local traffic only.
- `app:app` — `app.py` (file name, before the colon) and `app` (the Flask variable name inside it, after the colon).
- `Restart=on-failure` — automatically restarts the service if the process exits with an error.

### `gevent` isn't native to Gunicorn

Gunicorn supports several interchangeable *worker classes*, but some depend on external libraries not included by default:

| Worker class | External library required |
|---|---|
| `sync` | No |
| `gthread` | No |
| `gevent` | Yes — `gevent` package |
| `eventlet` | Yes — `eventlet` package |

It therefore needs to be installed explicitly:

```bash
pip install gunicorn gevent
```

### Apache configuration — `000-default.conf`

```apache
ProxyPass /tools/prestaimport http://127.0.0.1:8000/
ProxyPassReverse /tools/prestaimport http://127.0.0.1:8000/
SetEnv proxy-nokeepalive 1
SetEnv proxy-initial-not-pooled 1
```

- `ProxyPass` — requests to `http://<host>/tools/prestaimport/...` aren't looked up as static files, but forwarded to `http://127.0.0.1:8000/...`. **The prefix is stripped automatically** during forwarding.
- `ProxyPassReverse` — rewrites any URLs in response headers so they stay consistent with the public path.
- `proxy-nokeepalive` / `proxy-initial-not-pooled` — disable Apache optimizations that would otherwise buffer the response, breaking SSE streaming (logs would arrive all at once at the end instead of in real time).

Before `ProxyPass` could be used, the corresponding Apache modules had to be enabled:

```bash
sudo a2enmod proxy proxy_http
sudo systemctl restart apache2
```

### The full request flow

```
Browser requests:
http://192.168.1.36/tools/prestaimport/upload
        │
        ▼
Apache recognizes the /tools/prestaimport prefix
        │  strips it, forwards to:
        ▼
http://127.0.0.1:8000/upload
        │
        ▼
Gunicorn receives the request, assigns it to a free worker
        │
        ▼
Flask runs the @app.route("/upload", ...) handler
```

**`app.py` has no awareness of the `/tools/prestaimport` prefix** — its routes are always just `/`, `/upload`, `/stream-log/<task_id>`, `/scarica-zip/<task_id>`. Apache handles the translation, completely transparently to the Flask code.

As a result, the frontend (`app.html`) is the only place where the public prefix needs to be made explicit, since that's where `fetch`/`EventSource` calls originate from the browser:

```javascript
var UPLOAD_URL   = "/tools/prestaimport/upload";
var STREAM_URL   = "/tools/prestaimport/stream-log/";
var DOWNLOAD_URL = "/tools/prestaimport/scarica-zip/";
```

---

## 3. FAQ

**Q: How many workers are we using, and where do I check?**
2 workers, set via `--workers 2` in `prestaimport.service`. To check at runtime:
```bash
sudo systemctl status prestaimport
```
Three processes show up: 1 master (supervisor) + 2 workers (the ones actually answering requests).

**Q: Could we have used a single worker?**
Yes — with `gevent`, a single worker already handles a large number of concurrent connections. Using 2 is a resilience choice: if one worker crashes, the other keeps responding while the first restarts via `Restart=on-failure`.

**Q: Could we have used more sync workers instead of gevent?**
Technically yes, but at a much higher cost. With `sync`, each open SSE connection occupies an entire worker for its full duration — handling 20 simultaneous SSE connections would require 20 workers, each a separate Python process with its own in-memory copy of Flask and pandas. With `gevent`, the same result is achieved with 1-2 workers, because waiting connections don't block the process.

**Q: Does Gunicorn always listen on `127.0.0.1:8000`?**
No, it's a configuration chosen via `--bind`, not a fixed default. We could have used `0.0.0.0` (reachable from outside) or a different port. `127.0.0.1` was chosen to keep Gunicorn reachable only locally, leaving Apache as the sole public entry point.

**Q: Where is `.venv` located?**
Inside the project folder: `/var/www/prestaimport/.venv`. Created with `python3 -m venv .venv` directly in that directory, which is why the path in the systemd file is `/var/www/prestaimport/.venv/bin/gunicorn`.

**Q: Is introducing gevent tied to the SSE route?**
Yes, directly. Without a route that generates a continuous stream of events (like `/stream-log/<task_id>`), gevent wouldn't have been necessary: `gunicorn --workers 3 --bind 127.0.0.1:8000 app:app` with default `sync` workers would have sufficed. The deciding factor is whether a connection stays open waiting for multiple events (SSE, WebSocket) versus a classic request, however slow to process.

**Q: Why is the public path `/tools/prestaimport` and not simply `/`?**
To avoid conflicts with the pre-existing application on the same server/IP, which already responded on `/`. Initially `/prestaimport` was also tried, but Apache was already serving a static folder called `upload/` under `/var/www/html`, conflicting with the Flask `/upload` route. Moving everything under `/tools/prestaimport` eliminated every path conflict.

**Q: Why not use Werkzeug's `DispatcherMiddleware` to handle the prefix on the Flask side?**
It was tried, but it introduces unnecessary complexity when the prefix can be handled more simply and transparently directly by Apache via `ProxyPass`. With this approach, the Flask code stays agnostic of the public path — easier to maintain and easier to move to a different path in the future, by changing only the Apache configuration.

---

<a id="prestaimport--architettura-di-deployment-italiano"></a>
# PrestaImport — Architettura di deployment (Italiano)

*[English above](#prestaimport--deployment-architecture)*

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