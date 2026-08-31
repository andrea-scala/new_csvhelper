import pandas as pd
import logging
import os
import re
from datetime import datetime

pd.set_option('display.max_colwidth', None)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class CSVApi:
    """
    Gestisce il ciclo di vita del catalogo prodotti di un fornitore:
    lettura del CSV, pulizia dei campi problematici e riesportazione
    in un formato compatibile con l'importatore nativo di PrestaShop.

    Produce tre tipi di output:
    - Categorie (export/categorie/)
    - Prodotti puliti (export/prodotti/)
    - Combinazioni (export/combinazioni/)
    """

    # ID di partenza per le categorie generate.
    # 1 = root di sistema PS, 2 = Home. Si parte da 3.
    CATEGORY_ID_START = 3

    def __init__(self, source: str, sep: str, output_dir: str = "export"):
        """
        Legge il CSV del fornitore e inizializza il DataFrame interno.

        :param source: Percorso del file CSV da importare.
        :param sep:    Separatore di colonna usato nel CSV (es. '|').

        EAN13 viene forzato a stringa per preservare eventuali zeri
        iniziali e per evitare che pandas lo interpreti come intero
        o notazione scientifica.

        :raises ValueError:       Se il percorso o il separatore sono vuoti,
                                  o se il file non è un CSV.
        :raises FileNotFoundError: Se il file non esiste nel percorso indicato.
        """
        if not source:
            raise ValueError("Il percorso del file CSV non può essere vuoto.")
        if not os.path.exists(source):
            raise FileNotFoundError(f"File non trovato: {source}")
        if not source.endswith('.csv'):
            raise ValueError(f"Il file deve essere un CSV: {source}")
        if not sep:
            raise ValueError("Il separatore non può essere vuoto.")

        self.source = pd.read_csv(filepath_or_buffer=source, sep=sep, dtype={'EAN13': str})
        self.sep = sep
        self.output_dir = output_dir
        # Popolato da _build_categories() — usato da _save_chunks per i prodotti
        self._category_tree: dict[tuple, int] = {}

        logger.info(f"CSV caricato: {source} — {len(self.source)} prodotti, {len(self.source.columns)} colonne")

    # ------------------------------------------------------------------ #
    #  PULIZIA                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean_description(s: pd.Series) -> pd.Series:
        """
        Pulisce il campo Descrizione da contenuto incompatibile
        con il validatore di PrestaShop (Validate::isCleanHtml).

        1. Rimuove il pattern '"(' (misure in pollici escaped male).
        2. Rimuove tutti i tag HTML eccetto <h4> e </h4>.
        """
        logger.info("Pulizia descrizioni in corso...")

        result = s.str.replace('"(', '(', regex=False).apply(
            lambda x: re.sub(r'<(?!/?h4)[^>]+>', '', x) if pd.notna(x) else x
        )

        modified = (result != s).sum()
        logger.info(f"Descrizioni pulite: {modified} modificate su {len(s)} totali")
        return result

    @staticmethod
    def _clean_features(s: pd.Series) -> pd.Series:
        """
        Normalizza il campo Caratteristiche.
        Formato atteso: Nome:Valore:Posizione,Nome:Valore:Posizione,...
        """
        logger.info("Normalizzazione caratteristiche in corso...")

        def parse_and_rebuild(text: str) -> str:
            if pd.isna(text):
                return text
            pattern = r'([^;:]+?)\s*:\s*(.*?)\s*:(\d+)(?=;|$)'
            matches = re.findall(pattern, text)
            return ','.join(
                f'{m[0].strip()}:{m[1].strip().replace(",", ".")}:{m[2]}'
                for m in matches
            )

        result = s.apply(parse_and_rebuild)
        modified = (result != s).sum()
        logger.info(f"Caratteristiche normalizzate: {modified} modificate su {len(s)} totali")
        return result

    def _add_discount_amount(self) -> None:
        """
        Aggiunge 'Discount amount' e 'On sale (0/1)'.
        Differenze negative o < 0.50€ vengono azzerate.
        """
        logger.info("Calcolo sconti in corso...")

        base_price     = self.source["Prezzo (consigliato)"].astype(float)
        discount_price = self.source["Prezzo scontato (consigliato)"].astype(float)

        diff = (base_price - discount_price).round(2).clip(lower=0)
        diff = diff.where(diff >= 0.50, 0)

        self.source["Discount amount"]  = diff
        self.source["On sale (0/1)"]    = (diff > 0).astype(int)

        on_sale = (self.source["On sale (0/1)"] == 1).sum()
        logger.info(f"Sconti calcolati: {on_sale} prodotti in saldo su {len(self.source)} totali")

    def _add_tax(self) -> None:
        """Imposta Tax rules group = 53 (IVA italiana) su tutti i prodotti."""
        self.source["Tax rules group"] = 53
        logger.info(f"Regola IVA 53 applicata a {len(self.source)} prodotti")

    def _fix_duplicate_references(self) -> None:
        """
        Corregge i riferimenti duplicati aggiungendo un suffisso incrementale.
        Es: "00902992" → "00902992_2", "00902992_3"
        """
        logger.info("Verifica riferimenti duplicati in corso...")

        seen       = {}
        duplicates = 0

        for i, ref in enumerate(self.source["Riferimento"]):
            if ref in seen:
                seen[ref] += 1
                self.source.at[i, "Riferimento"] = f"{ref}_{seen[ref]}"
                logger.warning(f"Riferimento duplicato: '{ref}' → '{ref}_{seen[ref]}'")
                duplicates += 1
            else:
                seen[ref] = 1

        if duplicates == 0:
            logger.info("Nessun riferimento duplicato trovato")
        else:
            logger.warning(f"Corretti {duplicates} riferimenti duplicati")

    # ------------------------------------------------------------------ #
    #  CATEGORIE                                                           #
    # ------------------------------------------------------------------ #

    def _build_categories(self) -> pd.DataFrame:
        """
        Costruisce il DataFrame delle categorie da importare in PrestaShop.
 
        Ogni categoria è identificata dal suo percorso completo (tupla di nomi),
        così da distinguere omonimi a livelli diversi.
        Es: ('Bambole Gonfiabili',) ≠ ('Oggettistica', 'Bambole Gonfiabili')
 
        Struttura output (separatore ';'):
            Category ID | Active | Name | Parent category | Root category |
            Description | Meta title | Meta description | URL rewritten | Image URL
 
        Il campo 'Parent category' contiene il NOME del padre (non l'ID),
        come richiesto dal template di import nativo di PrestaShop.
        Per le categorie radice il padre è 'Home'.
 
        :return: DataFrame pronto per l'esportazione.
        """
        logger.info("Costruzione albero categorie in corso...")
 
        # path (tuple) → category_id
        tree: dict[tuple, int] = {}
        next_id = self.CATEGORY_ID_START
 
        for cat_string in self.source["Categoria"].dropna():
            parts = [p.strip() for p in cat_string.split(',') if p.strip()]
            for depth in range(len(parts)):
                path = tuple(parts[:depth + 1])
                if path not in tree:
                    tree[path] = next_id
                    next_id += 1
 
        # Salva il tree per uso interno (es. mapping prodotti)
        self._category_tree = tree
 
        rows = []
        for path, cat_id in tree.items():
            name        = path[-1]
            parent_name = path[-2] if len(path) > 1 else "Home"
            # Slug = ID + nome categoria, garantisce unicità e rispetta
            # il limite di 128 caratteri di PrestaShop.
            # Es: "176-lubrificanti-al-silicone"
            name_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
            url_slug  = f"{cat_id}-{name_slug}"[:128]
 
            rows.append({
                "Category ID":          cat_id,
                "Active (0/1)":         1,
                "Name *":               name,
                "Parent category":      parent_name,
                "Root category (0/1)":  0,
                "Description":          "",
                "Meta title":           name,
                "Meta description":     "",
                "URL rewritten":        url_slug,
                "Image URL":            "",
            })
 
        df_cat = pd.DataFrame(rows)
 
        # Deduplicazione: due percorsi diversi possono produrre la stessa
        # coppia (Name, Parent) — es. entrambi terminano con
        # "Lubrificanti Anali -> Lubrificanti al silicone".
        # In quel caso sono la stessa categoria PS: si tiene solo la prima.
        before = len(df_cat)
        df_cat = df_cat.drop_duplicates(subset=["Name *", "Parent category"]).reset_index(drop=True)
        df_cat["Category ID"] = range(self.CATEGORY_ID_START, self.CATEGORY_ID_START + len(df_cat))
        after = len(df_cat)
 
        if before != after:
            logger.warning(f"Deduplicate {before - after} categorie con stesso nome e padre")
 
        logger.info(f"Categorie costruite: {after} nodi")
        return df_cat

    # ------------------------------------------------------------------ #
    #  PRODOTTI                                                            #
    # ------------------------------------------------------------------ #

    def _build_products(self) -> pd.DataFrame:
        """
        Costruisce il DataFrame dei prodotti base da importare in PrestaShop.

        Per ogni gruppo di righe con lo stesso ID prodotto, produce
        una sola riga base con:
        - Campi comuni (nome, categoria, prezzo, ecc.) dalla prima riga
        - Riferimento = ID prodotto (es. "4076")
        - Campi combinazione azzerati SOLO se il prodotto ha effettivamente
        almeno una riga con Nome/Valore attributo valorizzati (cioè genera
        combinazioni). Se il prodotto è "semplice" (nessuna variante),
        EAN13 viene mantenuto sul prodotto base perché non finirebbe
        da nessun'altra parte.

        :return: DataFrame con una riga per prodotto.
        """
        logger.info("Costruzione prodotti base in corso...")

        df = (
            self.source
            .groupby("ID prodotto", sort=False)
            .first()
            .reset_index()
        )

        # Riferimento = solo ID prodotto
        df["Riferimento"] = df["ID prodotto"].astype(str)

        # Determina quali prodotti hanno almeno una combinazione vera
        has_variants = self.source.groupby("ID prodotto")["Nome attributo"].apply(
            lambda s: s.notna().any() and (s.astype(str).str.strip() != "").any()
        )
        mask = df["ID prodotto"].map(has_variants).fillna(False)

        n_with_variants = mask.sum()
        n_simple = (~mask).sum()
        logger.info(
            f"Prodotti con combinazioni: {n_with_variants} — "
            f"prodotti semplici (EAN mantenuto): {n_simple}"
        )

        # Azzera EAN13 solo dove ci sono davvero combinazioni
        df.loc[mask, "EAN13"] = ""

        # Nome/Valore attributo e ID combinazione sono sempre campi
        # di riepilogo a livello prodotto: si azzerano sempre, che ci
        # siano o meno combinazioni (il dettaglio va nel file combinazioni)
        df["ID prodotto + ID combinazione"] = ""
        df["Nome attributo"]                = ""
        df["Valore attributo"]              = ""

        logger.info(f"Prodotti base costruiti: {len(df)} righe (da {len(self.source)} combinazioni)")
        return df

    # ------------------------------------------------------------------ #
    #  COMBINAZIONI                                                        #
    # ------------------------------------------------------------------ #

    def _build_combinations(self) -> pd.DataFrame:
        """
        Costruisce il DataFrame delle combinazioni da importare in PrestaShop.

        Lavora su self.source (già pulito) e produce una riga per ogni
        prodotto con attributo/valore definiti.

        Colonne output (separatore ';'):
            Product ID* | Attribute (Name:Type:Position)* | Value (Value:Position)* |
            Supplier reference | Reference | EAN13 | ISBN | UPC |
            Wholesale price | Impact on price | Ecotax | Quantity |
            Minimal quantity | Low stock level | Impact on weight |
            Default (0 = No, 1 = Yes) | Combination available date |
            Image position | Image URLs (x,y,z...) | Image alt texts (x,y,z...) |
            ID / Name of shop | Advanced Stock Managment | Depends on stock | Warehouse

        :return: DataFrame pronto per l'esportazione.
        """
        logger.info("Costruzione combinazioni in corso...")

        rows = []
        for _, row in self.source.iterrows():
            nome_attr  = str(row.get("Nome attributo", "")).strip()
            valore_attr = str(row.get("Valore attributo", "")).strip()

            if not nome_attr or nome_attr == "nan":
                continue
            if not valore_attr or valore_attr == "nan":
                continue

            ean = "" if pd.isna(row.get("EAN13")) else str(row.get("EAN13", "")).strip()

            rows.append({
                "Product ID*":                    row["ID prodotto"],
                "Attribute (Name:Type:Position)*": f"{nome_attr}:select:0",
                "Value (Value:Position)*":         f"{valore_attr}:0",
                "Supplier reference":              "",
                "Reference":                       str(row.get("Riferimento", "")).strip(),
                "EAN13":                           ean,
                "ISBN":                            "",
                "UPC":                             "",
                "Wholesale price":                 "",
                "Impact on price":                 0,
                "Ecotax":                          0,
                "Quantity":                        int(row.get("Quantità", 0)),
                "Minimal quantity":                1,
                "Low stock level":                 "",
                "Impact on weight":                0,
                "Default (0 = No, 1 = Yes)":       0,
                "Combination available date":      "",
                "Image position":                  "",
                "Image URLs (x,y,z...)":           str(row.get("URL immagini", "")).strip(),
                "Image alt texts (x,y,z...)":      "",
                "ID / Name of shop":               1,
                "Advanced Stock Managment":        0,
                "Depends on stock":                0,
                "Warehouse":                       "",
            })

        df_comb = pd.DataFrame(rows)
        logger.info(f"Combinazioni costruite: {len(df_comb)} righe")
        return df_comb

    # ------------------------------------------------------------------ #
    #  SALVATAGGIO                                                         #
    # ------------------------------------------------------------------ #

    def _save_chunks(
        self,
        df: pd.DataFrame,
        subfolder: str,
        sep: str,
        chunk_size: int,
        prefix: str = "export",
    ) -> list[str]:
        """
        Salva un DataFrame in uno o più file CSV nella cartella indicata.

        Se il numero di righe supera chunk_size, il file viene spezzato
        in più chunk numerati (es. 20260604_01_di_03.csv).
        Altrimenti viene salvato in un unico file senza numerazione.

        :param df:         DataFrame da salvare.
        :param subfolder:  Sottocartella di destinazione (es. 'prodotti').
        :param sep:        Separatore CSV da usare in output.
        :param chunk_size: Soglia oltre la quale attivare lo split.
        :param prefix:     Prefisso del nome file. Default: 'export'.
        :return:           Lista dei path dei file generati.
        """
        out_dir = os.path.join(self.output_dir, subfolder)
        os.makedirs(out_dir, exist_ok=True)

        ts    = datetime.now().strftime('%Y%m%d_%H%M%S')
        paths = []

        if len(df) <= chunk_size:
            path = os.path.join(out_dir, f"{ts}_{prefix}.csv")
            df.to_csv(path, sep=sep, index=False, na_rep="")
            paths.append(path)
            logger.info(f"Salvato: {path} ({len(df)} righe)")
        else:
            total = (len(df) + chunk_size - 1) // chunk_size
            logger.info(f"Salvataggio in {total} chunk da {chunk_size} righe...")

            for i, start in enumerate(range(0, len(df), chunk_size)):
                chunk    = df.iloc[start:start + chunk_size]
                num      = f"0{i+1}" if i < 9 else str(i + 1)
                filename = f"{ts}_{num}_di_{total}_{prefix}.csv"
                path     = os.path.join(out_dir, filename)
                chunk.to_csv(path, sep=sep, index=False, na_rep="")
                paths.append(path)
                logger.info(f"Salvato chunk {i+1}/{total}: {path}")

        return paths

    # ------------------------------------------------------------------ #
    #  ORCHESTRATORE                                                       #
    # ------------------------------------------------------------------ #

    def clean_csv(self, save: int = 0, chunk_size: int = 500) -> dict:
        """
        Orchestratore principale. Esegue in sequenza:
          1. Pulizia del catalogo (descrizioni, caratteristiche, IVA, sconti,
             riferimenti duplicati)
          2. Costruzione categorie
          3. Costruzione combinazioni

        Se save=1, salva i risultati su disco in cartelle separate:
          - export/categorie/
          - export/prodotti/
          - export/combinazioni/

        Lo split in chunk si attiva solo se il numero di righe supera
        chunk_size (default 500). La soglia è uniforme per tutti e tre
        gli output.

        :param save:       1 = salva su disco, 0 = restituisce solo i DataFrame.
        :param chunk_size: Soglia righe per attivare lo split in chunk.
        :return:           Dict con chiavi 'prodotti', 'categorie', 'combinazioni'
                           contenenti rispettivamente i DataFrame o le liste di path.
        """
        logger.info("=== Avvio pipeline CSVApi ===")

        # --- Step 1: pulizia prodotti ---
        logger.info("--- Step 1: pulizia catalogo ---")
        self.source["Descrizione"]     = self._clean_description(self.source["Descrizione"])
        self.source["Caratteristiche"] = self._clean_features(self.source["Caratteristiche"])
        self._add_tax()
        self._add_discount_amount()
        self._fix_duplicate_references()

        # --- Step 2: prodotti base ---
        logger.info("--- Step 2: costruzione prodotti base ---")
        df_prodotti = self._build_products()

        # --- Step 3: categorie ---
        logger.info("--- Step 3: costruzione categorie ---")
        df_categorie = self._build_categories()

        # --- Step 4: combinazioni ---
        logger.info("--- Step 4: costruzione combinazioni ---")
        df_combinazioni = self._build_combinations()

        logger.info("=== Pipeline completata ===")

        if not save:
            return {
                "prodotti":      df_prodotti,
                "categorie":     df_categorie,
                "combinazioni":  df_combinazioni,
            }

        # Salvataggio su disco
        paths_prodotti     = self._save_chunks(df_prodotti,      "prodotti",     self.sep, chunk_size, "prodotti")
        paths_categorie    = self._save_chunks(df_categorie,     "categorie",    ";",      chunk_size, "categorie")
        paths_combinazioni = self._save_chunks(df_combinazioni,  "combinazioni", ";",      chunk_size, "combinazioni")

        return {
            "prodotti":     paths_prodotti,
            "categorie":    paths_categorie,
            "combinazioni": paths_combinazioni,
        }


