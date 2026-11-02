# Vela

Vela 1.0.0 ist eine selbstgehostete Webanwendung zum Herunterladen von Video und Audio. MeTube dient als Inspiration; Oberfläche, Auftragsverwaltung und Architektur sind eigenständig umgesetzt.

Enthalten sind Metadatenvorschau, Video- und Audiodownloads, Auflösungswahl, Playlists mit optionaler Eintragsauswahl, eine dauerhafte Warteschlange mit Fortschritt, Abbruch und Wiederholen sowie Verlauf, Dateidownload und Server-/Speicherstatus. Bei Bedarf lassen sich eigene Quellsitzungen für Instagram, YouTube, TikTok, Facebook und X hinterlegen. Kanalabonnements sind noch nicht enthalten.

Vela öffnet sich **ohne Anmeldung**. Der Zugang wird auf das vertrauenswürdige Heimnetz begrenzt; alle dort zugelassenen Nutzer können Downloads und Dateien verwalten. Schreibende Browseranfragen müssen weiterhin von Vela selbst stammen.

Unter **Server → Sitzungen für Quellen → Instagram** kann ein eigener Instagram-Zugang hinterlegt werden. Ein getrennter Browserdienst verwendet dafür ein ausschließlich für Vela angelegtes Profil auf dem Heimserver, übernimmt die Sitzung und versucht bei Bedarf eine neue Anmeldung. Verlangt Instagram eine Bestätigung, erscheint ein interaktives Anmeldefenster in Vela; wartende Aufträge können danach automatisch fortfahren. Ein dauerhaft unbeaufsichtigter Betrieb und der Zugriff auf jedes Video sind nicht garantiert.

Alternativ kann für die unterstützten Quellen eine selbst exportierte `cookies.txt` im Netscape-Format hochgeladen werden. Vela übernimmt nur erlaubte Domains der gewählten Quelle und liest keine persönlichen Browserprofile aus. Zugangsdaten und Cookie-Dateien gehören ausschließlich in Vela, niemals in den Chat oder ins Repository. Abrufe erfolgen direkt, ohne externe Download-Dienste.

Die Anwendung verwendet Python/FastAPI, React/TypeScript/Vite und SQLite. API, Download-Worker und Instagram-Browserdienst laufen getrennt; yt-dlp, Deno und FFmpeg bilden die Engine. Die [Architekturentscheidung](docs/architecture.md) hält Aufbau und ursprüngliche Analyse fest. Der [Betrieb](docs/operations.md) beschreibt die Bereitstellung.

Downloadziel ist `\\unas.home.arpa\Unprotected\Vela`, ein eigener Unterordner der gewünschten Freigabe. Die fehlende Redundanz dieses Speicherbereichs ist bewusst akzeptiert; Medienbackups gehören nicht zum Auftrag. Datenbank und temporäre Dateien bleiben auf dem Heimserver.

Vela läuft unter **[https://vela.home.arpa/](https://vela.home.arpa/)** und ist in NEXUS verlinkt. Am 2. November 2026 wurden Vorschau, Video, Audio und ein öffentlicher YouTube-Download bis zur Datei auf dem UNAS einschließlich Hashprüfung bestätigt. Der Worker-Neustart ließ die API ohne Ausfall erreichbar. Die Linux-Prüfung besteht aus 222 Tests, die Frontend-Prüfung aus 25 Tests. Der neue Instagram-Login benötigt für die abschließende Kontoprüfung einen vom Nutzer direkt in Vela hinterlegten Zugang.

Vela wird direkt auf dem Heimserver bereitgestellt. Eine Veröffentlichung in Orbit erfolgt ausschließlich auf ausdrücklichen Nutzerauftrag; der vorübergehend angelegte Katalogeintrag wurde auf Nutzerwunsch zurückgenommen.

## Entwicklung und Prüfung

Voraussetzungen: Python ab 3.12, SQLite mit WAL-reset-Fix und Node.js 24.13 oder kompatibel. Die Container stellen die passende SQLite-Bibliothek bereit. Für echte Downloads werden zusätzlich yt-dlp mit EJS, Deno und FFmpeg benötigt.

Im Projektverzeichnis:

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

Im Verzeichnis `frontend`:

```powershell
npm ci
npm test
npm run build
```

`npm run dev` startet die lokale Oberfläche; `/api` wird an `127.0.0.1:8080` weitergereicht. Produktionsdateien entstehen unter `frontend/dist` und werden durch die API ausgeliefert. Infrastrukturzugänge und Laufzeitdaten gehören nicht ins Repository.
