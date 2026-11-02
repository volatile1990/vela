# Vela

Vela 1.0.0 ist eine selbstgehostete Webanwendung zum Herunterladen von Video und Audio. MeTube dient als Inspiration; Oberfläche, Auftragsverwaltung und Architektur sind eigenständig umgesetzt.

Enthalten sind Metadatenvorschau, Video- und Audiodownloads, Auflösungswahl, Playlists mit optionaler Eintragsauswahl, eine dauerhafte Warteschlange mit Fortschritt, Abbruch und Wiederholen sowie Verlauf, Dateidownload und Server-/Speicherstatus. Bei Bedarf lassen sich eigene Quellsitzungen für Instagram, YouTube, TikTok, Facebook und X hinterlegen. Kanalabonnements sind noch nicht enthalten.

Vela öffnet sich **ohne Anmeldung**. Der Zugang wird auf das vertrauenswürdige Heimnetz begrenzt; alle dort zugelassenen Nutzer können Downloads und Dateien verwalten. Schreibende Browseranfragen müssen weiterhin von Vela selbst stammen.

Unter **Server → Sitzungen für Quellen → Instagram** kann ein eigener Instagram-Zugang hinterlegt werden. Ein getrennter Anmeldedienst verwendet dafür primär die lokale Python-Schnittstelle von [Instaloader](https://instaloader.github.io/). Er prüft und verwendet gespeicherte Sitzungen und meldet sich bei Bedarf mit den hinterlegten Daten neu an. Für zusätzliche Bestätigungen kann Vela ein interaktives Browserfenster öffnen oder auf die Instagram-App verweisen; wartende Aufträge können nach erfolgreicher Freigabe automatisch fortfahren. Ein dauerhaft unbeaufsichtigter Betrieb und der Zugriff auf jedes Video sind nicht garantiert.

Eine erfolgreich bestätigte Anmeldung mit neuem Sitzungscookie bleibt für Downloads nutzbar, wenn nur die zusätzliche GraphQL-Profilprüfung `feedback_required` meldet. Eine echte Sicherheitsabfrage oder eine abweichende Kontoidentität stoppt die Freigabe weiterhin.

Optional kann Vela auch die Authenticator-Codes für Instagram erzeugen. Dafür wird der Einrichtungsschlüssel direkt in Vela gespeichert, wahlweise als Base32-/TOTP-Eintrag oder über ein lokal gelesenes QR-Bild eines einzelnen Instagram-Exports aus Google Authenticator. Vela unterstützt das `otpauth-migration`-Exportformat; unbekannte Exportformate und Exporte mit mehreren Konten werden abgelehnt. Das Bild verlässt den Browser nicht, der Schlüssel wird ausschließlich an den eigenen Vela-Dienst übertragen und dort verschlüsselt gespeichert. Speichern startet keine Anmeldung. Zusätzliche Sicherheitsbestätigungen durch Instagram können weiterhin einen Eingriff erfordern.

Alternativ kann für die unterstützten Quellen eine selbst exportierte `cookies.txt` im Netscape-Format hochgeladen werden. Vela übernimmt nur erlaubte Domains der gewählten Quelle und liest keine persönlichen Browserprofile aus. Zugangsdaten und Cookie-Dateien gehören ausschließlich in Vela, niemals in den Chat oder ins Repository. Abrufe erfolgen direkt, ohne externe Download-Dienste.

Die Anwendung verwendet Python/FastAPI, React/TypeScript/Vite und SQLite. API, Download-Worker und Instagram-Anmeldedienst laufen getrennt; yt-dlp, Deno und FFmpeg bilden die Download-Engine. Instaloader übernimmt die Instagram-Anmeldung, Playwright/Chromium die nötigen Browserbestätigungen. Die [Architekturentscheidung](docs/architecture.md) hält Aufbau und ursprüngliche Analyse fest. Der [Betrieb](docs/operations.md) beschreibt die Bereitstellung.

Downloadziel ist `\\unas.home.arpa\Unprotected\Vela`, ein eigener Unterordner der gewünschten Freigabe. Die fehlende Redundanz dieses Speicherbereichs ist bewusst akzeptiert; Medienbackups gehören nicht zum Auftrag. Datenbank und temporäre Dateien bleiben auf dem Heimserver.

Vela läuft unter **[https://vela.home.arpa/](https://vela.home.arpa/)** und ist in NEXUS verlinkt. Am 2. November 2026 wurden Vorschau, Video, Audio und ein öffentlicher YouTube-Download bis zur Datei auf dem UNAS einschließlich Hashprüfung bestätigt. Der Worker-Neustart ließ die API ohne Ausfall erreichbar. Die Linux-Prüfung besteht aus 452 Tests, die Frontend-Prüfung aus 57 Tests. Bei einer neuen Instagram-Anmeldung können weiterhin Sicherheitsbestätigungen durch den Nutzer erforderlich sein.

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