# ---------------------------------------------------------------------- #
#  CLI                                                                    #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline catalogo fornitore → PrestaShop")

    parser.add_argument("-S",  "--source",     type=str, required=True,           help="Percorso del file CSV sorgente")
    parser.add_argument("-D",  "--sep",        type=str, required=False, default="|", help="Separatore colonne (default: |)")
    parser.add_argument("-SV", "--save",       type=int, required=False, default=0,   help="1 = salva su disco")
    parser.add_argument("-C",  "--chunk-size", type=int, required=False, default=500, help="Righe per chunk (default: 500)")
    parser.add_argument("-O", "--output-dir", type=str, required=False, default="export",
                     help="Cartella di output (default: export)")
    args = parser.parse_args()

    cm = CSVApi(args.source, sep=args.sep, output_dir=args.output_dir)
    result = cm.clean_csv(save=args.save, chunk_size=args.chunk_size)

    if not args.save:
        print("\n=== Riepilogo ===")
        print(f"Prodotti:     {len(result['prodotti'])} righe")
        print(f"Categorie:    {len(result['categorie'])} righe")
        print(f"Combinazioni: {len(result['combinazioni'])} righe")
    else:
        print("\n=== File generati ===")
        for k, paths in result.items():
            print(f"\n{k.upper()}:")
            for p in paths:
                print(f"  {p}")