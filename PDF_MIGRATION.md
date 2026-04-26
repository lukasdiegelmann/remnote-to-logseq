# PDF-Migration RemNote → Logseq (offen, fortsetzen wenn PDFs verfügbar)

## Status der Erkundung

- **565 distinkte PDFs** in der DB referenziert
- **1.727 PDF-bezogene Rems** insgesamt (PDF-Anker, Seitenzahlen, Highlights/Markierungen)
- **0 PDFs** liegen unter `~/remnote/remnote-666c2b227ea3bab0376d53ba/files/` (dort nur PNG/JPEG/WebP)

## Speicherformen in der DB

In `quanta`-Rows mit `rcrp = "f.u"` (Power-up „Pdf" → URL-Slot):

1. **S3-URLs** — `https://remnote-user-data.s3.amazonaws.com/<hash>.pdf`
   - Eventuell noch öffentlich abrufbar, eventuell 403 nach Account-Schließung
   - 565 Requests, sollte rate-limited werden

2. **Local-File-Pointer** — `%LOCAL_FILE%<hash>.pdf`
   - Datei muss lokal vorhanden sein (Hash-basiertes Lookup)
   - Aktuell **nicht in `files/`** — Quelle muss noch gefunden werden

## Verwandte Power-up-Rems

PDFs sind in RemNote als „Pdf"-Power-up modelliert. Pro PDF gibt es Kind-Rems mit verschiedenen `rcrp`-Slots:

| `rcrp` | Bedeutung | `value`-Beispiel |
|--------|-----------|-----|
| `f.u`  | URL/Datei | `["…s3.amazonaws.com/….pdf"]` oder `["%LOCAL_FILE%….pdf"]` |
| `f.n`  | Originaldateiname | `["Praktikumsrichtlinien_abWiSe2223.pdf"]` |
| `f.p`  | Seitenzahl (vermutlich) | tbd |
| `f.h`  | Highlight (vermutlich) | tbd |

PDF-Markierung-/Highlight-Rems heißen im UI „PDF-Markierung" und „PDF-Seitenzahl". Genaues Schema noch zu verifizieren.

## Quellen, die der User morgen prüft

- Alter RemNote-Desktop-Cache: `~/.config/RemNote/`, `~/.local/share/RemNote/`
- Downloads-Ordner
- Dropbox / iCloud-Sync-Pfad
- Backup-Verzeichnis

→ Sobald Pfad bekannt: Hash → Datei mappen und nach `~/Documents/Logseq/Graphs/2LD/assets/` kopieren.

## Migrations-Strategie (entschieden, sobald PDFs vorliegen)

1. **PDF-Datei einbinden:** in `assets/` kopieren, im Markdown verlinken via
   `[Originalname.pdf](../assets/<hash>.pdf)`
   (Logseq erkennt `.pdf`-Links und öffnet im internen PDF-Viewer.)

2. **S3-URLs (Fallback):** mit `urllib`, sequenziell, 1 req/s, Timeout 10s.
   Bei Fehler: Markdown-Link auf die Original-URL belassen + Warnung.

3. **Highlights** — drei Optionen, **noch zu entscheiden**:
   - **a)** Pro PDF einen Sammel-Block mit Liste der Highlight-Texte + Seitenzahl. Kein Klick-Sprung, aber Inhalt erhalten.
   - **b)** Highlights komplett ignorieren.
   - **c)** Logseq-natives `assets/<pdf>.edn`-Highlight-Format erzeugen. Komplexer; fragil; Geometrie-Mapping nötig.

4. **PDF-Anker / Seitenzahl-Refs** im Fließtext: als Block-Link auf den PDF-Block + Seitenzahl im Text-Content.

## Skript-Anpassung

In `migrate_logseq.py`:

- Beim Asset-Lookup `.pdf`-Endung erlauben (aktuell nur Bilder).
- Neue Detection: Power-up-Pdf-Rem → emit als spezieller Block mit Link zum PDF in `assets/`.
- Optional: Highlight-Rems sammeln und als Kind-Blöcke unter dem PDF-Block emittieren.

## Aufgaben für nächste Session

- [ ] User stellt lokale PDFs bereit / nennt Quellpfad
- [ ] DB-Schema für `f.p`, `f.h` final verifizieren (3 Sample-Highlight-Rems dumpen)
- [ ] Highlight-Strategie wählen (a/b/c)
- [ ] `migrate_logseq.py` patchen
- [ ] Re-run + Verifikation: alle 565 PDFs als klickbare Links in Logseq, ggf. Highlights als Subblöcke
